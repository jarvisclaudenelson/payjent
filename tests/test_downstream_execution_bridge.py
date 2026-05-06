import json

from payjent.config import Settings, get_settings
from payjent.main import app
import payjent.main as main_module


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    import hashlib, hmac
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _create_action(client, bot_headers, service_url="https://downstream.example/run", headers=None, body=None):
    return client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1",
            "external_user_id": "user-1",
            "request_summary": "run premium downstream action",
            "request_hash": "downstream-hash",
            "amount_minor": 250,
            "currency": "USD",
            "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": service_url,
            "method": "POST",
            "body": body or {"task": "run", "payment_token": "must-not-forward", "grant_id": "grant_leak"},
            "headers": headers or {"X-Safe": "ok", "Authorization": "Bearer leak", "X-Api-Key": "leak"},
            "payjent_managed_execution": True,
        },
    ).json()


def _paid_body(payment_session_id, amount=250, currency="usd"):
    return json.dumps({
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_downstream", "payment_session_id": payment_session_id, "amount_total": amount, "currency": currency}},
    }, separators=(",", ":")).encode()


def test_stripe_paid_webhook_executes_downstream_once_without_tokens(client, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_downstream", "https://checkout.stripe.test/session"))
    monkeypatch.setattr(main_module, "_safe_public_https_url", lambda url: (True, None))
    calls = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(main_module.httpx, "Client", FakeClient)
    action = _create_action(client, bot_headers)
    body = _paid_body(action["payment_session_id"])
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    duplicate = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)

    assert paid.status_code == 200
    assert duplicate.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == "https://downstream.example/run"
    assert calls[0]["json"] == {"task": "run"}
    sent = json.dumps(calls[0]).lower()
    assert "payment_token" not in sent
    assert "grant_" not in sent
    assert "authorization" not in {k.lower() for k in calls[0]["headers"]}
    assert "x-api-key" not in {k.lower() for k in calls[0]["headers"]}
    status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert status["fulfillment_events"][0]["status"] == "executed"
    assert status["fulfillment_events"][0]["metadata"]["type"] == "payjent_downstream_execution"


def test_unsafe_service_url_fails_closed_and_does_not_execute(client, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_downstream", "https://checkout.stripe.test/session"))
    action = _create_action(client, bot_headers, service_url="http://localhost/run")
    body = _paid_body(action["payment_session_id"])

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)})

    assert paid.status_code == 200
    status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert status["fulfillment_events"][0]["status"] == "failed"
    assert "https" in status["fulfillment_events"][0]["metadata"]["reason"]
