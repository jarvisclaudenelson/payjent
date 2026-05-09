import re


def test_demo_page_tells_agent_payment_story(client):
    response = client.get("/demo")
    assert response.status_code == 200
    text = response.text
    for copy in [
        "Hackathon demo",
        "Ask the agent",
        "runtime price",
        "Payjent approval",
        "Decal/task-budget readiness",
        "FAL returns artifact",
        "Returned artifact",
        "Register an agent",
    ]:
        assert copy in text
    assert "<pre" not in text
    assert "SDK" not in text


def test_hackathon_alias_renders_demo(client):
    response = client.get("/hackathon")
    assert response.status_code == 200
    assert "FAL image action" in response.text


def test_landing_is_agent_driven_not_sdk_first(client):
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    assert "Register your agent" in text
    assert "one-time install link" in text
    assert "Ask your agent" in text or "ask your agent" in text
    assert "/demo" in text
    assert "no copy-paste setup ceremony" in text
    assert "SDK" not in text
    assert "<pre" not in text
    assert "Integration snippet" not in text
    assert "curl -X" not in text


def test_dashboard_register_install_link_flow_and_agent_detail_agent_readable(client):
    registered = client.post("/auth/register", data={"email": "agentfirst@example.com", "password": "correct horse battery staple"}, follow_redirects=False)
    assert registered.status_code == 303

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Register agent and create install link" in dashboard.text
    assert "Ask the agent to verify Payjent capabilities" in dashboard.text
    assert "integration snippets" not in dashboard.text

    created = client.post(
        "/dashboard/agents/register",
        data={"name": "Hackathon Agent", "platform": "discord", "bot_id": "hackathon-agent", "default_currency": "USD"},
    )
    assert created.status_code == 200
    assert "One-time Agent Install Link" in created.text
    assert "agent-install/" in created.text
    assert "payjent_" not in created.text

    overview = client.get("/dashboard")
    agent_id = re.search(r"/dashboard/agents/(agent_[a-f0-9]+)", overview.text).group(1)
    detail = client.get(f"/dashboard/agents/{agent_id}")
    assert detail.status_code == 200
    assert "Agent-readable setup checklist" in detail.text
    assert "Generate one-time install link" in detail.text
    assert "Integration snippet" not in detail.text
    assert "curl -X" not in detail.text
    assert "&lt;operator-key&gt;" not in detail.text
    assert "Do not paste raw credentials" in detail.text


def test_dashboard_requires_auth_for_agent_first_pages(client):
    for path in ("/dashboard", "/dashboard/agents/agent_missing"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] in {"/auth/register", "/auth/login"}
