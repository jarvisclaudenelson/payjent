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
    names = {tool["name"] for tool in data["tools"]}
    assert "payjent.create_paid_action" in names
    assert "payjent.create_pay_sh_premium_action" in names
    assert "payjent.check_payment" in names
    assert "payjent.resume_paid_action" in names
    assert "payjent.complete_action" in names
    assert "payjent.list_capabilities" in names
    assert "paid-before-execute" in data["security_invariants"]
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
