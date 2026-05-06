import json

from payjent.config import Settings, get_settings
from payjent.main import app
import payjent.main as main_module


def _purchase_payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "Buy exact quoted Amazon item via procurement executor",
        "request_hash": "purchase-hash",
        "amount_minor": 4299,
        "currency": "USD",
        "cost_breakdown": [{"label": "Amazon quoted total", "amount_minor": 4299}],
        "merchant": {"name": "Amazon", "domain": "amazon.com"},
        "item": {"summary": "Exact item title quantity 1", "url": "https://www.amazon.com/dp/EXAMPLE"},
        "order_summary": "Buy quantity 1 at exact merchant quote",
        "service_url": "https://executor.example/purchase",
        "method": "POST",
        "body": {"merchant": "amazon.com", "item_url": "https://www.amazon.com/dp/EXAMPLE", "quantity": 1, "quoted_total_minor": 4299, "currency": "USD"},
        "headers": {"Content-Type": "application/json"},
        "payjent_fulfillment_callback": True,
    }
    payload.update(overrides)
    return payload


def _stripe_settings(allowed="executor.example"):
    return Settings(checkout_provider="stripe", stripe_secret_key="sk_test_fake", public_base_url="https://payjent.example", stripe_webhook_secret="whsec_test", managed_execution_allowed_hosts=allowed)


def test_purchase_action_requires_exact_quote_breakdown(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: _stripe_settings()
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))
    payload = _purchase_payload(cost_breakdown=[{"label": "wrong", "amount_minor": 4200}])
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=payload)
    assert response.status_code == 422
    assert "sum" in response.json()["detail"].lower() or "breakdown" in response.json()["detail"].lower()


def test_purchase_action_requires_fulfillment_callback_and_service_url(client, bot_headers):
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload(payjent_fulfillment_callback=False))
    assert response.status_code == 422
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload(service_url=""))
    assert response.status_code == 422


def test_purchase_action_rejects_credential_like_body_keys(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: _stripe_settings()
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload(body={"amazon_password": "nope"}))
    assert response.status_code == 422
    assert "credential" in response.json()["detail"].lower()


def test_purchase_action_rejects_unallowlisted_host_before_stripe_checkout(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: _stripe_settings(allowed="other.example")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload())
    assert response.status_code == 422
    assert "not allowed" in response.json()["detail"]


def test_purchase_action_requires_configured_procurement_allowlist_before_stripe_checkout(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: _stripe_settings(allowed="")
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("checkout should not be created")))
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload())
    assert response.status_code == 422
    assert "procurement executor" in response.json()["detail"]


def test_purchase_action_allowlisted_host_creates_checkout_with_truthful_metadata(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: _stripe_settings()
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_purchase", "https://checkout.stripe.test/purchase"))
    response = client.post("/api/v1/purchase-actions", headers=bot_headers, json=_purchase_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["payment_url"] == "https://checkout.stripe.test/purchase"
    quote = client.get(f"/api/v1/quotes/{data['quote_id']}").json()
    text = json.dumps(quote).lower()
    assert "merchant_purchase" in text
    assert "does not send funds to the agent" in text
    assert "directly pay amazon" in text


def test_purchase_tool_discovery_and_docs_truthful_money_flow(client):
    manifest = client.get("/.well-known/payjent-tools.json").json()
    text = json.dumps(manifest).lower()
    assert "payjent.create_purchase_fulfillment" in text
    assert "verified post fulfillment callback" in text or "verified post" in text
    assert "does not send funds to the agent" in text
    assert "does not directly pay amazon" in text
    docs = client.get("/docs/agent-payjent-self-setup.md")
    assert docs.status_code == 200
    docs_text = docs.text.lower()
    assert "merchant purchase / procurement handoff" in docs_text
    assert "payjent does **not** send funds to the agent" in docs_text
    assert "fail closed if there is no procurement executor" in docs_text
