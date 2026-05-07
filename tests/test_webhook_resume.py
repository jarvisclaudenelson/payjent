import time

import pytest
from sqlmodel import Session, select

from payjent.config import get_settings
from payjent.models import PaymentSession, ResumeEvent, WebhookDeliveryAttempt
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


def test_resume_event_polling_fallback_and_ack(client, engine, quote_payload, bot_headers, operator_headers):
    action = client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert paid.status_code == 200

    polled = client.get("/api/v1/agents/bot-1/resume-events", headers=bot_headers)
    assert polled.status_code == 200
    events = polled.json()["events"]
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "agent_action.ready"
    assert event["action_id"] == action["action_id"]
    assert event["payload"]["resume_hint"]["consume_url"].endswith(f"/{action['action_id']}/start")
    assert "payment_token" not in str(event)
    assert "grant_" not in str(event)

    ack = client.post(f"/api/v1/resume-events/{event['id']}/ack", headers=bot_headers)
    assert ack.status_code == 200
    assert ack.json()["acked"] is True
    assert client.get("/api/v1/agents/bot-1/resume-events", headers=bot_headers).json()["events"] == []


def test_resume_event_created_for_callback_and_duplicate_settlement_has_one_active_event(client, engine, quote_payload, bot_headers, operator_headers, monkeypatch):
    calls = []

    class DummyResponse:
        status_code = 200
        text = "ok"

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
    first = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    second = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert first.status_code == 200
    assert second.status_code == 409

    body = calls[0]["json"]
    assert "payment_token" not in body
    assert "grant" not in body
    assert verify_webhook_signature(body, calls[0]["headers"]["X-Payjent-Timestamp"], calls[0]["headers"]["X-Payjent-Signature"], get_settings().signing_secret)
    with Session(engine) as session:
        events = session.exec(select(ResumeEvent)).all()
        assert len(events) == 1
        assert events[0].callback_status == "success"
        assert events[0].signature


def test_failed_callback_retained_for_retry_and_polling(client, engine, quote_payload, bot_headers, operator_headers, monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            raise RuntimeError("agent down")

    monkeypatch.setattr("payjent.main.httpx.Client", FailingClient)
    payload = {**quote_payload, "callback_url": "https://agent.example/hook"}
    action = client.post("/api/v1/agent-actions", json=payload, headers=bot_headers).json()
    assert client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).status_code == 200

    polled = client.get("/api/v1/agents/bot-1/resume-events", headers=bot_headers).json()["events"]
    assert len(polled) == 1
    assert polled[0]["callback_status"] == "failed"
    with Session(engine) as session:
        event = session.exec(select(ResumeEvent)).one()
        attempt = session.exec(select(WebhookDeliveryAttempt)).one()
        assert event.callback_attempt_id == attempt.id
        assert attempt.status == "failed"
        assert "agent down" in attempt.error
