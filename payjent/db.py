from collections.abc import Generator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

POSTGRES_SCHEMES_WITHOUT_DRIVER = ("postgres://", "postgresql://")
POSTGRES_PSYCOPG_SCHEME = "postgresql+psycopg://"


def normalize_database_url(database_url: str) -> str:
    """Normalize common database URLs into SQLAlchemy/SQLModel URLs.

    Vercel/Supabase commonly expose pooled Postgres URLs with either the
    deprecated ``postgres://`` scheme or a driver-less ``postgresql://`` scheme.
    SQLAlchemy 2 works best here when the psycopg3 driver is explicit.
    """
    if database_url.startswith(POSTGRES_SCHEMES_WITHOUT_DRIVER):
        return POSTGRES_PSYCOPG_SCHEME + database_url.split("://", 1)[1]
    return database_url


def database_backend_from_url(database_url: str) -> str:
    """Return a non-secret backend label for status/health responses."""
    normalized_url = normalize_database_url(database_url)
    scheme = normalized_url.split("://", 1)[0].lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    if scheme.startswith("postgresql") or scheme.startswith("postgres"):
        return "postgresql"
    return scheme.split("+", 1)[0]


def make_engine(database_url: str | None = None):
    raw_url = database_url or get_settings().database_url
    url = normalize_database_url(raw_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def migrate_sqlite_account_auth_columns(db_engine: Engine) -> None:
    """Add WorkOS-era Account columns to pre-migration SQLite databases.

    SQLModel.create_all() intentionally does not alter existing tables. The
    demo/pre-live deployment path uses SQLite without Alembic, so keep this
    compatibility shim tightly scoped to the Account auth columns that were
    added for WorkOS.
    """
    if db_engine.dialect.name != "sqlite":
        return

    inspector = inspect(db_engine)
    if "account" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("account")}
    with db_engine.begin() as connection:
        if "auth_provider" not in columns:
            connection.execute(text("ALTER TABLE account ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'password'"))
        if "workos_user_id" not in columns:
            connection.execute(text("ALTER TABLE account ADD COLUMN workos_user_id TEXT"))


def account_password_hash_nullable(session: Session) -> bool:
    """Return whether the live SQLite Account table allows password_hash NULL."""
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return True
    for column in inspect(bind).get_columns("account"):
        if column["name"] == "password_hash":
            return bool(column["nullable"])
    return True


WORKOS_UNUSABLE_PASSWORD_HASH = "workos_unusable_password_hash"


engine = make_engine()


def _add_text_column_if_missing(db_engine: Engine, *, table_name: str, column_name: str) -> None:
    if db_engine.dialect.name not in {"sqlite", "postgresql"}:
        return
    inspector = inspect(db_engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in columns:
        with db_engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} TEXT"))


def migrate_quote_callback_column(db_engine: Engine) -> None:
    """Add callback_url to existing quote tables for pre-live deployments.

    Payjent is still pre-live and does not have Alembic migrations yet. Keep this
    additive compatibility shim narrow so existing SQLite/Postgres deployments
    can accept the new optional callback_url column after upgrade.
    """
    _add_text_column_if_missing(db_engine, table_name="quote", column_name="callback_url")


def migrate_payment_session_provider_columns(db_engine: Engine) -> None:
    """Add provider/idempotency columns to existing pre-live payment_session tables."""
    for column_name in ("provider_session_id", "idempotency_key", "receipt_id"):
        _add_text_column_if_missing(db_engine, table_name="paymentsession", column_name=column_name)


def migrate_grant_payment_session_column(db_engine: Engine) -> None:
    """Add payment_session_id to existing pre-live grant tables."""
    _add_text_column_if_missing(db_engine, table_name="grant", column_name="payment_session_id")


def migrate_spend_ledger_operation_id_column(db_engine: Engine) -> None:
    """Add operation_id to existing pre-live spend ledger tables.

    Spend authorization idempotency was added before Payjent has formal Alembic
    migrations. Existing SQLite/Postgres deployments may already have
    spendledgerentry rows without operation_id, so add the column and backfill it
    from the row id. SQLModel.create_all() will not alter existing tables.
    """
    _add_text_column_if_missing(db_engine, table_name="spendledgerentry", column_name="operation_id")
    if db_engine.dialect.name not in {"sqlite", "postgresql"}:
        return
    inspector = inspect(db_engine)
    if "spendledgerentry" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("spendledgerentry")}
    if "operation_id" not in columns:
        return
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE spendledgerentry SET operation_id = id WHERE operation_id IS NULL OR operation_id = ''"))


# Backwards-compatible alias for tests/imports that referenced the original SQLite-only shim.
def migrate_sqlite_quote_callback_column(db_engine: Engine) -> None:
    migrate_quote_callback_column(db_engine)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    migrate_sqlite_account_auth_columns(engine)
    migrate_quote_callback_column(engine)
    migrate_payment_session_provider_columns(engine)
    migrate_grant_payment_session_column(engine)
    migrate_spend_ledger_operation_id_column(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
