import re

from sqlmodel import Session, select

from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.models import Account


def test_public_root_renders_landing_page(client):
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 200
    assert "When your agent needs to spend" in root.text
    assert "Register an agent" in root.text
    assert "one-time install link" in root.text
    assert "Ask your agent" in root.text or "ask your agent" in root.text
    assert "/demo" in root.text
    assert "SDK" not in root.text
    assert "<pre" not in root.text
    assert "curl -X" not in root.text
    assert "payjent.dev" not in root.text
    assert "grant_" not in root.text
    assert "payment_token" not in root.text
    assert "<pre" not in root.text
    assert "<code" not in root.text
    assert "code-block" not in root.text


def test_unauthenticated_dashboard_redirects_without_metadata(client, operator_headers):
    agent = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "Hermes Research", "platform": "discord", "bot_id": "hermes-bot", "default_currency": "USD"},
    ).json()["agent"]
    for path in ("/dashboard", f"/dashboard/agents/{agent['id']}"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] in {"/auth/register", "/auth/login"}
        assert "Hermes Research" not in response.text
        assert "hermes-bot" not in response.text


def test_register_creates_account_hash_sets_cookie_and_accesses_dashboard(client, engine):
    response = client.post(
        "/auth/register",
        data={"email": "Dev@Example.COM", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "payjent_dashboard_session" in response.headers.get("set-cookie", "")
    assert "HttpOnly" in response.headers.get("set-cookie", "")

    with Session(engine) as session:
        account = session.exec(select(Account).where(Account.email == "dev@example.com")).one()
        assert account.password_hash.startswith("pbkdf2_sha256$")
        assert "correct horse" not in account.password_hash

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Signed in as" in dashboard.text
    assert "dev@example.com" in dashboard.text


def test_login_succeeds_and_fails_appropriately(client):
    client.post("/auth/register", data={"email": "login@example.com", "password": "goodpassword"})
    client.cookies.clear()

    bad = client.post("/auth/login", data={"email": "login@example.com", "password": "badpassword"})
    assert bad.status_code == 200
    assert "Invalid email or password" in bad.text
    assert "badpassword" not in bad.text

    good = client.post(
        "/auth/login",
        data={"email": "LOGIN@example.com", "password": "goodpassword"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/dashboard"
    assert "payjent_dashboard_session" in good.headers.get("set-cookie", "")


def test_logout_clears_session(client):
    client.post("/auth/register", data={"email": "logout@example.com", "password": "goodpassword"})
    out = client.post("/auth/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/auth/login"
    assert "payjent_dashboard_session" in out.headers.get("set-cookie", "")
    dashboard = client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code == 303


def test_production_dashboard_redirects_without_session_and_renders_with_session(client, operator_headers):
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, public_base_url="https://payjent.example")
    try:
        no_session = client.get("/dashboard", follow_redirects=False)
        assert no_session.status_code == 303
        assert no_session.headers["location"] == "/auth/register"
        assert "Prod Agent" not in no_session.text

        registered = client.post(
            "/auth/register",
            data={"email": "prod@example.com", "password": "production-pass"},
            follow_redirects=False,
        )
        assert registered.status_code == 303
        assert "Secure" in registered.headers.get("set-cookie", "")
        created = client.post(
            "https://testserver/dashboard/agents/register",
            data={"name": "Prod Agent", "platform": "discord", "bot_id": "prod-bot", "default_currency": "USD"},
        )
        assert created.status_code == 200
        dashboard = client.get("https://testserver/dashboard")
        assert dashboard.status_code == 200
        detail_link = re.search(r"/dashboard/agents/(agent_[a-f0-9]+)", dashboard.text)
        assert detail_link is not None
        detail = client.get(f"https://testserver{detail_link.group(0)}")
        assert detail.status_code == 200
        assert "Prod Agent" in dashboard.text
        assert "prod-bot" in detail.text
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_operator_api_auth_behavior_unchanged(client):
    payload = {"name": "X", "platform": "discord", "bot_id": "x"}
    missing = client.post("/api/v1/agents/register", json=payload)
    assert missing.status_code == 401
    bot_only = client.post("/api/v1/agents/register", headers={"Authorization": "Bearer test-bot-key"}, json=payload)
    assert bot_only.status_code == 403


def test_authenticated_dashboard_has_agent_credential_form_not_operator_curl(client):
    client.post("/auth/register", data={"email": "dashform@example.com", "password": "password123"})
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Register agent and create install link" in response.text
    assert "/dashboard/agents/register" in response.text
    assert "generate install link" in response.text
    assert "Agent Install Link" in response.text
    assert "/docs/agent-payjent-self-setup.md" in response.text
    assert "Agent-owner quickstart" in response.text
    assert "Policy controls MVP" in response.text
    assert "Paid-action lifecycle ledger" in response.text
    assert "assess_checkout_risk" in response.text
    assert "&lt;operator-key&gt;" not in response.text


def test_dashboard_form_creates_agent_and_shows_install_link_without_raw_key(client):
    client.post("/auth/register", data={"email": "dashcreate@example.com", "password": "password123"})
    created = client.post(
        "/dashboard/agents/register",
        data={"name": "Dashboard Agent", "platform": "discord", "bot_id": "dashboard-bot", "default_currency": "USD"},
    )
    assert created.status_code == 200
    assert "One-time Agent Install Link" in created.text
    assert "Primary safe setup" in created.text
    assert "payjent_" not in created.text
    assert "Copy this Payjent agent credential now" not in created.text

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Dashboard Agent" in dashboard.text
    detail_link = re.search(r"/dashboard/agents/(agent_[a-f0-9]+)", dashboard.text)
    assert detail_link is not None
    detail = client.get(detail_link.group(0))
    assert detail.status_code == 200
    assert "Dashboard Agent" in detail.text
    assert "Generate one-time install link" in detail.text
    assert "Create manual recovery credential" in detail.text
    assert "Unsafe manual/admin recovery fallback" in detail.text


def test_repeated_dashboard_registration_does_not_leak_raw_credential(client):
    client.post("/auth/register", data={"email": "repeat@example.com", "password": "password123"})
    first = client.post(
        "/dashboard/agents/register",
        data={"name": "Repeat Agent", "platform": "discord", "bot_id": "repeat-bot", "default_currency": "USD"},
    )
    assert first.status_code == 200
    assert re.search(r"payjent_[A-Za-z0-9_\-]+", first.text) is None
    second = client.post(
        "/dashboard/agents/register",
        data={"name": "Repeat Agent", "platform": "discord", "bot_id": "repeat-bot", "default_currency": "USD"},
    )
    assert second.status_code == 200
    assert "Agent already registered" in second.text
    assert "One-time Agent Install Link" in second.text
    assert re.search(r"payjent_[A-Za-z0-9_\-]+", second.text) is None


def test_unauthenticated_dashboard_agent_form_post_redirects(client):
    response = client.post(
        "/dashboard/agents/register",
        data={"name": "No Session", "platform": "discord", "bot_id": "no-session", "default_currency": "USD"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] in {"/auth/register", "/auth/login"}
