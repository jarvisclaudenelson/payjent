import payjent.main as main_module
from payjent.config import Settings, get_settings
from payjent.main import app


def _payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "Run exact paid premium x402 task",
        "request_hash": "x402-hash",
        "amount_minor": 175,
        "currency": "USD",
        "cost_breakdown": [{"label": "exact provider x402 quote", "amount_minor": 175}],
        "target_url": "https://example.com/premium/task",
        "method": "POST",
        "headers": {"Content-Type": "application/json", "X-Trace-Id": "trace-1"},
        "body": {"query": "premium-data"},
        "service_fqn": "example/provider",
        "resource": "task",
        "provider_metadata": {"catalog": "test", "quote_id": "provider-quote-1"},
    }
    payload.update(overrides)
    return payload


def test_discovery_manifest_includes_generic_x402_tool(client):
    manifest = client.get("/.well-known/payjent-tools.json")
    assert manifest.status_code == 200
    tools = {tool["name"]: tool for tool in manifest.json()["tools"]}
    generic = tools["payjent.create_x402_paid_action"]
    assert generic["endpoint"] == "/api/v1/premium-actions/x402"
    assert generic["execution_boundary"] == "agent_executes_after_spend_authorization"
    assert "Payjent never POSTs" in generic["description"]


def test_generic_x402_route_creates_arbitrary_envelope_without_executing_target(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret="whsec_test",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_x402", "https://checkout.stripe.test/x402"))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            raise AssertionError("Payjent must not execute the generic x402 target")

    monkeypatch.setattr(main_module.httpx, "Client", FakeClient)
    response = client.post("/api/v1/premium-actions/x402", headers=bot_headers, json=_payload())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["payment_url"] == "https://checkout.stripe.test/x402"
    assert data["request_fingerprint"] == "x402-hash"
    assert data["execution_boundary"] == "agent_executes_after_spend_authorization"
    assert "https://example.com/premium/task" in data["command_preview"]
    assert data["provider_metadata"] == {"catalog": "test", "quote_id": "provider-quote-1"}
    assert calls == []

    quote = client.get(f"/api/v1/quotes/{data['quote_id']}").json()
    envelope = quote["execution_envelope"]
    assert envelope["service_url"] == "https://example.com/premium/task"
    assert envelope["method"] == "POST"
    assert envelope["headers"] == {"Content-Type": "application/json", "X-Trace-Id": "trace-1"}
    assert envelope["body"] == {"query": "premium-data"}
    assert envelope["payjent_fulfillment_callback"] is False
    assert envelope["payjent_managed_execution"] is False
    assert envelope["payjent_execution_boundary"] == "agent_executes_after_spend_authorization"


def test_generic_x402_rejects_secret_headers(client, bot_headers):
    response = client.post("/api/v1/premium-actions/x402", headers=bot_headers, json=_payload(headers={"Authorization": "Bearer nope"}))
    assert response.status_code == 422
    assert "secret-like outbound header" in response.text


def test_stripe_checkout_rejects_usd_amount_below_card_minimum_before_stripe_call(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret="whsec_test",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: (_ for _ in ()).throw(AssertionError("Stripe should not be called for below-minimum checkout")))

    response = client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json=_payload(amount_minor=1, cost_breakdown=[{"label": "exact provider quote", "amount_minor": 1}]),
    )

    assert response.status_code == 422
    assert "Stripe checkout minimum for USD is 50 minor units" in response.text


def test_generic_x402_full_create_pay_consume_spend_complete_flow(client, bot_headers, operator_headers):
    created = client.post("/api/v1/premium-actions/x402", headers=bot_headers, json=_payload(amount_minor=125, cost_breakdown=[{"label": "exact quote", "amount_minor": 125}]))
    assert created.status_code == 200, created.text
    action = created.json()

    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert paid.status_code == 200

    status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    payment_token = status.json()["payment_token"]
    presentation = {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": "x402-hash"}

    consumed = client.post(f"/api/v1/agent-actions/{action['action_id']}/consume", headers=bot_headers, json={"payment_token": payment_token, "presentation": presentation})
    assert consumed.status_code == 200
    assert consumed.json()["execution_envelope"]["service_url"] == "https://example.com/premium/task"

    spend = client.post(
        f"/api/v1/grants/{payment_token}/spend-authorizations",
        headers=bot_headers,
        json={
            "operation_id": f"x402:{action['action_id']}:x402-hash",
            "presentation": presentation,
            "tool": "payjent.create_x402_paid_action",
            "vendor": "pay.sh",
            "rail": "x402",
            "amount_minor": 125,
            "currency": "USD",
            "reason": "Run exact paid premium x402 task",
            "provider_reference": "https://example.com/premium/task",
            "metadata": {"provider": "pay_sh", "agent_external_execution": True},
            "capture": True,
        },
    )
    assert spend.status_code == 200, spend.text
    assert spend.json()["status"] == "captured"
    assert spend.json()["rail"] == "x402_payment"
    assert spend.json()["remaining_budget"] == 0

    # Simulate agent-side external execution by recording provider receipt/job metadata, not by Payjent calling the target.
    complete = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/complete",
        headers=bot_headers,
        json={"status": "fulfilled", "metadata": {"executed_by": "agent", "provider_receipt": "rcpt_test_1", "provider_job_id": "job_test_1", "spend_id": spend.json()["id"]}},
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "fulfilled"
    assert complete.json()["metadata"]["provider_job_id"] == "job_test_1"
