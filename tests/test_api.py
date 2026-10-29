import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import Settings, create_app

KEY = "test-key-" + "x" * 32
AUTH = {"X-API-Key": KEY}


@pytest.fixture
def lab(tmp_path):
    sent = []
    statuses = [200]

    def handler(request):
        sent.append(request)
        return httpx.Response(statuses.pop(0) if len(statuses) > 1 else statuses[0])

    cfg = Settings(api_key=KEY, database=str(tmp_path / "test.db"),
                   targets={"staging": "https://example.com/webhook"}, retry_delay=0)
    with TestClient(create_app(cfg, httpx.MockTransport(handler))) as client:
        yield client, sent, statuses


def captured(client, body=b'\x00\xff{"event":"paid"}'):
    ep = client.post("/api/endpoints", headers=AUTH, json={"name": "Stripe sandbox"}).json()
    response = client.post(ep["capture_path"] + "?test=1", content=body,
                           headers={"X-Webhook-Token": ep["capture_token"],
                                    "Authorization": "secret", "Content-Type": "application/json"})
    assert response.status_code == 202
    return ep, response.json()["id"]


def test_auth_and_health(lab):
    client, _, _ = lab
    assert client.get("/health").status_code == 200
    assert client.get("/api/events").status_code == 401
    assert client.post("/api/endpoints", json={"name": "test"}).status_code == 401


def test_binary_capture_and_redaction(lab):
    client, _, _ = lab
    ep, identifier = captured(client)
    item = client.get(f"/api/events/{identifier}", headers=AUTH).json()
    assert base64.b64decode(item["body_base64"]) == b'\x00\xff{"event":"paid"}'
    assert dict(item["headers"])["authorization"] == "[REDACTED]"
    assert dict(item["headers"])["x-webhook-token"] == "[REDACTED]"
    assert item["query"] == "test=1"
    assert client.post(ep["capture_path"], content=b"bad").status_code == 404
    assert len(client.get("/api/events", headers=AUTH, params={"endpoint_id": ep["id"]}).json()) == 1
    assert client.get("/api/events?offset=1", headers=AUTH).json() == []


def test_size_limit(lab):
    client, _, _ = lab
    ep, _ = captured(client)
    response = client.post(ep["capture_path"], content=b"x" * 1_048_577,
                           headers={"X-Webhook-Token": ep["capture_token"]})
    assert response.status_code == 413
    assert len(client.get("/api/events", headers=AUTH).json()) == 1


def test_retry_and_audit(lab):
    client, sent, statuses = lab
    _, identifier = captured(client)
    statuses[:] = [503, 429, 204]
    response = client.post(f"/api/events/{identifier}/replay", headers=AUTH,
                           json={"target": "staging", "attempts": 3})
    assert [d["status"] for d in response.json()["deliveries"]] == [503, 429, 204]
    assert len(sent) == 3
    assert sent[0].content == b'\x00\xff{"event":"paid"}'
    assert "authorization" not in sent[0].headers
    assert "x-webhook-token" not in sent[0].headers
    assert str(sent[0].url) == "https://example.com/webhook"
    assert len(client.get(f"/api/events/{identifier}", headers=AUTH).json()["deliveries"]) == 3


@pytest.mark.parametrize("status", [200, 302, 400, 401])
def test_no_retry_for_success_redirect_or_client_error(lab, status):
    client, sent, statuses = lab
    statuses[:] = [status]
    _, identifier = captured(client)
    client.post(f"/api/events/{identifier}/replay", headers=AUTH,
                json={"target": "staging", "attempts": 3})
    assert len(sent) == 1


def test_unapproved_target_and_missing_event(lab):
    client, sent, _ = lab
    _, identifier = captured(client)
    assert client.post(f"/api/events/{identifier}/replay", headers=AUTH,
                       json={"target": "http://127.0.0.1"}).status_code == 422
    assert not sent
    assert client.get("/api/events/missing", headers=AUTH).status_code == 404


def test_network_error_is_audited(tmp_path):
    def fail(request):
        raise httpx.ConnectTimeout("timeout", request=request)
    cfg = Settings(api_key=KEY, database=str(tmp_path / "db"),
                   targets={"test": "https://example.com"}, retry_delay=0)
    with TestClient(create_app(cfg, httpx.MockTransport(fail))) as client:
        _, identifier = captured(client)
        result = client.post(f"/api/events/{identifier}/replay", headers=AUTH,
                             json={"target": "test", "attempts": 2}).json()
        assert [d["error"] for d in result["deliveries"]] == ["ConnectTimeout"] * 2


def test_persistence(tmp_path):
    cfg = Settings(api_key=KEY, database=str(tmp_path / "db"))
    with TestClient(create_app(cfg)) as client:
        _, identifier = captured(client)
    with TestClient(create_app(cfg)) as client:
        assert client.get(f"/api/events/{identifier}", headers=AUTH).status_code == 200


def test_configuration():
    with pytest.raises(RuntimeError):
        create_app(Settings(api_key="short"))
    with pytest.raises(RuntimeError):
        create_app(Settings(api_key=KEY, targets={"bad": "http://localhost"}))


def test_idempotent_capture_and_conflict(lab):
    client, _, _ = lab
    ep, _ = captured(client)
    headers = {"X-Webhook-Token": ep["capture_token"], "Idempotency-Key": "payment-42"}
    first = client.post(ep["capture_path"], headers=headers, content=b"paid").json()
    second = client.post(ep["capture_path"], headers=headers, content=b"paid").json()
    assert first["id"] == second["id"]
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["sha256"] == hashlib.sha256(b"paid").hexdigest()
    conflict = client.post(ep["capture_path"], headers=headers, content=b"different")
    assert conflict.status_code == 409
    assert len(client.get("/api/events", headers=AUTH).json()) == 2


def test_concurrent_duplicates_share_one_event(lab):
    client, _, _ = lab
    ep, _ = captured(client)
    headers = {"X-Webhook-Token": ep["capture_token"], "Idempotency-Key": "concurrent"}

    def send(_):
        response = client.post(ep["capture_path"], headers=headers, content=b"same")
        assert response.status_code == 202
        return response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(8)))
    assert len({item["id"] for item in results}) == 1
    assert sum(not item["duplicate"] for item in results) == 1
    assert len(client.get("/api/events", headers=AUTH).json()) == 2


def test_idempotency_keys_are_scoped_to_endpoint(lab):
    client, _, _ = lab
    endpoints = [captured(client)[0] for _ in range(2)]
    results = [client.post(ep["capture_path"], content=b"payload", headers={
        "X-Webhook-Token": ep["capture_token"], "Idempotency-Key": "shared"
    }).json() for ep in endpoints]
    assert results[0]["id"] != results[1]["id"]


@pytest.mark.parametrize("change", ["method", "query", "content-type"])
def test_idempotency_detects_request_metadata_changes(lab, change):
    client, _, _ = lab
    ep, _ = captured(client)
    headers = {"X-Webhook-Token": ep["capture_token"], "Idempotency-Key": "metadata"}
    client.post(ep["capture_path"], headers=headers, content=b"same")
    method, path = "POST", ep["capture_path"]
    if change == "method":
        method = "PUT"
    elif change == "query":
        path += "?changed=1"
    else:
        headers["Content-Type"] = "application/json"
    assert client.request(method, path, headers=headers, content=b"same").status_code == 409


def test_stats_and_payload_fingerprint(lab):
    client, _, statuses = lab
    empty = client.get("/api/stats", headers=AUTH).json()
    assert empty["capture"] == {"events": 0, "captured_bytes": 0}
    assert empty["delivery"]["attempts"] == 0
    _, identifier = captured(client, b"payload")
    item = client.get(f"/api/events/{identifier}", headers=AUTH).json()
    assert item["sha256"] == hashlib.sha256(b"payload").hexdigest()
    statuses[:] = [503, 200]
    client.post(f"/api/events/{identifier}/replay", headers=AUTH,
                json={"target": "staging", "attempts": 2})
    stats = client.get("/api/stats", headers=AUTH).json()
    assert stats["capture"] == {"events": 1, "captured_bytes": 7}
    assert stats["delivery"]["attempts"] == 2
    assert stats["delivery"]["successful"] == 1
    assert stats["delivery"]["failed"] == 1
    assert client.get("/api/stats").status_code == 401


def test_idempotency_survives_restart(tmp_path):
    cfg = Settings(api_key=KEY, database=str(tmp_path / "db"))
    with TestClient(create_app(cfg)) as client:
        ep, _ = captured(client)
        headers = {"X-Webhook-Token": ep["capture_token"], "Idempotency-Key": "persistent"}
        first = client.post(ep["capture_path"], headers=headers, content=b"same").json()
    with TestClient(create_app(cfg)) as client:
        second = client.post(ep["capture_path"], headers=headers, content=b"same").json()
        assert first["id"] == second["id"]
        assert second["duplicate"] is True
