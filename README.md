# Webhook Lab

**Capture the bytes. Understand the event. Reproduce the delivery.**

[![Tests](https://github.com/ozof70/webhook-lab/actions/workflows/test.yml/badge.svg)](https://github.com/ozof70/webhook-lab/actions/workflows/test.yml)
![Python](https://img.shields.io/badge/python-3.13-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-009688)
![Storage](https://img.shields.io/badge/storage-SQLite_WAL-003B57)

A self-hosted webhook debugging toolkit built with FastAPI, SQLite, and Docker. Capture binary payloads, inspect requests, deduplicate retries, and replay events to named HTTPS targets with persistent delivery audit trails.

## Why it exists

A webhook failure is often difficult to reproduce: the original bytes disappear into logs, retries create duplicate events, and a manual resend loses its history. Webhook Lab keeps the captured request and every replay attempt together so you can investigate what arrived and what happened next.

## Engineering highlights

| Capability | Implementation |
| --- | --- |
| Byte-preserving capture | BLOB storage, Base64 inspection, SHA-256 payload fingerprints |
| Concurrent deduplication | Endpoint-scoped idempotency keys, unique constraints, atomic SQLite transactions |
| Conflict detection | Reusing a key with different method, query, Content-Type, or payload returns `409` |
| Controlled replay | Named HTTPS target allowlist, redirects disabled, bounded attempts, exponential backoff |
| Delivery observability | Per-attempt status, error class, latency, timestamp, and aggregate statistics |
| Credential boundaries | Separate admin key and hashed capture tokens; common credential headers redacted |
| Async request handling | Streaming body limits and outbound responses; database work offloaded from the event loop |
| Reproducible development | Pinned direct dependencies, mocked integration tests, GitHub Actions, non-root Docker |

## Quick start

```sh
git clone https://github.com/ozof70/webhook-lab.git
cd webhook-lab
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `WEBHOOK_API_KEY` in `.env` to the generated value. Configure `REPLAY_TARGETS` with an HTTPS staging endpoint that you control, or use `{}` to disable replay destinations.

```dotenv
WEBHOOK_API_KEY=your-random-secret-at-least-32-characters
REPLAY_TARGETS={"staging":"https://your-service.example/webhook"}
```

```sh
docker compose up --build -d
```

Open **http://localhost:8000/docs** for interactive API documentation. Enter the admin key in the `x-api-key` field of management operations. The service binds to loopback by default; events persist in a Docker volume.

On PowerShell, copy the configuration with `Copy-Item .env.example .env`.

## Try the complete workflow

The following shell example uses `curl` and `jq`. Set `WEBHOOK_API_KEY` to the same value configured on the server.

```sh
BASE=http://localhost:8000

# 1. Provision an isolated capture endpoint.
ENDPOINT=$(curl -fsS "$BASE/api/endpoints" \
  -H "X-API-Key: $WEBHOOK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"payment-sandbox"}')
CAPTURE_PATH=$(echo "$ENDPOINT" | jq -r .capture_path)
CAPTURE_TOKEN=$(echo "$ENDPOINT" | jq -r .capture_token)

# 2. Capture a payment event. Repeating this key and content returns the same event.
EVENT=$(curl -fsS "$BASE$CAPTURE_PATH" \
  -H "X-Webhook-Token: $CAPTURE_TOKEN" \
  -H 'Idempotency-Key: payment-42' \
  -H 'Content-Type: application/json' \
  -d '{"type":"payment.succeeded","amount":1200}')
EVENT_ID=$(echo "$EVENT" | jq -r .id)

# 3. Inspect the captured bytes, fingerprint, metadata, and delivery history.
curl -fsS "$BASE/api/events/$EVENT_ID" \
  -H "X-API-Key: $WEBHOOK_API_KEY" | jq

# 4. Replay to the operator-configured staging destination.
curl -fsS "$BASE/api/events/$EVENT_ID/replay" \
  -H "X-API-Key: $WEBHOOK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"target":"staging","attempts":3}' | jq

# 5. Inspect capture volume and successful / failed delivery attempts.
curl -fsS "$BASE/api/stats" -H "X-API-Key: $WEBHOOK_API_KEY" | jq
```

Capture response:

```json
{
  "id": "8c060746-0a59-40e4-90eb-53ab689158b1",
  "bytes": 42,
  "sha256": "<64-character payload digest>",
  "duplicate": false
}
```

The digest and byte count above illustrate the response shape. Repeating a matching idempotent capture returns the original `id` with `duplicate: true`; the capture token is returned only when the endpoint is created.

## API surface

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | None | Database readiness |
| POST / GET | `/api/endpoints` | Admin key | Create / list capture endpoints |
| POST / PUT / PATCH | `/hooks/{id}` | Capture token | Capture an event |
| GET | `/api/events` | Admin key | Filter and paginate events |
| GET | `/api/events/{id}` | Admin key | Inspect payload and delivery history |
| GET | `/api/targets` | Admin key | List configured target names |
| GET | `/api/stats` | Admin key | Capture and delivery aggregates |
| POST | `/api/events/{id}/replay` | Admin key | Replay a captured event |

Event listing supports `endpoint_id`, `limit` (1–100), and `offset`. Detailed bodies are Base64 encoded so non-UTF-8 payloads survive inspection intact.

## Design decisions

**Deduplication is transactional.** An optional `Idempotency-Key` (1–128 characters) is scoped to a capture endpoint. Key lookup and event insertion run inside `BEGIN IMMEDIATE`, backed by a composite primary key. Concurrent requests with the same key and fingerprint resolve to one event. Keys persist across restarts and currently do not expire. Without a key, each request creates a new event. Other headers are not part of the fingerprint; the first capture's metadata is retained.

**Replay has explicit boundaries.** Replay preserves the original method, payload, `Content-Type`, and `Accept`, and adds `X-Webhook-Lab-Event`. It does not forward the original query string or other incoming headers. Destinations are fixed operator-configured HTTPS URLs, never caller-supplied arbitrary URLs. Redirects and environment proxies are disabled.

**Retries expose side effects.** The default is one attempt. `attempts` includes the first delivery and is capped at three. Transport failures, HTTP 429, and HTTP 5xx retry after 0.5 and 1 second. Other responses stop the loop, including redirects and non-429 client errors. `Retry-After` is not interpreted. Receivers should deduplicate by `X-Webhook-Lab-Event` because a timeout can occur after the receiver processes an event.

**Storage stays inspectable.** SQLite WAL stores endpoints, binary events, idempotency mappings, and delivery attempts. Statistics use one read transaction for a consistent snapshot. Blocking database operations in async routes run in the thread pool. This keeps the deployment small and the implementation straightforward to review.

## Development

Requires Python 3.13.

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Set the environment before starting the application. Local development does **not** automatically load `.env`.

```powershell
$env:WEBHOOK_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:REPLAY_TARGETS = '{}'
python -m uvicorn app.main:create_app --factory --reload
```

On Linux/macOS, use `export WEBHOOK_API_KEY=...` and `export REPLAY_TARGETS='{}'`.

```sh
python -m pytest -q
```

Tests use temporary databases and HTTPX mock transports. They cover authentication, binary integrity, redaction, payload limits, pagination, retry decisions, network errors, delivery audits, persistence, concurrent deduplication, conflict handling, and statistics. No test delivers webhooks to external services.

## Deployment scope

This version is an internal debugging service for a single instance. Replay runs inside the HTTP request; there is no durable background queue, multi-tenant isolation, automatic retention, or rate limiter. A process crash can leave an in-flight delivery without an audit row. Back up the database and plan storage retention before sustained use.

The capture sender must support a custom `X-Webhook-Token` header. Provider-specific webhook signatures are not verified. SHA-256 is a content fingerprint, not proof of sender authenticity.

Payloads, query strings, and custom headers can contain sensitive data. Redacting common credential headers is not complete anonymization. Externally accessible deployments need HTTPS, access controls, request timeouts, and traffic limits at the reverse proxy. Configure only trusted replay domains and enforce network egress rules to block private networks and cloud metadata endpoints; URL allowlisting alone does not prevent DNS rebinding.

## Project map

```text
app/main.py                 Application factory, API routes, storage and replay policy
tests/test_api.py           Integration, concurrency and failure-path tests
.github/workflows/test.yml  CI test workflow
Dockerfile                  Non-root Python runtime
compose.yaml                Local service and persistent volume
.env.example                Configuration template
```

Built on [FastAPI](https://fastapi.tiangolo.com/tutorial/) and [HTTPX](https://www.python-httpx.org/advanced/transports/).
