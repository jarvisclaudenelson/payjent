import warnings

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from payjent import demo
from payjent.db import get_session
from payjent.main import app
from payjent.models import BotCredential


def test_demo_seed_creates_credentials_and_prints_exports(engine, capsys):
    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-bot", operator_id="demo-operator")

    assert credentials.bot_id == "demo-bot"
    assert credentials.bot_key.startswith("payjent_")
    assert credentials.operator_key.startswith("payjent_")

    with Session(engine) as session:
        rows = session.exec(select(BotCredential)).all()
    assert sorted(c.role for c in rows) == ["bot", "operator"]
    assert all(not c.key_hash.startswith("payjent_") for c in rows)

    demo.print_seed_exports(credentials)
    output = capsys.readouterr().out
    assert "export PAYJENT_DEMO_BOT_ID='demo-bot'" in output
    assert "export PAYJENT_BOT_KEY='" in output
    assert credentials.bot_key in output
    assert credentials.operator_key in output


def test_demo_run_flow_with_testclient(engine):
    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-bot")

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            result = demo.run_flow_with_client(
                client,
                bot_id=credentials.bot_id,
                bot_key=credentials.bot_key,
                operator_key=credentials.operator_key,
            )
    finally:
        app.dependency_overrides.clear()

    assert result["quote"]["status"] == "quoted"
    assert result["payment_session"]["status"] == "checkout_created"
    assert result["verified"]["valid"] is True
    assert result["verified"]["consumed"] is False
    assert result["consumed"]["consumed"] is True
    assert result["fulfillment"]["status"] == "fulfilled"


def test_demo_link_purchase_uses_fake_link_and_prints_boundary(engine, capsys):
    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-link-bot")

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            result = demo.run_link_purchase_with_client(
                client,
                bot_id=credentials.bot_id,
                bot_key=credentials.bot_key,
                operator_key=credentials.operator_key,
            )
    finally:
        app.dependency_overrides.clear()

    assert result["link_approval"]["approval_url"] == "https://link.example/approve/sr_payjent_demo_link_purchase"
    assert result["payment_session"]["status"] == "checkout_created"
    assert result["payment_session"]["receipt_id"] is None

    demo.print_link_purchase_summary(result)
    output = capsys.readouterr().out
    assert "approval_url=https://link.example/approve/sr_payjent_demo_link_purchase" in output
    assert "polling_hint=link-cli spend-request retrieve sr_payjent_demo_link_purchase --format json" in output
    assert "checkout_created/unpaid" in output
    assert "no receipt or grant is issued" in output


def test_demo_link_purchase_cli_command_returns_success_without_real_link(tmp_path):
    import os
    import subprocess
    import sys

    db_url = f"sqlite:///{tmp_path / 'demo-link.db'}"
    env = {**os.environ, "PAYJENT_DATABASE_URL": db_url, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "link-purchase"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "approval_url=https://link.example/approve/sr_payjent_demo_link_purchase" in completed.stdout
    assert "polling_hint=link-cli spend-request retrieve sr_payjent_demo_link_purchase --format json" in completed.stdout
    assert "checkout_created/unpaid" in completed.stdout
    assert "no receipt or grant is issued" in completed.stdout


def test_demo_link_purchase_cli_ignores_stale_default_sqlite_db(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    stale_db = tmp_path / "payjent.db"
    with sqlite3.connect(stale_db) as conn:
        conn.execute(
            "CREATE TABLE paymentsession ("
            "id VARCHAR NOT NULL PRIMARY KEY, "
            "quote_id VARCHAR NOT NULL, "
            "provider VARCHAR NOT NULL, "
            "status VARCHAR NOT NULL, "
            "checkout_url VARCHAR, "
            "idempotency_key VARCHAR NOT NULL, "
            "receipt_id VARCHAR, "
            "created_at DATETIME NOT NULL)"
        )

    env = {k: v for k, v in os.environ.items() if k != "PAYJENT_DATABASE_URL"}
    env["PYTHONPATH"] = os.getcwd()
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "link-purchase"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "approval_url=https://link.example/approve/sr_payjent_demo_link_purchase" in completed.stdout
    assert "checkout_created/unpaid" in completed.stdout

    with sqlite3.connect(stale_db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(paymentsession)")]
    assert "provider_session_id" not in columns


def test_demo_agent_prompt_with_testclient_uses_gate_and_stored_envelope(engine, capsys):
    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-agent-bot")

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            result = demo.run_agent_prompt_with_client(
                client,
                bot_id=credentials.bot_id,
                bot_key=credentials.bot_key,
                operator_key=credentials.operator_key,
            )
    finally:
        app.dependency_overrides.clear()

    assert result["unpaid_execute_blocked"] is True
    assert result["resume"]["execution_envelope"] == demo._agent_demo_envelope()
    assert result["tampered_fresh_prompt"] not in result["result_text"]
    assert "stored work only" in result["result_text"]
    assert result["fulfilled"].status == "fulfilled"

    demo.print_agent_prompt_summary(result)
    output = capsys.readouterr().out
    assert "PAYMENT_PROMPT:" in output
    assert "checkout_url=http://testserver/pay/" in output
    assert "pending_id=" in output
    assert "price=USD 7.00" in output
    assert "unpaid_execute_blocked=True" in output
    assert "grant_verified_and_consumed_before_fulfillment=True" in output
    assert "final_status=fulfilled" in output


def test_demo_agent_prompt_cli_ignores_stale_default_sqlite_db(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    stale_db = tmp_path / "payjent.db"
    with sqlite3.connect(stale_db) as conn:
        conn.execute("CREATE TABLE paymentsession (id VARCHAR NOT NULL PRIMARY KEY)")

    env = {k: v for k, v in os.environ.items() if k != "PAYJENT_DATABASE_URL"}
    env["PYTHONPATH"] = os.getcwd()
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "agent-prompt"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "PAYMENT_PROMPT:" in completed.stdout
    assert "checkout_url=http://testserver/pay/" in completed.stdout
    assert "unpaid_execute_blocked=True" in completed.stdout
    assert "final_status=fulfilled" in completed.stdout


def test_app_lifespan_has_no_fastapi_on_event_deprecation_warning(engine):
    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with TestClient(app):
                pass
    finally:
        app.dependency_overrides.clear()

    messages = [str(w.message) for w in caught]
    assert not any("on_event is deprecated" in message for message in messages)
