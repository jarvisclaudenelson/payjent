from payjent.db import database_backend_from_url, get_session, make_engine, normalize_database_url
from payjent.main import app


def test_normalize_postgres_url_uses_psycopg_driver():
    assert (
        normalize_database_url("postgres://user:pass@example.com:5432/db?sslmode=require")
        == "postgresql+psycopg://user:pass@example.com:5432/db?sslmode=require"
    )


def test_normalize_postgresql_url_uses_psycopg_driver():
    assert (
        normalize_database_url("postgresql://user:pass@example.com/db")
        == "postgresql+psycopg://user:pass@example.com/db"
    )


def test_normalize_preserves_explicit_driver_and_sqlite():
    assert normalize_database_url("postgresql+asyncpg://user:pass@example.com/db") == "postgresql+asyncpg://user:pass@example.com/db"
    assert normalize_database_url("sqlite:///./payjent.db") == "sqlite:///./payjent.db"


def test_database_backend_from_url_does_not_include_secrets():
    assert database_backend_from_url("postgres://user:secret@example.com/db") == "postgresql"
    assert database_backend_from_url("sqlite:///./payjent.db") == "sqlite"


def test_make_engine_keeps_sqlite_check_same_thread_behavior():
    engine = make_engine("sqlite://")
    assert engine.dialect.name == "sqlite"


def test_healthz_reports_sqlite_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": {"ok": True, "backend": "sqlite"}}


def test_healthz_returns_503_without_secret_leakage(client):
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class BrokenSession:
        def get_bind(self):
            return FakeBind()

        def exec(self, _statement):
            raise RuntimeError("could not connect to postgresql://user:secret@example.com/db")

    def broken_session():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken_session
    try:
        response = client.get("/healthz")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    payload = response.json()
    assert payload == {"status": "unhealthy", "database": {"ok": False, "backend": "postgresql"}}
    assert "secret" not in response.text
    assert "example.com" not in response.text
