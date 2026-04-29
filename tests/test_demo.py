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
