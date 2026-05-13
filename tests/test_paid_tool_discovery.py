import json
import re


def _no_secret_words(payload):
    text = json.dumps(payload).lower()
    for forbidden in ("test-bot-key", "test-operator-key", "payment_token", "credential\":", "key_hash"):
        assert forbidden not in text
    assert not re.search(r"grant_[a-f0-9]{8,}", text)


def test_public_manifest_returns_safe_tool_discovery(client):
    response = client.get("/.well-known/payjent-tools.json")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Payjent"
    assert data["docs_url"].endswith("/docs/agent-payjent-self-setup.md")
    assert data["authenticated_capabilities_url"].endswith("/api/v1/agent-capabilities")
    assert data["auth"]["header"] == "X-Payjent-Bot-Key"
    assert data["premium_action_presets_url"].endswith("/api/v1/premium-action-presets")
    assert data["premium_action_preset_count"] >= 6
    premium = data["premium_tool_discovery"]
    assert premium["premium_action_preset_count"] == data["premium_action_preset_count"]
    assert "manifest -> authenticated capabilities -> presets -> exact provider quote" in premium["premium_tool_quickstart"][0]
    assert "does not execute provider API calls" in premium["execution_boundary"]
    assert "agent-side private credential only" in premium["provider_api_credential_policy"]
    assert "exa.deep_search" in json.dumps(premium["recommended_premium_paths"])
    assert premium["creation_template"]["endpoint"].endswith("/api/v1/premium-action-presets/{preset_id}/actions")
    assert "input" in premium["creation_template"]["required_fields"]
    names = {tool["name"] for tool in data["tools"]}
    assert "payjent.create_paid_action" in names
    assert "payjent.create_pay_sh_premium_action" in names
    assert "payjent.check_payment" in names
    assert "payjent.resume_paid_action" in names
    assert "payjent.complete_action" in names
    assert "payjent.list_capabilities" in names
    assert any("paid-before-execute" == item for item in data["security_invariants"])
    assert any("no hidden/default fees" in item for item in data["security_invariants"])
    assert data["pricing_policy"]["rule"] == "exact_provider_quote_required"
    assert data["public_base_url"] == "https://payjent.com"
    manifest_text = json.dumps(data).lower()
    assert "task budget" in manifest_text
    assert "execution readiness" in manifest_text
    assert "auto-resume" in manifest_text
    assert "refund by default" in manifest_text
    assert "dashboard/platform connections" in manifest_text
    assert "exact provider/merchant quoted price" in manifest_text
    assert "do not use placeholder" in manifest_text
    assert "operator fees must be explicit" in manifest_text or "explicit cost_breakdown line items" in manifest_text
    assert "no default or hidden fees" in manifest_text or "no hidden/default fees" in manifest_text
    create_tool = next(tool for tool in data["tools"] if tool["name"] == "payjent.create_paid_action")
    pay_sh_tool = next(tool for tool in data["tools"] if tool["name"] == "payjent.create_pay_sh_premium_action")
    assert create_tool["pricing_policy"]["rule"] == "exact_provider_quote_required"
    assert create_tool["amount_requirements"]["fail_closed_if_unknown"] is True
    assert "payjent_fulfillment_callback" in pay_sh_tool["description"]
    assert "legacy alias" in pay_sh_tool["description"]
    assert "managed execution" not in pay_sh_tool["description"].lower()
    assert "performs the premium action" not in manifest_text
    _no_secret_words(data)


def test_agent_capabilities_requires_bot_auth(client):
    response = client.get("/api/v1/agent-capabilities")
    assert response.status_code in {401, 403}


def test_agent_capabilities_returns_current_agent_without_secrets(client, operator_headers, bot_headers):
    agent = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "Discovery Bot", "platform": "discord", "bot_id": "bot-1", "default_currency": "USD"},
    ).json()["agent"]
    response = client.get("/api/v1/agent-capabilities", headers=bot_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["agent"]["id"] == agent["id"]
    assert data["agent"]["bot_id"] == "bot-1"
    assert data["enabled_rails"] == []
    tools = {tool["name"]: tool for tool in data["tools"]}
    assert tools["payjent.create_paid_action"]["available"] is True
    assert tools["payjent.create_pay_sh_premium_action"]["available"] is True
    assert tools["payjent.authorize_x402_spend"]["available"] is False
    assert data["docs_url"].endswith("/docs/agent-payjent-self-setup.md")
    assert data["dashboard_url"].endswith(f"/dashboard/agents/{agent['id']}")
    premium = data["premium_tool_discovery"]
    assert premium["premium_action_presets_url"].startswith("http://testserver/api/v1/premium-action-presets")
    assert premium["installed_agent_readiness"]["payjent_credential_present"] is True
    assert premium["installed_agent_readiness"]["premium_presets_available"] is True
    assert premium["installed_agent_readiness"]["x402_spend_authorization_available"] is False
    assert "Payjent stores safe payment-gated envelopes" in premium["execution_boundary"]
    assert "firecrawl.scrape" in premium["creation_template"]["input_fields_by_preset"]
    capabilities_text = json.dumps(data).lower()
    assert "exact provider/merchant quoted price" in capabilities_text
    assert "do not use placeholder" in capabilities_text
    assert tools["payjent.create_paid_action"]["pricing_policy"]["unknown_price_behavior"] == "fail_closed_await_exact_provider_quote"
    assert tools["payjent.create_pay_sh_premium_action"]["amount_requirements"]["cost_breakdown"] == "required; must match amount_minor; operator fees must be separately labeled"
    _no_secret_words(data)


def test_agent_capabilities_show_x402_rail_and_caps(client, operator_headers, bot_headers):
    agent = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "x402 Bot", "platform": "cli", "bot_id": "bot-1", "default_currency": "USD"},
    ).json()["agent"]
    configured = client.post(
        f"/api/v1/agents/{agent['id']}/x402/configure",
        headers=operator_headers,
        json={"network": "base-sepolia", "pay_to": "0xTEST", "max_per_request_minor": 900, "max_per_call_minor": 250, "enabled": True},
    )
    assert configured.status_code == 200
    response = client.get("/api/v1/agent-capabilities", headers=bot_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["enabled_rails"][0]["rail"] == "x402"
    assert data["enabled_rails"][0]["status"] == "enabled"
    assert data["limits"]["x402"]["max_per_request_minor"] == 900
    assert data["limits"]["x402"]["max_per_call_minor"] == 250
    tools = {tool["name"]: tool for tool in data["tools"]}
    assert tools["payjent.authorize_x402_spend"]["available"] is True
    _no_secret_words(data)


def test_dashboard_agent_detail_includes_paid_tool_discovery(client):
    client.post("/auth/register", data={"email": "owner@example.com", "password": "correct-horse-123"})
    created = client.post(
        "/dashboard/agents/register",
        data={"name": "Dashboard Bot", "platform": "discord", "bot_id": "dash-bot", "default_currency": "USD"},
        follow_redirects=False,
    )
    assert created.status_code == 200
    match = re.search(r"/dashboard/agents/(agent_[a-f0-9]+)", created.text)
    assert match
    detail = client.get(f"/dashboard/agents/{match.group(1)}")
    assert detail.status_code == 200
    assert "Paid tool discovery" in detail.text
    assert "/.well-known/payjent-tools.json" in detail.text
    assert "/api/v1/agent-capabilities" in detail.text


def test_agent_setup_docs_mention_discovery_endpoints(client):
    response = client.get("/docs/agent-payjent-self-setup.md")
    assert response.status_code == 200
    assert "/.well-known/payjent-tools.json" in response.text
    assert "/api/v1/agent-capabilities" in response.text
    assert "https://payjent.com" in response.text
    assert "task budget" in response.text.lower()
    assert "spend control" in response.text.lower()
    assert "dashboard/platform connections" in response.text.lower()
    assert "do not ask" in response.text.lower()
    assert "PAYJENT_SIGNING_SECRET" not in response.text
