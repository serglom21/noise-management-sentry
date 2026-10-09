#!/usr/bin/env python3
"""Issue-alert webhook. Verifies the raw-body HMAC, then discards in the background.

    uvicorn webhook_app:app --port 8000

The Lambda entrypoint is ``handler``. Lambda runs the work before returning,
because the runtime freezes the process after the response. The FastAPI route
returns immediately and finishes on a background task, which is what Sentry's
one-second webhook timeout needs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from noisebot.api import SentryClient
from noisebot.config import ROOT, Settings, load_settings
from noisebot.events import event_release
from noisebot.ledger import SqliteLedger
from noisebot.pipeline import consider, load_normalized

logger = logging.getLogger("noisebot.webhook")
app = FastAPI(title="noise-repro webhook")
_SAMPLE_PATH = ROOT / "samples" / "event_alert.json"


def verify_signature(raw_body: bytes, secret: str, signature: str | None) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def extract_issue_id(payload: dict) -> str | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    issue_id = event.get("issue_id")
    if issue_id is None:
        issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
        issue_id = issue.get("id")
    if issue_id is None:
        return None
    return str(issue_id)


def extract_event(payload: dict) -> dict | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event = data.get("event")
    return event if isinstance(event, dict) else None


def save_sample(raw_body: bytes) -> None:
    """Write one raw payload. Sandbox only: the body contains event data."""

    if _SAMPLE_PATH.exists():
        return
    _SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SAMPLE_PATH.write_bytes(raw_body)
    _SAMPLE_PATH.chmod(0o600)


def handle_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
) -> tuple[int, dict, str | None]:
    """Validate the request. Returns ``(status, body, issue_id or None)``."""

    signature = _header(headers, "Sentry-Hook-Signature")
    if not settings.client_secret or not verify_signature(raw_body, settings.client_secret, signature):
        return 401, {"detail": "invalid signature"}, None

    resource = (_header(headers, "Sentry-Hook-Resource") or "").strip()
    if resource != "event_alert":
        return 200, {"status": "ignored", "resource": resource}, None

    if settings.save_webhook_sample:
        save_sample(raw_body)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"detail": "invalid json"}, None
    if not isinstance(payload, dict):
        return 400, {"detail": "invalid json"}, None

    issue_id = extract_issue_id(payload)
    if not issue_id:
        return 200, {"status": "ignored", "reason": "missing_issue_id"}, None
    return 202, {"status": "accepted", "issue_id": issue_id}, issue_id


def process_issue(issue_id: str, settings: Settings, event: dict | None = None) -> str:
    client = SentryClient(settings)
    ledger = SqliteLedger(settings.ledger_path, settings.daily_cap)
    raw = client.get_issue_with_stats(issue_id) or client.get_issue(issue_id)
    if not raw:
        logger.info("webhook issue %s not found", issue_id)
        return "missing"
    project = (raw.get("project") or {}).get("slug") or settings.project
    latest = client.latest_release_version(project)
    # The alert already applied "more than N in 1 hour". Do not wait on stats
    # here: hourly buckets lag behind the alert that just fired.
    if event is None or not event_release(event):
        fetched = client.latest_event(issue_id)
        if event is None:
            event = fetched
        elif fetched and event_release(fetched):
            event = {**event, "release": event_release(fetched)}
    count = raw.get("count")
    try:
        window_count = int(count)
    except (TypeError, ValueError):
        window_count = 0
    normalized = load_normalized(
        raw,
        client=client,
        latest_release=latest,
        window_count=window_count,
        event=event,
    )
    return consider(
        normalized,
        settings=settings,
        ledger=ledger,
        client=client,
        because="event_alert",
    )


def _settings() -> Settings:
    return load_settings(
        dotenv=True,
        require=("SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_TOKEN", "SENTRY_CLIENT_SECRET"),
    )


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.post("/sentry")
async def sentry_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    try:
        settings = _settings()
    except SystemExit as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)
    status, body, issue_id = handle_webhook(raw, request.headers, settings)
    if issue_id:
        background.add_task(process_issue, issue_id, settings, extract_event(json.loads(raw)))
    return JSONResponse(body, status_code=status)


def handler(event: dict, context: object) -> dict:
    """API Gateway proxy wrapper. Work runs inline; Lambda would drop a background task."""

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw)
    elif isinstance(raw, bytes):
        raw_body = raw
    else:
        raw_body = raw.encode("utf-8")
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    try:
        settings = _settings()
    except SystemExit as exc:
        return {"statusCode": 500, "body": json.dumps({"detail": str(exc)})}
    status, body, issue_id = handle_webhook(raw_body, headers, settings)
    if issue_id:
        payload = json.loads(raw_body.decode("utf-8"))
        process_issue(issue_id, settings, extract_event(payload))
    return {"statusCode": status, "body": json.dumps(body)}
