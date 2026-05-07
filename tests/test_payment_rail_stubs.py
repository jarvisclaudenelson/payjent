import hashlib
import hmac
import json

from fastapi import HTTPException
from sqlmodel import Session, select

from payjent.auth import create_bot_credential
from payjent.config import Settings, get_settings
from payjent.models import FulfillmentEvent, PaymentSession, Quote
from payjent.providers.stripe import create_stripe_checkout_session, create_stripe_refund
import payjent.main as main_module
from payjent.main import app


def _checkout(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    return q, ps


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _store_stripe_paid_quote(engine, *, quote_status: str = "failed") -> tuple[str, str]:
    quote = Quote(
        id="quote_refund_test",
        bot_id="bot-1",
        external_user_id="user-1",
        request_summary="refund failed action",
        request_hash="refund-hash",
        amount_minor=250,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 250}],
        execution_envelope={"action": "test"},
        quote_hash="refund-quote-hash",
        status=quote_status,
    )
    payment_session = PaymentSession(
        id="ps_refund_test",
        quote_id=quote.id,
        provider="stripe",
        status="paid",
        checkout_url="https://checkout.stripe.test/session",
        provider_session_id="cs_refund_test",
    )
    quote_id = quote.id
    payment_session_id = payment_session.id
    with Session(engine) as session:
        session.add(quote)
        session.add(payment_session)
        session.commit()
    return quote_id, payment_session_id


def test_stripe_refund_adapter_uses_payment_intent_and_safe_idempotency():
    quote = Quote(
        id="quote_adapter_refund",
        bot_id="bot-1",
        external_user_id="user-1",
        request_summary="refund adapter",
        request_hash="refund-adapter-hash",
        amount_minor=250,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 250}],
        execution_envelope={},
        quote_hash="quote-hash",
        status="failed",
    )
    payment_session = PaymentSession(
        id="ps_adapter_refund",
        quote_id=quote.id,
        provider="stripe",
        status="paid",
        provider_session_id="cs_adapter_refund",
    )

    class FakeStripeRefundClient:
        def __init__(self):
            self.refund_payload = None
            self.idempotency_key = None

        def retrieve_checkout_session(self, provider_session_id):
            assert provider_session_id == "cs_adapter_refund"
            return {"payment_intent": "pi_adapter_refund"}

        def create_refund(self, payload, idempotency_key):
            self.refund_payload = payload
            self.idempotency_key = idempotency_key
            return {"id": "re_adapter_refund", "status": "succeeded"}

    fake = FakeStripeRefundClient()
    refund_id, refund_status = create_stripe_refund(payment_session, quote, Settings(stripe_secret_key="sk_test_fake"), reason="failed action", client=fake)

    assert refund_id == "re_adapter_refund"
    assert refund_status == "succeeded"
    assert fake.idempotency_key == "refund:ps_adapter_refund"
    assert fake.refund_payload["payment_intent"] == "pi_adapter_refund"
    assert fake.refund_payload["amount"] == 250
    assert fake.refund_payload["metadata"]["payjent_refund_reason"] == "failed action"


def test_operator_can_refund_failed_stripe_payment_session(client, engine, operator_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_secret_key="sk_test_fake")
    quote_id, payment_session_id = _store_stripe_paid_quote(engine)
    calls = []
    monkeypatch.setattr(main_module, "create_stripe_refund", lambda ps, q, settings, reason=None: calls.append((ps.id, q.id, reason)) or ("re_test_refund", "succeeded"))

    response = client.post(f"/api/v1/payment-sessions/{payment_session_id}/refund", headers=operator_headers, json={"reason": "downstream action failed"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["payment_session_id"] == payment_session_id
    assert payload["quote_id"] == quote_id
    assert payload["payment_status"] == "refunded"
    assert payload["quote_status"] == "refunded"
    assert payload["refund_id"] == "re_test_refund"
    assert payload["amount_minor"] == 250
    assert calls == [(payment_session_id, quote_id, "downstream action failed")]
    with Session(engine) as session:
        stored_ps = session.get(PaymentSession, payment_session_id)
        stored_quote = session.get(Quote, quote_id)
        event = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == quote_id, FulfillmentEvent.status == "refunded")).first()
    assert stored_ps.status == "refunded"
    assert stored_quote.status == "refunded"
    assert event.metadata_json["refund_id"] == "re_test_refund"


def test_refund_requires_failed_quote_unless_forced(client, engine, operator_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_secret_key="sk_test_fake")
    quote_id, payment_session_id = _store_stripe_paid_quote(engine, quote_status="fulfilled")
    monkeypatch.setattr(main_module, "create_stripe_refund", lambda *_args, **_kwargs: ("re_forced_refund", "succeeded"))

    blocked = client.post(f"/api/v1/payment-sessions/{payment_session_id}/refund", headers=operator_headers, json={"reason": "admin review"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "quote must be failed before refund unless force=true"

    forced = client.post(f"/api/v1/payment-sessions/{payment_session_id}/refund", headers=operator_headers, json={"reason": "admin override", "force": True})
    assert forced.status_code == 200, forced.text
    assert forced.json()["refund_id"] == "re_forced_refund"

    duplicate = client.post(f"/api/v1/payment-sessions/{payment_session_id}/refund", headers=operator_headers, json={"reason": "duplicate", "force": True})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "payment session is already refunded"


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


def test_stripe_webhook_marks_paid_and_duplicate_is_idempotent(client, quote_payload, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_paid", "https://checkout.stripe.test/session"))
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_paid", "payment_session_id": ps["id"], "amount_total": quote_payload["amount_minor"], "currency": quote_payload["currency"].lower()}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert paid.status_code == 200
    payload = paid.json()
    assert payload == {"received": True, "processed": True}
    assert "grant" not in paid.text.lower()
    assert "receipt" not in paid.text.lower()
    stored = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert stored["status"] == "paid"
    assert stored["provider"] == "stripe"
    status = client.get(f"/api/v1/agent-actions/{ps['quote_id']}", headers=bot_headers).json()
    assert status["payment_token_status"] == "available"
    assert status["payment_token"]

    duplicate = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["processed"] is False
    assert duplicate.json()["reason"] == "payment session already paid"


def test_stripe_webhook_rejects_paid_event_missing_amount_without_grant(client, quote_payload, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_missing_amount", "https://checkout.stripe.test/session"))
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_missing_amount", "payment_session_id": ps["id"], "currency": quote_payload["currency"].lower()}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    response = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Stripe amount missing"
    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert unchanged["status"] == "checkout_created"


def test_stripe_webhook_rejects_paid_event_missing_currency_without_grant(client, quote_payload, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_missing_currency", "https://checkout.stripe.test/session"))
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_missing_currency", "payment_session_id": ps["id"], "amount_total": quote_payload["amount_minor"]}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    response = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Stripe currency missing"
    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert unchanged["status"] == "checkout_created"


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
    assert calls["idempotency_key"] == "ps_1"
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


def test_stripe_adapter_accepts_stripe_sdk_object_response_without_get_method():
    quote = Quote(
        id="quote_sdk_object",
        bot_id="bot-1",
        external_user_id="user-1",
        request_summary="do a thing",
        request_hash="hash-sdk-object",
        amount_minor=250,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 250}],
        quote_hash="qh-sdk-object",
    )
    payment_session = PaymentSession(id="ps_sdk_object", quote_id="quote_sdk_object", provider="stripe")

    class StripeLikeObject:
        def __init__(self):
            self._data = {"id": "cs_sdk_object", "url": "https://checkout.stripe.test/sdk-object"}

        def __getitem__(self, key):
            return self._data[key]

    class FakeStripeClient:
        def create_checkout_session(self, payload, idempotency_key):
            return StripeLikeObject()

    provider_session_id, url = create_stripe_checkout_session(
        quote,
        payment_session,
        Settings(stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example"),
        client=FakeStripeClient(),
    )

    assert provider_session_id == "cs_sdk_object"
    assert url == "https://checkout.stripe.test/sdk-object"


def test_stripe_checkout_provider_errors_are_mapped_to_safe_502():
    quote = Quote(
        id="quote_stripe_error",
        bot_id="bot-1",
        external_user_id="user-1",
        request_summary="do a paid thing",
        request_hash="hash-stripe-error",
        amount_minor=50,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 50}],
        quote_hash="qh-stripe-error",
    )
    payment_session = PaymentSession(id="ps_stripe_error", quote_id="quote_stripe_error", provider="stripe")

    class FakeStripeError(Exception):
        user_message = "No such price: sk_live_secret_should_not_be_here\nPlease check Stripe setup"
        code = "resource_missing"
        http_status = 400

    class FailingStripeClient:
        def create_checkout_session(self, payload, idempotency_key):
            raise FakeStripeError()

    try:
        create_stripe_checkout_session(
            quote,
            payment_session,
            Settings(stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example"),
            client=FailingStripeClient(),
        )
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "Stripe checkout session creation failed" in exc.detail
        assert "resource_missing" in exc.detail
        assert "\n" not in exc.detail
        assert "sk_live" not in exc.detail
    else:
        raise AssertionError("expected Stripe checkout failure to map to HTTPException")


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
    captured = {}

    def fake_stripe_checkout(quote, payment_session, settings):
        captured["request_hash"] = quote.request_hash
        captured["payment_session_id"] = payment_session.id
        captured["payment_session_idempotency_key"] = payment_session.idempotency_key
        return "cs_test_prompt", "https://checkout.stripe.test/prompt"

    monkeypatch.setattr(main_module, "create_stripe_checkout_session", fake_stripe_checkout)

    response = client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["payment_url"] == "https://checkout.stripe.test/prompt"
    assert data["payment_prompt"]["payment_url"] == "https://checkout.stripe.test/prompt"
    assert "https://checkout.stripe.test/prompt" in data["payment_prompt"]["message"]
    assert captured["request_hash"] == quote_payload["request_hash"]
    assert captured["payment_session_id"].startswith("ps_")
    assert captured["payment_session_idempotency_key"] is None


def test_production_mock_and_local_checkout_fail_closed(client, engine, quote_payload):
    settings = Settings(
        env="production",
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
        "production_persistent_database_configured": True,
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
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_map", "payment_status": "paid", "amount_total": quote_payload["amount_minor"], "currency": quote_payload["currency"].lower()}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert paid.status_code == 200
    assert paid.json() == {"received": True, "processed": True}
    stored = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert stored["id"] == ps["id"]
    assert stored["status"] == "paid"


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


def test_production_agent_action_mock_provider_fails_even_with_default_dev_mode(client, engine, quote_payload):
    settings = Settings(
        env="production",
        signing_secret="prod-signing-secret-for-test",
        checkout_provider="mock",
        public_base_url="https://payjent.example",
    )
    api_key = "prod-agent-action-mock-fails-key"
    with Session(engine) as session:
        create_bot_credential(session, "bot-1", api_key, settings.signing_secret)
    app.dependency_overrides[get_settings] = lambda: settings

    response = client.post("/api/v1/agent-actions", json=quote_payload, headers={"Authorization": f"Bearer {api_key}"})

    assert response.status_code == 503
    assert response.json()["detail"] == "active checkout provider not configured"


def test_production_readiness_default_sqlite_is_not_active_payment_ready(client):
    app.dependency_overrides[get_settings] = lambda: Settings(
        env="production",
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="whsec_test",
        public_base_url="https://payjent.example",
    )

    response = client.get("/api/v1/payment-readiness")

    assert response.status_code == 200
    data = response.json()
    assert data["database_configured"] is True
    assert data["production_persistent_database_configured"] is False
    assert data["active_payment_ready"] is False
    assert "sqlite:///" not in response.text


def test_stripe_webhook_rejects_non_stripe_session_without_issuing_grant(client, quote_payload, bot_headers):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=secret)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "payment_intent.succeeded", "data": {"object": {"metadata": {"payment_session_id": ps["id"]}, "amount_received": quote_payload["amount_minor"], "currency": quote_payload["currency"].lower()}}}, separators=(",", ":")).encode()

    response = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)})

    assert response.status_code == 409
    assert response.json()["detail"] == "payment session provider is not stripe"
    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert unchanged["status"] == "checkout_created"
    assert unchanged["receipt_id"] is None


def test_stripe_webhook_rejects_provider_session_amount_and_currency_mismatches(client, quote_payload, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret)
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_expected", "https://checkout.stripe.test/session"))
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()

    cases = [
        ({"id": "cs_test_wrong", "metadata": {"payment_session_id": ps["id"]}, "amount_total": quote_payload["amount_minor"], "currency": quote_payload["currency"].lower()}, "Stripe provider_session_id mismatch"),
        ({"id": "cs_test_expected", "metadata": {"payment_session_id": ps["id"]}, "amount_total": quote_payload["amount_minor"] + 1, "currency": quote_payload["currency"].lower()}, "Stripe amount mismatch"),
        ({"id": "cs_test_expected", "metadata": {"payment_session_id": ps["id"]}, "amount_total": quote_payload["amount_minor"], "currency": "eur"}, "Stripe currency mismatch"),
    ]
    for obj, detail in cases:
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": obj}}, separators=(",", ":")).encode()
        response = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)})
        assert response.status_code == 409
        assert response.json()["detail"] == detail

    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}").json()
    assert unchanged["status"] == "checkout_created"
    assert unchanged["receipt_id"] is None


def test_stripe_pay_page_requires_https_checkout_url(client, quote_payload, bot_headers, monkeypatch, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_test_http", "https://checkout.stripe.test/page"))
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    with Session(engine) as session:
        stored = session.get(PaymentSession, ps["id"])
        stored.checkout_url = "http://checkout.stripe.test/insecure"
        session.add(stored)
        session.commit()

    response = client.get(f"/pay/{ps['id']}")

    assert response.status_code == 200
    assert "Continue to secure payment" not in response.text
    assert "http://checkout.stripe.test/insecure" not in response.text
