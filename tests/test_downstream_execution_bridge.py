import json

from payjent.config import Settings, get_settings
from payjent.main import app
import payjent.main as main_module


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    import hashlib, hmac
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _create_action(client, bot_headers, service_url="https://downstream.example/run", headers=None, body=None, method="POST"):
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
            "method": method,
            "body": body or {"task": "run"},
            "headers": headers or {"Accept": "application/json", "Authorization": "Bearer leak", "X-Api-Key": "leak", "Cookie": "leak"},
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
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret, managed_execution_allowed_hosts="downstream.example")
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
    assert paid.json() == {"received": True, "processed": True}
    assert "grant" not in paid.text.lower()
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


def test_unsafe_service_url_rejected_before_checkout(client, bot_headers, monkeypatch):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret=secret, managed_execution_allowed_hosts="downstream.example")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))

    response = client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1", "external_user_id": "user-1", "request_summary": "run", "request_hash": "bad-url",
            "amount_minor": 250, "currency": "USD", "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": "http://localhost/run", "method": "POST", "body": {"task": "run"}, "payjent_managed_execution": True,
        },
    )

    assert response.status_code == 422
    assert "https" in response.json()["detail"]


def test_production_requires_managed_execution_allowlist(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))

    response = client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1", "external_user_id": "user-1", "request_summary": "run", "request_hash": "prod-no-allow",
            "amount_minor": 250, "currency": "USD", "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": "https://downstream.example/run", "method": "POST", "body": {"task": "run"}, "payjent_managed_execution": True,
        },
    )

    assert response.status_code == 422
    assert "ALLOWED_HOSTS" in response.json()["detail"]


def test_production_allowed_managed_execution_host_creates_checkout(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test", managed_execution_allowed_hosts="downstream.example")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_allowed", "https://checkout.stripe.test/session"))

    action = _create_action(client, bot_headers)

    assert action["payment_session_id"].startswith("ps_")
    assert action["payment_url"] == "https://checkout.stripe.test/session"


def test_generic_quote_checkout_rejects_unallowlisted_managed_execution_before_stripe(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))
    quote = client.post(
        "/api/v1/quotes",
        headers=bot_headers,
        json={
            "bot_id": "bot-1",
            "external_user_id": "user-1",
            "request_summary": "generic downstream action",
            "request_hash": "generic-prod-no-allow",
            "amount_minor": 250,
            "currency": "USD",
            "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "execution_envelope": {
                "service_url": "https://downstream.example/run",
                "method": "POST",
                "body": {"task": "run"},
                "payjent_managed_execution": True,
            },
        },
    )
    assert quote.status_code == 200

    response = client.post(f"/api/v1/quotes/{quote.json()['id']}/checkout", headers=bot_headers)

    assert response.status_code == 422
    assert "ALLOWED_HOSTS" in response.json()["detail"]


def test_generic_quote_checkout_allows_allowlisted_managed_execution(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test", managed_execution_allowed_hosts="downstream.example")
    calls = []

    def fake_create(*args):
        calls.append(args)
        return "cs_generic_allowed", "https://checkout.stripe.test/session"

    monkeypatch.setattr(main_module, "create_stripe_checkout_session", fake_create)
    quote = client.post(
        "/api/v1/quotes",
        headers=bot_headers,
        json={
            "bot_id": "bot-1",
            "external_user_id": "user-1",
            "request_summary": "generic downstream action",
            "request_hash": "generic-prod-allowed",
            "amount_minor": 250,
            "currency": "USD",
            "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "execution_envelope": {
                "service_url": "https://downstream.example/run",
                "method": "POST",
                "body": {"task": "run"},
                "payjent_managed_execution": True,
            },
        },
    )
    assert quote.status_code == 200

    response = client.post(f"/api/v1/quotes/{quote.json()['id']}/checkout", headers=bot_headers)

    assert response.status_code == 200
    assert response.json()["checkout_url"] == "https://checkout.stripe.test/session"
    assert len(calls) == 1


def test_nested_reserved_body_token_rejected_before_checkout(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test", managed_execution_allowed_hosts="downstream.example")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))

    response = client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1", "external_user_id": "user-1", "request_summary": "run", "request_hash": "nested-token",
            "amount_minor": 250, "currency": "USD", "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": "https://downstream.example/run", "method": "POST", "body": {"task": {"token": "leak"}}, "payjent_managed_execution": True,
        },
    )

    assert response.status_code == 422
    assert "body.task.token" in response.json()["detail"]
