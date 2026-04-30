from sqlmodel import Session, select

from payjent.auth import hash_api_key
from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.models import AgentProfile, BotCredential, RailConnection


def _register(client, operator_headers):
    r = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "Hermes Research", "platform": "discord", "bot_id": "hermes-bot", "default_currency": "usd"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_register_agent_creates_profile_and_one_time_hashed_bot_key(client, operator_headers, engine):
    data = _register(client, operator_headers)
    assert data["agent"]["bot_id"] == "hermes-bot"
    assert data["agent"]["default_currency"] == "USD"
    bot_key = data["bot_api_key"]
    assert bot_key.startswith("payjent_")

    again = client.post(
        "/api/v1/agents/register",
        headers=operator_headers,
        json={"name": "Hermes Research", "platform": "discord", "bot_id": "hermes-bot", "default_currency": "USD"},
    )
    assert again.status_code == 200
    assert again.json()["bot_api_key"] is None

    with Session(engine) as session:
        agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == "hermes-bot")).one()
        cred = session.exec(select(BotCredential).where(BotCredential.bot_id == "hermes-bot")).one()
        assert agent.name == "Hermes Research"
        assert cred.key_hash == hash_api_key(bot_key, Settings().signing_secret)
        assert bot_key not in cred.key_hash


def test_stripe_connect_start_complete_local_no_network(client, operator_headers, engine, monkeypatch):
    monkeypatch.setattr("payjent.main.create_stripe_checkout_session", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network adapter called")))
    agent = _register(client, operator_headers)["agent"]

    started = client.post(f"/api/v1/agents/{agent['id']}/stripe-connect/start", headers=operator_headers)
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["mode"] == "local"
    assert body["account_id"].startswith("acct_test_")
    assert body["status"] == "onboarding_started"

    completed = client.post(f"/api/v1/agents/{agent['id']}/stripe-connect/complete", headers=operator_headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "connected"

    with Session(engine) as session:
        rail = session.exec(select(RailConnection).where(RailConnection.agent_id == agent["id"], RailConnection.rail == "stripe_connect")).one()
        assert rail.config_json["account_id"].startswith("acct_test_")


def test_x402_config_validation_and_persistence(client, operator_headers, engine):
    agent = _register(client, operator_headers)["agent"]
    bad = client.post(
        f"/api/v1/agents/{agent['id']}/x402/configure",
        headers=operator_headers,
        json={"network": "base-sepolia", "max_per_request_minor": 100, "max_per_call_minor": 101, "enabled": True},
    )
    assert bad.status_code == 422

    good = client.post(
        f"/api/v1/agents/{agent['id']}/x402/configure",
        headers=operator_headers,
        json={
            "network": "base-sepolia",
            "pay_to": "0xTEST_PAY_TO",
            "facilitator_url": "https://facilitator.example",
            "max_per_request_minor": 900,
            "max_per_call_minor": 250,
            "enabled": True,
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["status"] == "enabled"
    with Session(engine) as session:
        rail = session.exec(select(RailConnection).where(RailConnection.agent_id == agent["id"], RailConnection.rail == "x402")).one()
        assert rail.config_json["pay_to"] == "0xTEST_PAY_TO"
        assert "private" not in rail.config_json


def test_dashboard_and_agent_detail_render_key_copy(client, operator_headers):
    agent = _register(client, operator_headers)["agent"]
    overview = client.get("/dashboard")
    assert overview.status_code == 200
    for copy in ["Payjent dashboard v0", "Agent registration", "Stripe Connect", "x402", "integration snippets"]:
        assert copy.lower().split()[0] in overview.text.lower()

    detail = client.get(f"/dashboard/agents/{agent['id']}")
    assert detail.status_code == 200
    for copy in ["Stripe Connect", "x402 rail configuration", "Integration snippet", "Recent payments / spend ledger", "discord-aggregator-stripe-smoke"]:
        assert copy in detail.text


def test_production_stripe_connect_start_fails_closed(client, operator_headers):
    agent = _register(client, operator_headers)["agent"]
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, public_base_url="https://payjent.example")
    try:
        r = client.post(f"/api/v1/agents/{agent['id']}/stripe-connect/start", headers=operator_headers)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert r.status_code == 503
    assert "refusing to simulate" in r.text
