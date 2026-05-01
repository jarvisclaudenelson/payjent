from sqlmodel import Session, select

from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.models import Account


def test_unauthenticated_root_redirects_to_dashboard_then_auth(client):
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/dashboard"

    final = client.get("/", follow_redirects=True)
    assert final.status_code == 200
    assert "Create your Payjent account" in final.text


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
    agent = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "Prod Agent", "platform": "discord", "bot_id": "prod-bot", "default_currency": "USD"},
    ).json()["agent"]
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
        dashboard = client.get("https://testserver/dashboard")
        detail = client.get(f"https://testserver/dashboard/agents/{agent['id']}")
        assert dashboard.status_code == 200
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
