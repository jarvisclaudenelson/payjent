from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

from payjent import workos_auth
from payjent.auth import DASHBOARD_SESSION_COOKIE
from payjent.config import Settings, get_settings
from payjent.db import WORKOS_UNUSABLE_PASSWORD_HASH, get_session, migrate_sqlite_account_auth_columns
from payjent.main import app
from payjent.models import Account


class FakeUserManagement:
    def __init__(self, email="WorkOS@Example.COM", user_id="user_123"):
        self.email = email
        self.user_id = user_id

    def get_authorization_url(self, provider, redirect_uri):
        assert provider == "authkit"
        return f"https://authkit.workos.test/authorize?provider={provider}&redirect_uri={redirect_uri}"

    def authenticate_with_code(self, code):
        assert code == "good_code"
        return {"user": {"email": self.email, "id": self.user_id}}


class FakeWorkOSClient:
    def __init__(self, email="WorkOS@Example.COM", user_id="user_123"):
        self.user_management = FakeUserManagement(email=email, user_id=user_id)


def workos_settings(**overrides):
    values = {
        "workos_api_key": "sk_test_not_a_real_secret",
        "workos_client_id": "client_123",
        "public_base_url": "https://payjent.example",
    }
    values.update(overrides)
    return Settings(**values)


def test_register_page_shows_workos_cta_only_when_configured(client):
    missing = client.get("/auth/register")
    assert missing.status_code == 200
    assert "Sign in with WorkOS AuthKit" not in missing.text
    assert "WorkOS AuthKit sign-in is not configured" in missing.text

    app.dependency_overrides[get_settings] = lambda: workos_settings()
    configured = client.get("/auth/register")
    assert configured.status_code == 200
    assert "Sign in with WorkOS AuthKit" in configured.text
    assert "/auth/workos/login" in configured.text


def test_workos_login_redirects_to_hosted_authkit_url(client, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: workos_settings(workos_redirect_uri="https://payjent.vercel.app/auth/workos/callback")
    monkeypatch.setattr(workos_auth, "create_workos_client", lambda settings: FakeWorkOSClient())

    response = client.get("/auth/workos/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://authkit.workos.test/authorize?provider=authkit&redirect_uri=https://payjent.vercel.app/auth/workos/callback"


def test_workos_login_missing_config_fails_closed_without_secrets(client):
    app.dependency_overrides[get_settings] = lambda: Settings(workos_api_key="sk_test_should_not_leak")

    response = client.get("/auth/workos/login")

    assert response.status_code == 503
    assert "WorkOS AuthKit is not configured" in response.text
    assert "sk_test_should_not_leak" not in response.text


def test_workos_callback_creates_account_sets_cookie_and_redirects(client, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: workos_settings()
    monkeypatch.setattr(workos_auth, "create_workos_client", lambda settings: FakeWorkOSClient())

    response = client.get("/auth/workos/callback?code=good_code", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    set_cookie = response.headers.get("set-cookie", "")
    assert DASHBOARD_SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    with Session(engine) as session:
        account = session.exec(select(Account).where(Account.email == "workos@example.com")).one()
        assert account.auth_provider == "workos"
        assert account.workos_user_id == "user_123"
        assert account.password_hash is None

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "workos@example.com" in dashboard.text


def test_workos_callback_links_existing_account(client, engine, monkeypatch):
    client.post("/auth/register", data={"email": "existing@example.com", "password": "correct-horse"})
    client.cookies.clear()
    app.dependency_overrides[get_settings] = lambda: workos_settings()
    monkeypatch.setattr(workos_auth, "create_workos_client", lambda settings: FakeWorkOSClient(email="existing@example.com", user_id="user_existing"))

    response = client.get("/auth/workos/callback?code=good_code", follow_redirects=False)

    assert response.status_code == 303
    with Session(engine) as session:
        accounts = session.exec(select(Account).where(Account.email == "existing@example.com")).all()
        assert len(accounts) == 1
        assert accounts[0].auth_provider == "workos"
        assert accounts[0].workos_user_id == "user_existing"
        assert accounts[0].password_hash.startswith("pbkdf2_sha256$")


def test_workos_callback_migrates_old_sqlite_account_schema(monkeypatch):
    old_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with old_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE account (
                id VARCHAR NOT NULL PRIMARY KEY,
                email VARCHAR NOT NULL UNIQUE,
                password_hash VARCHAR NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        connection.execute(
            text("INSERT INTO account (id, email, password_hash, created_at) VALUES (:id, :email, :password_hash, :created_at)"),
            {"id": "acct_old", "email": "old@example.com", "password_hash": "pbkdf2_sha256$old", "created_at": "2026-01-01 00:00:00"},
        )

    migrate_sqlite_account_auth_columns(old_engine)
    columns = {column["name"]: column for column in inspect(old_engine).get_columns("account")}
    assert columns["auth_provider"]["nullable"] is False
    assert "workos_user_id" in columns

    with Session(old_engine) as session:
        accounts = session.exec(select(Account)).all()
        assert len(accounts) == 1
        assert accounts[0].auth_provider == "password"

    def override_session():
        with Session(old_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: workos_settings()
    monkeypatch.setattr(workos_auth, "create_workos_client", lambda settings: FakeWorkOSClient(email="legacy-workos@example.com", user_id="user_legacy"))
    try:
        with TestClient(app) as legacy_client:
            response = legacy_client.get("/auth/workos/callback?code=good_code", follow_redirects=False)
            assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    with Session(old_engine) as session:
        account = session.exec(select(Account).where(Account.email == "legacy-workos@example.com")).one()
        assert account.auth_provider == "workos"
        assert account.workos_user_id == "user_legacy"
        assert account.password_hash == WORKOS_UNUSABLE_PASSWORD_HASH


def test_workos_callback_missing_or_failed_code_is_safe(client, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: workos_settings()
    missing = client.get("/auth/workos/callback")
    assert missing.status_code == 400
    assert "missing authorization code" in missing.text

    def fail_auth(_client, _code):
        raise RuntimeError("contains secret sk_test_should_not_leak")

    monkeypatch.setattr(workos_auth, "create_workos_client", lambda settings: FakeWorkOSClient())
    monkeypatch.setattr(workos_auth, "authenticate_with_code", fail_auth)
    failed = client.get("/auth/workos/callback?code=bad_code")
    assert failed.status_code == 401
    assert "WorkOS authentication failed" in failed.text
    assert "sk_test_should_not_leak" not in failed.text
