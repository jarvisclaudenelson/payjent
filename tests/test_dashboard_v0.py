from sqlmodel import Session, select

from payjent.auth import hash_api_key
from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.models import Account, AgentProfile, BotCredential, PaymentSession, Quote, RailConnection, SpendLedgerEntry


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
    assert client.post("/auth/register", data={"email": "dev@example.com", "password": "correct-pass"}, follow_redirects=False).status_code == 303
    created = client.post(
        "/dashboard/agents/register",
        data={"name": "Hermes Research", "platform": "discord", "bot_id": "hermes-bot", "default_currency": "USD"},
    )
    assert created.status_code == 200
    overview = client.get("/dashboard")

    for copy in ["Payment operations", "Agent registration", "install link", "Ask the agent"]:
        assert copy.lower().split()[0] in overview.text.lower()
    assert "integration snippets" not in overview.text

    assert overview.status_code == 200
    agent_id = overview.text.split("data-agent-id='", 1)[1].split("'", 1)[0]
    detail = client.get(f"/dashboard/agents/{agent_id}")
    assert detail.status_code == 200

    for copy in ["Payment rails", "Agent-readable setup checklist", "Recent payments / spend ledger"]:
        assert copy in detail.text
    assert "Integration snippet" not in detail.text
    assert "curl -X" not in detail.text


def test_root_landing_is_public_and_dashboard_shows_how_paid_currency_safe(client, engine):
    landing = client.get("/")
    assert landing.status_code == 200
    assert "approve paid agent actions" in landing.text
    assert "Register your agent" in landing.text

    assert client.post("/auth/register", data={"email": "dev@example.com", "password": "correct horse battery staple"}, follow_redirects=False).status_code == 303
    with Session(engine) as session:
        q_usd = Quote(id="quote_usd", bot_id="bot-1", external_user_id="user-1", request_summary="USD paid action", request_hash="hash-usd", amount_minor=150, currency="USD", cost_breakdown=[{"label": "work", "amount_minor": 150}], quote_hash="qh-usd", status="paid")
        q_eur = Quote(id="quote_eur", bot_id="bot-1", external_user_id="user-2", request_summary="EUR paid action", request_hash="hash-eur", amount_minor=250, currency="EUR", cost_breakdown=[{"label": "work", "amount_minor": 250}], quote_hash="qh-eur", status="fulfilled")
        session.add(q_usd)
        session.add(q_eur)
        session.add(PaymentSession(id="ps_mock", quote_id="quote_usd", provider="mock", status="paid"))
        session.add(PaymentSession(id="ps_stripe", quote_id="quote_eur", provider="stripe", status="checkout_created"))
        session.add(SpendLedgerEntry(id="spend_eur", grant_id="grant_eur", quote_id="quote_eur", operation_id="op-eur", tool="weather", vendor="meteo", rail="link", amount_minor=75, currency="EUR", reason="Need premium forecast", status="authorized"))
        session.commit()

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "How paid: mock / paid" in dashboard.text
    assert "How paid: stripe / checkout_created" in dashboard.text
    assert "1.50 USD" in dashboard.text
    assert "2.50 EUR" in dashboard.text
    assert "Grouped by currency" in dashboard.text
    assert "4.00 USD" not in dashboard.text
    assert "dashboard sample" not in dashboard.text


def test_production_dashboard_pages_fail_closed_without_metadata(client, operator_headers):
    agent = _register(client, operator_headers)["agent"]
    client.post(f"/api/v1/agents/{agent['id']}/stripe-connect/start", headers=operator_headers)
    client.post(
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
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, public_base_url="https://payjent.example")
    try:
        responses = [client.get("/dashboard", follow_redirects=False), client.get(f"/dashboard/agents/{agent['id']}", follow_redirects=False)]
    finally:
        app.dependency_overrides.pop(get_settings, None)

    forbidden_values = [
        "Hermes Research",
        "hermes-bot",
        "stripe_connect",
        "acct_test_",
        "x402 rail configuration",
        "0xTEST_PAY_TO",
        "facilitator.example",
        "discord-aggregator-stripe-smoke",
        "Integration snippet",
    ]
    for response in responses:
        assert response.status_code == 303
        assert response.headers["location"] in {"/auth/register", "/auth/login"}
        for value in forbidden_values:
            assert value not in response.text


def test_production_stripe_connect_start_fails_closed(client, operator_headers):
    agent = _register(client, operator_headers)["agent"]
    app.dependency_overrides[get_settings] = lambda: Settings(env="production", dev_mode=False, public_base_url="https://payjent.example")
    try:
        r = client.post(f"/api/v1/agents/{agent['id']}/stripe-connect/start", headers=operator_headers)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert r.status_code == 503
    assert "refusing to simulate" in r.text
