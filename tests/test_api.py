import base64
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
