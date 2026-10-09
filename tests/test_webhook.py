import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import webhook_app
from noisebot.config import Settings


def _sign(body: bytes, secret: str = "test-webhook-key") -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _env(monkeypatch):
    monkeypatch.setenv("SENTRY_ORG", "sandbox")
    monkeypatch.setenv("SENTRY_PROJECT", "noise-repro")
    monkeypatch.setenv("SENTRY_TOKEN", "test-token")
    monkeypatch.setenv("SENTRY_CLIENT_SECRET", "test-webhook-key")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SAVE_WEBHOOK_SAMPLE", "false")


def _payload(issue_id: str = "42") -> bytes:
    return json.dumps(
        {
            "action": "triggered",
            "data": {"event": {"issue_id": issue_id, "title": "Example", "release": "sample-app@6.1.0"}},
        }
    ).encode()


def test_valid_signature_accepts_and_queues(monkeypatch):
    _env(monkeypatch)
    seen = {}

    def fake_process(issue_id, settings, event=None):
        seen["issue_id"] = issue_id
        seen["release"] = (event or {}).get("release")
        assert isinstance(settings, Settings)
        return "would_discard"

    monkeypatch.setattr(webhook_app, "process_issue", fake_process)
    body = _payload()
    client = TestClient(webhook_app.app)
    response = client.post(
        "/sentry",
        content=body,
        headers={
            "Sentry-Hook-Resource": "event_alert",
            "Sentry-Hook-Signature": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert seen == {"issue_id": "42", "release": "sample-app@6.1.0"}


def test_tampered_body_is_rejected(monkeypatch):
    _env(monkeypatch)
    called = False

    def fake_process(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(webhook_app, "process_issue", fake_process)
    body = _payload()
    signature = _sign(body)
    tampered = body[:-1] + (b"}" if body[-1:] != b"}" else b" ")
    client = TestClient(webhook_app.app)
    response = client.post(
        "/sentry",
        content=tampered,
        headers={
            "Sentry-Hook-Resource": "event_alert",
            "Sentry-Hook-Signature": signature,
        },
    )
    assert response.status_code == 401
    assert called is False


def test_non_event_alert_is_ignored(monkeypatch):
    _env(monkeypatch)
    called = False

    def fake_process(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(webhook_app, "process_issue", fake_process)
    body = _payload()
    client = TestClient(webhook_app.app)
    response = client.post(
        "/sentry",
        content=body,
        headers={
            "Sentry-Hook-Resource": "issue",
            "Sentry-Hook-Signature": _sign(body),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert called is False


def test_issue_id_comes_from_the_event():
    body = json.dumps({"data": {"event": {"issue_id": 99}}}).encode()
    status, payload, issue_id = webhook_app.handle_webhook(
        body,
        {"Sentry-Hook-Resource": "event_alert", "Sentry-Hook-Signature": _sign(body)},
        _settings_stub(),
    )
    assert status == 202
    assert issue_id == "99"
    assert payload["issue_id"] == "99"


def test_lambda_handler_runs_the_same_check(monkeypatch):
    _env(monkeypatch)
    seen = {}

    def fake_process(issue_id, settings, event=None):
        seen["issue_id"] = issue_id
        return "would_discard"

    monkeypatch.setattr(webhook_app, "process_issue", fake_process)
    body = _payload("7")
    result = webhook_app.handler(
        {
            "body": body.decode(),
            "isBase64Encoded": False,
            "headers": {
                "Sentry-Hook-Resource": "event_alert",
                "Sentry-Hook-Signature": _sign(body),
            },
        },
        None,
    )
    assert result["statusCode"] == 202
    assert seen["issue_id"] == "7"


def _settings_stub():
    from pathlib import Path

    from noisebot.config import Settings

    return Settings(
        org="sandbox",
        project="noise-repro",
        token="test-token",
        dsn=None,
        client_secret="test-webhook-key",
        host="https://sentry.io",
        dry_run=True,
        min_age_days=0,
        daily_cap=20,
        threshold=50,
        window_hours=24,
        allowed_projects=frozenset({"noise-repro"}),
        require_owner=False,
        ledger_path=Path("/tmp/noise-repro-test-ledger.sqlite"),
        save_webhook_sample=False,
        team=None,
    )
