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


def test_demo_discord_aggregator_stripe_smoke_uses_fake_stripe_webhook(engine, capsys, monkeypatch):
    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-stripe-smoke-bot")

    def override_session():
        with Session(engine) as session:
            yield session

    def explode_if_live_stripe_is_called(*args, **kwargs):
        raise AssertionError("live Stripe SDK/network adapter must not be called by smoke demo")

    import payjent.providers.stripe as stripe_provider

    monkeypatch.setattr(stripe_provider.StripeSDKCheckoutClient, "create_checkout_session", explode_if_live_stripe_is_called)
    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            result = demo.run_discord_aggregator_stripe_smoke_with_client(
                client,
                bot_id=credentials.bot_id,
                bot_key=credentials.bot_key,
                operator_key=credentials.operator_key,
            )
    finally:
        app.dependency_overrides.clear()

    assert result["payment_session"]["provider"] == "stripe"
    assert result["payment_session"]["checkout_url"] == "https://checkout.stripe.test/discord-aggregator"
    assert result["payment_session"]["provider_session_id"] == "cs_test_discord_aggregator"
    assert result["stripe_webhook"]["processed"] is True
    assert result["stripe_test_webhook_simulated"] is True
    assert result["live_stripe_charge"] is False
    assert result["grant"]["id"].startswith("grant_")
    assert result["x402"]["spend"]["status"] == "captured"
    assert result["fulfilled"].status == "fulfilled"

    demo.print_discord_aggregator_stripe_smoke_summary(result)
    output = capsys.readouterr().out
    assert "checkout_url=https://checkout.stripe.test/discord-aggregator" in output
    assert "provider_session_id=cs_test_discord_aggregator" in output
    assert "stripe_test_webhook_simulated=True" in output
    assert "stripe_webhook_processed=True" in output
    assert "live_stripe_charge=False" in output
    assert "grant_consumed_before_x402_spend=True" in output
    assert "x402_spend_status=captured" in output
    assert "x402_captured=True" in output
    assert "final_status=fulfilled" in output


def test_demo_discord_aggregator_stripe_smoke_cli_returns_success_without_real_stripe(tmp_path):
    import os
    import subprocess
    import sys

    db_url = f"sqlite:///{tmp_path / 'demo-stripe-smoke.db'}"
    env = {**os.environ, "PAYJENT_DATABASE_URL": db_url, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "discord-aggregator-stripe-smoke"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "checkout_url=https://checkout.stripe.test/discord-aggregator" in completed.stdout
    assert "provider_session_id=cs_test_discord_aggregator" in completed.stdout
    assert "stripe_test_webhook_simulated=True" in completed.stdout
    assert "live_stripe_charge=False" in completed.stdout
    assert "grant_id=grant_" in completed.stdout
    assert "grant_consumed_before_x402_spend=True" in completed.stdout
    assert "x402_spend_status=captured" in completed.stdout
    assert "final_status=fulfilled" in completed.stdout


def test_demo_agent_owner_quickstart_cli_returns_success_and_redacts_tokens(tmp_path):
    import os
    import re
    import subprocess
    import sys

    db_url = f"sqlite:///{tmp_path / 'agent-owner-quickstart.db'}"
    env = {**os.environ, "PAYJENT_DATABASE_URL": db_url, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "agent-owner-quickstart"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Payjent agent owner quickstart smoke completed." in completed.stdout
    assert "create premium pay.sh action" in completed.stdout
    assert "unpaid_poll_payment_token=None" in completed.stdout
    assert "paid_poll_discovered_token=grant_..." in completed.stdout
    assert "resumed_provider=pay_sh" in completed.stdout
    assert "resumed_settlement=external_x402_runtime" in completed.stdout
    assert "external_pay_sh_execution=integrating_agent_runtime" in completed.stdout
    assert "fulfilled_status=fulfilled" in completed.stdout
    assert "Public users never paste grant ids/payment tokens" in completed.stdout
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", completed.stdout)


def test_demo_agent_webhook_resume_cli_returns_success_and_redacts_tokens(tmp_path):
    import os
    import re
    import subprocess
    import sys

    db_url = f"sqlite:///{tmp_path / 'agent-webhook-resume.db'}"
    env = {**os.environ, "PAYJENT_DATABASE_URL": db_url, "PYTHONPATH": os.getcwd()}
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "agent-webhook-resume"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Payjent agent webhook resume smoke completed." in completed.stdout
    assert "signed webhook delivered" in completed.stdout
    assert "callback_signature_verified=True" in completed.stdout
    assert "callback_contains_payment_token=False" in completed.stdout
    assert "callback_contains_grant=False" in completed.stdout
    assert "resumed_provider=pay_sh" in completed.stdout
    assert "fulfilled_status=fulfilled" in completed.stdout
    assert "Webhook payloads do not include grant ids/payment tokens" in completed.stdout
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", completed.stdout)


def test_demo_hosted_agent_webhook_smoke_in_process_redacts_tokens(engine, capsys):
    import re

    with Session(engine) as session:
        credentials = demo.seed_credentials(session=session, bot_id="demo-hosted-smoke-bot")

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            result = demo.run_hosted_agent_webhook_smoke_with_client(
                client,
                base_url="http://testserver",
                bot_id=credentials.bot_id,
                bot_key=credentials.bot_key,
                operator_key=credentials.operator_key,
                in_process_callback=True,
            )
    finally:
        app.dependency_overrides.clear()

    assert result["callback_mode"] == "in_process"
    assert result["callback_validation"] == "verified"
    assert result["callback"]["signature_verified"] is True
    assert result["unpaid_poll"]["payment_token"] is None
    assert result["paid_poll"]["payment_token"].startswith("grant_")
    assert result["resumed"]["execution_envelope"]["provider"] == "pay_sh"
    assert result["fulfilled"].status == "fulfilled"

    demo.print_hosted_agent_webhook_smoke_summary(result)
    output = capsys.readouterr().out
    assert "Payjent hosted agent-owner smoke completed." in output
    assert "base_url=http://testserver" in output
    assert "hosted_mode=False" in output
    assert "callback_mode=in_process" in output
    assert "callback_validation=verified" in output
    assert "callback_contains_payment_token=False" in output
    assert "callback_contains_grant=False" in output
    assert "payment_link_exists=True" in output
    assert "unpaid_poll_payment_token=None" in output
    assert "paid_poll_discovered_token=grant_..." in output
    assert "operator_mock_pay=test_rail_only" in output
    assert "resumed_provider=pay_sh" in output
    assert "fulfilled_status=fulfilled" in output
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", output)


def test_demo_hosted_agent_webhook_smoke_cli_safe_local_mode(tmp_path):
    import os
    import re
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "PAYJENT_DATABASE_URL"}
    env["PYTHONPATH"] = os.getcwd()
    completed = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "hosted-agent-webhook-smoke", "--in-process"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "base_url=http://testserver" in completed.stdout
    assert "hosted_mode=False" in completed.stdout
    assert "callback_mode=in_process" in completed.stdout
    assert "callback_validation=verified" in completed.stdout
    assert "operator_mock_pay=test_rail_only" in completed.stdout
    assert "paid_poll_discovered_token=grant_..." in completed.stdout
    assert "fulfilled_status=fulfilled" in completed.stdout
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", completed.stdout)


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
