import time

import pytest
from sqlmodel import Session, select

from payjent.config import get_settings
from payjent.models import PaymentSession, WebhookDeliveryAttempt
from payjent.signing import sign_webhook_payload, verify_webhook_signature


def test_callback_url_validation_rejects_unsafe_schemes(client, quote_payload, bot_headers):
    for url in ["javascript:alert(1)", "file:///tmp/x", "ftp://example.com/hook", "http://example.com/hook"]:
        payload = {**quote_payload, "callback_url": url, "request_hash": f"hash-{url.split(':', 1)[0]}"}
        r = client.post("/api/v1/agent-actions", json=payload, headers=bot_headers)
        assert r.status_code == 422


def test_callback_url_validation_allows_https_and_local_http(client, quote_payload, bot_headers):
    for url in ["https://agent.example/hook", "http://testserver/hook", "http://127.0.0.1/hook"]:
        payload = {**quote_payload, "callback_url": url, "request_hash": f"hash-{url}"}
        r = client.post("/api/v1/agent-actions", json=payload, headers=bot_headers)
        assert r.status_code == 200, r.text


def test_webhook_signature_verify_valid_invalid_and_replay():
    payload = {"event_type": "agent_action.ready", "action_id": "act_1"}
    now = int(time.time())
    ts, sig = sign_webhook_payload(payload, "secret", timestamp=now)
    assert verify_webhook_signature(payload, ts, sig, "secret", now=now)
    assert not verify_webhook_signature({**payload, "action_id": "act_2"}, ts, sig, "secret", now=now)
    assert not verify_webhook_signature(payload, str(now - 999), sig, "secret", now=now, tolerance_seconds=300)


def test_callback_delivery_success_logs_and_payload_has_no_token(client, engine, quote_payload, bot_headers, operator_headers, monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 204
        text = ""

    class DummyClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return DummyResponse()

    monkeypatch.setattr("payjent.main.httpx.Client", DummyClient)
    payload = {**quote_payload, "callback_url": "https://agent.example/hook"}
    action = client.post("/api/v1/agent-actions", json=payload, headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert paid.status_code == 200
    assert calls
    body = calls[0]["json"]
    assert body["action_id"] == action["action_id"]
    assert "payment_token" not in body
    assert "grant" not in body
    assert verify_webhook_signature(body, calls[0]["headers"]["X-Payjent-Timestamp"], calls[0]["headers"]["X-Payjent-Signature"], get_settings().signing_secret)
    with Session(engine) as session:
        attempts = session.exec(select(WebhookDeliveryAttempt)).all()
        assert len(attempts) == 1
        assert attempts[0].status == "success"


def test_failing_callback_logs_failure_and_payment_stays_paid(client, engine, quote_payload, bot_headers, operator_headers, monkeypatch):
    class DummyClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            raise RuntimeError("agent down")

    monkeypatch.setattr("payjent.main.httpx.Client", DummyClient)
    payload = {**quote_payload, "callback_url": "https://agent.example/hook"}
    action = client.post("/api/v1/agent-actions", json=payload, headers=bot_headers).json()
    r = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert r.status_code == 200
    with Session(engine) as session:
        ps = session.get(PaymentSession, action["payment_session_id"])
        attempt = session.exec(select(WebhookDeliveryAttempt)).one()
        assert ps.status == "paid"
        assert attempt.status == "failed"
        assert "agent down" in attempt.error
