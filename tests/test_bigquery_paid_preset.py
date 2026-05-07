import json

from payjent.config import Settings, get_settings
from payjent.main import app
import payjent.main as main_module


def _payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "project_id": "real-project-123",
        "query": "SELECT 1 AS ok",
        "use_legacy_sql": False,
        "request_summary": "Run exact paid BigQuery query through pay.sh/x402",
        "request_hash": "bigquery-hash",
        "amount_minor": 250,
        "currency": "USD",
        "cost_breakdown": [{"label": "pay.sh BigQuery quoted x402 cost", "amount_minor": 250}],
    }
    payload.update(overrides)
    return payload


def test_bigquery_paid_query_preset_is_discoverable_and_documented(client):
    manifest = client.get("/.well-known/payjent-tools.json")
    assert manifest.status_code == 200
    tools = {tool["name"]: tool for tool in manifest.json()["tools"]}
    preset = tools["payjent.create_bigquery_paid_query"]
    assert preset["endpoint"] == "/api/v1/premium-actions/pay-sh/bigquery-query"
    assert preset["preset"]["provider"] == "pay_sh"
    assert preset["preset"]["service_fqn"] == "solana-foundation/google/bigquery"
    assert preset["preset"]["resource"] == "jobs"
    assert preset["preset"]["gateway"] == "https://bigquery.google.gateway-402.com/bigquery/v2"
    assert preset["preset"]["path_template"] == "/projects/{project_id}/queries"
    assert preset["preset"]["execution_boundary"] == "agent_executes_after_spend_authorization"
    assert "Payjent does not execute BigQuery" in preset["description"]

    docs = client.get("/docs/agent-payjent-self-setup.md")
    assert docs.status_code == 200
    assert "payjent.create_bigquery_paid_query" in docs.text
    assert "https://bigquery.google.gateway-402.com/bigquery/v2" in docs.text
    assert "agent executes BigQuery through pay.sh externally" in docs.text


def test_bigquery_paid_query_route_creates_exact_pay_sh_action_without_payjent_execution(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret="whsec_test",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_bigquery", "https://checkout.stripe.test/bigquery"))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            raise AssertionError("Payjent must not execute the BigQuery pay.sh gateway")

    monkeypatch.setattr(main_module.httpx, "Client", FakeClient)

    response = client.post("/api/v1/premium-actions/pay-sh/bigquery-query", headers=bot_headers, json=_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "pay_sh"
    assert data["premium_provider"] == "pay_sh"
    assert data["payment_url"] == "https://checkout.stripe.test/bigquery"
    assert "https://bigquery.google.gateway-402.com/bigquery/v2/projects/real-project-123/queries" in data["command_preview"]
    assert calls == []

    quote = client.get(f"/api/v1/quotes/{data['quote_id']}").json()
    envelope = quote["execution_envelope"]
    assert envelope["provider"] == "pay_sh"
    assert envelope["service_fqn"] == "solana-foundation/google/bigquery"
    assert envelope["resource"] == "jobs"
    assert envelope["service_url"] == "https://bigquery.google.gateway-402.com/bigquery/v2/projects/real-project-123/queries"
    assert envelope["method"] == "POST"
    assert envelope["body"] == {"query": "SELECT 1 AS ok", "useLegacySql": False}
    assert envelope["headers"] == {"Content-Type": "application/json"}
    assert envelope["payjent_fulfillment_callback"] is False
    assert envelope["payjent_managed_execution"] is False
    assert envelope["payjent_execution_boundary"] == "agent_executes_after_spend_authorization"
    assert quote["amount_minor"] == 250
    assert quote["cost_breakdown"] == [{"label": "pay.sh BigQuery quoted x402 cost", "amount_minor": 250}]


def test_bigquery_paid_query_status_grant_spend_capture_flow(client, bot_headers, operator_headers):
    created = client.post("/api/v1/premium-actions/pay-sh/bigquery-query", headers=bot_headers, json=_payload(amount_minor=125, cost_breakdown=[{"label": "exact quote", "amount_minor": 125}]))
    assert created.status_code == 200
    action = created.json()

    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    assert paid.status_code == 200

    status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers)
    assert status.status_code == 200
    payment_token = status.json()["payment_token"]
    presentation = {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": "bigquery-hash"}

    consumed = client.post(f"/api/v1/agent-actions/{action['action_id']}/consume", headers=bot_headers, json={"payment_token": payment_token, "presentation": presentation})
    assert consumed.status_code == 200
    assert consumed.json()["execution_envelope"]["service_url"] == "https://bigquery.google.gateway-402.com/bigquery/v2/projects/real-project-123/queries"

    spend = client.post(
        f"/api/v1/grants/{payment_token}/spend-authorizations",
        headers=bot_headers,
        json={
            "operation_id": f"pay_sh:{action['action_id']}:bigquery-hash",
            "presentation": presentation,
            "tool": "payjent.create_bigquery_paid_query",
            "vendor": "pay.sh",
            "rail": "x402",
            "amount_minor": 125,
            "currency": "USD",
            "reason": "Run exact paid BigQuery query through pay.sh/x402",
            "provider_reference": "https://bigquery.google.gateway-402.com/bigquery/v2/projects/real-project-123/queries",
            "metadata": {"provider": "pay_sh", "service_fqn": "solana-foundation/google/bigquery", "agent_executes": True},
            "capture": True,
        },
    )
    assert spend.status_code == 200
    assert spend.json()["status"] == "captured"
    assert spend.json()["rail"] == "x402_payment"
    assert spend.json()["remaining_budget"] == 0

    complete = client.post(f"/api/v1/agent-actions/{action['action_id']}/complete", headers=bot_headers, json={"status": "fulfilled", "metadata": {"executed_by": "agent", "spend_id": spend.json()["id"]}})
    assert complete.status_code == 200
    assert complete.json()["status"] == "fulfilled"
