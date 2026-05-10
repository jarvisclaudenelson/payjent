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


def test_existing_landing_design_remains_and_links_demo(client):
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    assert "Payment-gate agent actions" in text
    assert "The <em>payment gate</em> for agent work." in text
    assert "class='ribbon'" in text
    assert "class='stats'" in text
    assert "class='wedge-grid'" in text
    assert "class='receipt'" in text
    assert "class='final'" in text
    assert "Why Payjent" in text
    assert "Trust &amp; safety" in text
    assert "/demo" in text


def test_dashboard_requires_auth_for_existing_dashboard_pages(client):
    for path in ("/dashboard", "/dashboard/agents/agent_missing"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] in {"/auth/register", "/auth/login"}
