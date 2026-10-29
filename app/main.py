"""Webhook Lab: inspect exact payloads and replay to operator-approved targets."""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("WEBHOOK_API_KEY", ""))
    database: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "data/webhooks.db"))
    targets: dict[str, str] = field(default_factory=lambda: json.loads(os.getenv("REPLAY_TARGETS", "{}")))
    max_body: int = 1_048_576
    retry_delay: float = 0.5


def now():
    return datetime.now(timezone.utc).isoformat()


class EndpointInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ReplayInput(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    attempts: int = Field(default=1, ge=1, le=3)


def create_app(settings: Settings | None = None, transport=None):
    cfg = settings or Settings()
    if len(cfg.api_key) < 32:
        raise RuntimeError("WEBHOOK_API_KEY must contain at least 32 characters")
    for url in cfg.targets.values():
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise RuntimeError("Replay targets must be fixed HTTPS URLs without credentials or fragments")
    Path(cfg.database).parent.mkdir(parents=True, exist_ok=True)

    def db():
        conn = sqlite3.connect(cfg.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @asynccontextmanager
    async def lifespan(app):
        conn = db()
        with conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS endpoints (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
                    method TEXT NOT NULL, query TEXT NOT NULL, headers TEXT NOT NULL,
                    body BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_endpoint ON events(endpoint_id, created_at);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
                    target TEXT NOT NULL, attempt INTEGER NOT NULL, status INTEGER,
                    error TEXT, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL);
            """)
        conn.close()
        async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False,
                                     trust_env=False) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Webhook Lab", version="1.0.0", lifespan=lifespan,
                  description="Capture binary-safe webhook events, inspect them, and audit replays.")

    def authorize(x_api_key: str = Header(default="")):
        if not hmac.compare_digest(x_api_key, cfg.api_key):
            raise HTTPException(401, "Invalid API key")

    def rows(sql, args=()):
        conn = db()
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def write(sql, args):
        conn = db()
        try:
            with conn:
                conn.execute(sql, args)
        finally:
            conn.close()

    def event(event_id):
        found = rows("SELECT * FROM events WHERE id=?", (event_id,))
        if not found:
            raise HTTPException(404, "Event not found")
        return found[0]

    @app.get("/health")
    def health():
        rows("SELECT 1")
        return {"status": "ok"}

    @app.post("/api/endpoints", dependencies=[Depends(authorize)], status_code=201)
    def create_endpoint(data: EndpointInput):
        identifier, token = str(uuid4()), secrets.token_urlsafe(32)
        write("INSERT INTO endpoints VALUES (?,?,?,?)",
              (identifier, data.name, hashlib.sha256(token.encode()).hexdigest(), now()))
        return {"id": identifier, "name": data.name, "capture_path": f"/hooks/{identifier}",
                "capture_token": token, "token_header": "X-Webhook-Token"}

    @app.get("/api/endpoints", dependencies=[Depends(authorize)])
    def list_endpoints():
        return rows("SELECT id,name,created_at FROM endpoints ORDER BY created_at DESC")

    @app.api_route("/hooks/{endpoint_id}", methods=["POST", "PUT", "PATCH"] , status_code=202)
    async def capture(endpoint_id: str, request: Request, x_webhook_token: str = Header(default="")):
        found = rows("SELECT token_hash FROM endpoints WHERE id=?", (endpoint_id,))
        digest = hashlib.sha256(x_webhook_token.encode()).hexdigest()
        if not found or not hmac.compare_digest(found[0]["token_hash"], digest):
            raise HTTPException(404, "Endpoint not found")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > cfg.max_body:
                raise HTTPException(413, "Payload exceeds size limit")
            body.extend(chunk)
        # Store credentials redacted; never replay incoming authentication or proxy headers.
        sensitive = {"authorization", "cookie", "x-api-key", "x-webhook-token", "proxy-authorization"}
        headers = [(k, "[REDACTED]" if k.lower() in sensitive else v) for k, v in request.headers.items()]
        identifier = str(uuid4())
        write("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
              (identifier, endpoint_id, request.method, request.url.query, json.dumps(headers), bytes(body), now()))
        return {"id": identifier, "bytes": len(body)}

    @app.get("/api/events", dependencies=[Depends(authorize)])
    def list_events(endpoint_id: str | None = None, limit: int = Query(50, ge=1, le=100),
                    offset: int = Query(0, ge=0)):
        condition, args = ("WHERE endpoint_id=?", [endpoint_id]) if endpoint_id else ("", [])
        return rows(f"SELECT id,endpoint_id,method,created_at,length(body) AS bytes FROM events {condition} "
                    "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset))

    @app.get("/api/events/{event_id}", dependencies=[Depends(authorize)])
    def get_event(event_id: str):
        item = event(event_id)
        item["body_base64"] = base64.b64encode(item.pop("body")).decode()
        item["headers"] = json.loads(item["headers"])
        item["deliveries"] = rows("SELECT * FROM deliveries WHERE event_id=? ORDER BY created_at", (event_id,))
        return item

    @app.get("/api/targets", dependencies=[Depends(authorize)])
    def targets():
        return {"targets": list(cfg.targets)}

    @app.post("/api/events/{event_id}/replay", dependencies=[Depends(authorize)])
    async def replay(event_id: str, data: ReplayInput):
        item = event(event_id)
        if data.target not in cfg.targets:
            raise HTTPException(422, "Unknown replay target; configure REPLAY_TARGETS")
        headers = {k: v for k, v in json.loads(item["headers"]) if k.lower() in {"content-type", "accept"}}
        headers["X-Webhook-Lab-Event"] = event_id
        result = []
        for attempt in range(1, data.attempts + 1):
            start, status, error = time.monotonic(), None, None
            try:
                # Stream and close: an untrusted response cannot exhaust memory.
                async with app.state.client.stream(item["method"], cfg.targets[data.target],
                                                   content=item["body"], headers=headers) as response:
                    status = response.status_code
            except httpx.RequestError as exc:
                error = type(exc).__name__
            delivery = {"id": str(uuid4()), "event_id": event_id, "target": data.target,
                        "attempt": attempt, "status": status, "error": error,
                        "duration_ms": int((time.monotonic()-start)*1000), "created_at": now()}
            write("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?)", tuple(delivery.values()))
            result.append(delivery)
            if status is not None and status != 429 and status < 500:
                break
            if attempt < data.attempts:
                await asyncio.sleep(cfg.retry_delay * 2 ** (attempt - 1))
        return {"event_id": event_id, "deliveries": result}

    return app
