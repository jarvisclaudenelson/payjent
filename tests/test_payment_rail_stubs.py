import hashlib
import hmac
import json

from sqlmodel import Session

from payjent.auth import create_bot_credential
from payjent.config import Settings, get_settings
from payjent.models import PaymentSession, Quote
from payjent.providers.stripe import create_stripe_checkout_session
import payjent.main as main_module
from payjent.main import app


def _checkout(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    return q, ps


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_stripe_webhook_rejects_missing_or_invalid_signature_when_secret_configured(client, quote_payload, bot_headers):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=secret)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"metadata": {"payment_session_id": ps["id"]}}}}).encode()

    missing = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json"})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "missing Stripe signature"

    invalid = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json", "Stripe-Signature": "t=1700000000,v1=bad"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid Stripe signature"


def test_stripe_webhook_marks_paid_and_duplicate_is_idempotent(client, quote_payload, bot_headers):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=secret)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"payment_session_id": ps["id"]}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert paid.status_code == 200
    payload = paid.json()
    assert payload["processed"] is True
    assert payload["payment_session"]["status"] == "paid"
    assert payload["payment_session"]["provider"] == "stripe"
    assert payload["receipt"]["payload"]["provider"] == "stripe"
    assert payload["grant"]["payload"]["quote_id"] == ps["quote_id"]

    duplicate = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["processed"] is False
    assert duplicate.json()["reason"] == "payment session already paid"


def test_stripe_adapter_builds_checkout_payload_idempotency_and_metadata():
    quote = Quote(
        id="quote_1",
        bot_id="bot-1",
        external_user_id="user-1",
        request_summary="do a thing",
        request_hash="hash-1",
        amount_minor=250,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 250}],
        quote_hash="qh",
    )
    payment_session = PaymentSession(id="ps_1", quote_id="quote_1", provider="stripe", idempotency_key="idem-1")
    calls = {}

    class FakeStripeClient:
        def create_checkout_session(self, payload, idempotency_key):
            calls["payload"] = payload
            calls["idempotency_key"] = idempotency_key
            return {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"}

    provider_session_id, url = create_stripe_checkout_session(
        quote,
        payment_session,
        Settings(stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example"),
        client=FakeStripeClient(),
    )

    assert provider_session_id == "cs_test_123"
    assert url == "https://checkout.stripe.test/session"
    assert calls["idempotency_key"] == "idem-1"
    assert calls["payload"]["line_items"][0]["price_data"]["unit_amount"] == 250
    assert calls["payload"]["line_items"][0]["price_data"]["currency"] == "usd"
    assert calls["payload"]["success_url"] == "https://payjent.example/status/ps_1?checkout=success"
    assert calls["payload"]["cancel_url"] == "https://payjent.example/pay/ps_1?checkout=cancelled"
    assert calls["payload"]["metadata"] == {
        "quote_id": "quote_1",
        "payment_session_id": "ps_1",
        "bot_id": "bot-1",
        "request_hash": "hash-1",
    }
    assert calls["payload"]["payment_intent_data"]["metadata"] == calls["payload"]["metadata"]


def test_stripe_checkout_uses_adapter_payload_and_does_not_mark_paid(client, quote_payload, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
    )
    captured = {}

    def fake_create(quote, payment_session, settings):
        captured["quote_id"] = quote.id
        captured["payment_session_idempotency_key"] = payment_session.idempotency_key
        captured["settings"] = settings
        return "cs_test_123", "https://checkout.stripe.test/session"

    monkeypatch.setattr(main_module, "create_stripe_checkout_session", fake_create)
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    response = client.post(
        f"/api/v1/quotes/{q['id']}/checkout",
        headers={**bot_headers, "Idempotency-Key": "idem-1"},
    )

    assert response.status_code == 200
    ps = response.json()
    assert ps["provider"] == "stripe"
    assert ps["status"] == "checkout_created"
    assert ps["provider_session_id"] == "cs_test_123"
    assert ps["checkout_url"] == "https://checkout.stripe.test/session"
    assert ps["receipt_id"] is None
    assert captured["quote_id"] == q["id"]
    assert captured["payment_session_idempotency_key"] == "idem-1"


def test_agent_action_stripe_provider_returns_hosted_payment_prompt(client, quote_payload, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_prompt", "https://checkout.stripe.test/prompt"))

    response = client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["payment_url"] == "https://checkout.stripe.test/prompt"
    assert data["payment_prompt"]["payment_url"] == "https://checkout.stripe.test/prompt"
    assert "https://checkout.stripe.test/prompt" in data["payment_prompt"]["message"]


def test_production_mock_and_local_checkout_fail_closed(client, engine, quote_payload):
    settings = Settings(
        env="production",
        dev_mode=False,
        signing_secret="prod-signing-secret-for-test",
        checkout_provider="mock",
        public_base_url="https://payjent.example",
    )
    api_key = "prod-mock-fails-key"
    with Session(engine) as session:
        create_bot_credential(session, "bot-1", api_key, settings.signing_secret)
    app.dependency_overrides[get_settings] = lambda: settings
    headers = {"Authorization": f"Bearer {api_key}"}
    q = client.post("/api/v1/quotes", json=quote_payload, headers=headers).json()

    for override in (None, "local"):
        req_headers = dict(headers)
        if override:
            req_headers["X-Payjent-Provider"] = override
        response = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=req_headers)
        assert response.status_code == 503
        assert response.json()["detail"] == "active checkout provider not configured"


def test_stripe_pay_page_shows_secure_cta_not_mock_form(client, quote_payload, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_page", "https://checkout.stripe.test/page"))
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()

    response = client.get(f"/pay/{ps['id']}")

    assert response.status_code == 200
    assert "Continue to secure payment" in response.text
    assert "https://checkout.stripe.test/page" in response.text
    assert "/mock-pay" not in response.text


def test_payment_readiness_reports_booleans_without_secret_values(client):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_do_not_leak",
        stripe_webhook_secret="whsec_do_not_leak",
        public_base_url="https://payjent.example",
        database_url="postgresql://user:password@db/payjent",
    )

    response = client.get("/api/v1/payment-readiness")

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "active_payment_ready": True,
        "checkout_provider": "stripe",
        "stripe_secret_configured": True,
        "stripe_webhook_configured": True,
        "public_base_url_configured": True,
        "database_configured": True,
    }
    body = response.text
    assert "sk_test_do_not_leak" not in body
    assert "whsec_do_not_leak" not in body
    assert "postgresql://" not in body


def test_stripe_checkout_fails_closed_when_config_missing(client, quote_payload, bot_headers):
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key=None, public_base_url="https://payjent.example")
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    response = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "PAYJENT_STRIPE_SECRET_KEY is required for Stripe checkout"

    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url=None)
    response = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "PAYJENT_PUBLIC_BASE_URL is required for Stripe checkout"


def test_production_per_request_stripe_requires_webhook_secret_before_provider_call(
    client, engine, quote_payload, monkeypatch
):
    settings = Settings(
        env="production",
        dev_mode=False,
        signing_secret="prod-signing-secret-for-test",
        checkout_provider="mock",
        public_base_url="https://payjent.example",
        stripe_secret_key="sk_test_fake",
        stripe_webhook_secret=None,
    )
    api_key = "prod-test-bot-key"
    with Session(engine) as session:
        create_bot_credential(session, "bot-1", api_key, settings.signing_secret)
    app.dependency_overrides[get_settings] = lambda: settings

    provider_called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("Stripe provider should not be called without a production webhook secret")

    monkeypatch.setattr(main_module, "create_stripe_checkout_session", fail_if_called)
    headers = {"Authorization": f"Bearer {api_key}"}
    quote = client.post("/api/v1/quotes", json=quote_payload, headers=headers).json()

    response = client.post(
        f"/api/v1/quotes/{quote['id']}/checkout",
        headers={**headers, "X-Payjent-Provider": "stripe"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "PAYJENT_STRIPE_WEBHOOK_SECRET is required for Stripe checkout in production"
    assert provider_called is False


def test_stripe_webhook_can_map_provider_session_id(client, quote_payload, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret=secret,
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_map", "https://checkout.stripe.test/session"))
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_map", "payment_status": "paid"}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert paid.status_code == 200
    assert paid.json()["processed"] is True
    assert paid.json()["payment_session"]["id"] == ps["id"]
    assert paid.json()["payment_session"]["status"] == "paid"


def test_stripe_webhook_rejects_unconfigured_secret_without_marking_paid(client, quote_payload, bot_headers):
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=None)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = {"type": "payment_intent.succeeded", "data": {"object": {"metadata": {"payment_session_id": ps["id"]}}}}
    response = client.post("/api/v1/webhooks/stripe", json=body)
    assert response.status_code == 503
    assert response.json()["detail"] == "Stripe webhook secret not configured"

    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}")
    assert unchanged.json()["status"] == "checkout_created"


def test_crypto_mark_paid_requires_operator_and_uses_shared_issuance(client, quote_payload, bot_headers, operator_headers):
    _, ps = _checkout(client, quote_payload, bot_headers)

    denied = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=bot_headers)
    assert denied.status_code == 403

    paid = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=operator_headers)
    assert paid.status_code == 200
    payload = paid.json()
    assert payload["payment_session"]["status"] == "paid"
    assert payload["payment_session"]["provider"] == "crypto-manual"
    assert payload["receipt"]["payload"]["provider"] == "crypto-manual"
    assert payload["grant"]["payload"]["quote_id"] == ps["quote_id"]

    duplicate = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=operator_headers)
    assert duplicate.status_code == 409
