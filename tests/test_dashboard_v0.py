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

    for copy in ["Payment operations", "Agent registration", "Stripe Connect", "x402", "integration snippets"]:
        assert copy.lower().split()[0] in overview.text.lower()

    assert overview.status_code == 200
    agent_id = overview.text.split("data-agent-id='", 1)[1].split("'", 1)[0]
    detail = client.get(f"/dashboard/agents/{agent_id}")
    assert detail.status_code == 200

    for copy in ["Stripe Connect", "x402 rail configuration", "Integration snippet", "Recent payments / spend ledger", "discord-aggregator-stripe-smoke"]:
        assert copy in detail.text


def test_dashboard_launch_checklist_and_action_status_are_safe(client, engine):
    assert client.post("/auth/register", data={"email": "launch@example.com", "password": "correc...aple"}, follow_redirects=False).status_code == 303
    created = client.post(
        "/dashboard/agents/register",
        data={"name": "Launch Agent", "platform": "discord", "bot_id": "launch-bot", "default_currency": "USD"},
    )
    assert created.status_code == 200
    forbidden_secret = "payjent_FORBIDDEN_SECRET_TOKEN"
    with Session(engine) as session:
        stage_rows = [
            ("quote_stage_quoted", "quoted", None),
            ("quote_stage_checkout", "quoted", "checkout_created"),
            ("quote_stage_paid", "paid", "paid"),
            ("quote_stage_executing", "executing", "paid"),
            ("quote_stage_succeeded", "fulfilled", "paid"),
            ("quote_stage_failed", "failed", "paid"),
        ]
        for quote_id, status, payment_status in stage_rows:
            session.add(Quote(id=quote_id, bot_id="launch-bot", external_user_id="user", request_summary=f"{status} action", request_hash=f"hash-{quote_id}", amount_minor=100, currency="USD", cost_breakdown=[{"label": "work", "amount_minor": 100}], quote_hash=f"qh-{quote_id}", status=status))
            if payment_status:
                session.add(PaymentSession(id=f"ps_{quote_id}", quote_id=quote_id, provider="mock", status=payment_status, checkout_url=forbidden_secret, provider_session_id=forbidden_secret, idempotency_key=forbidden_secret))
        session.commit()

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    for label in [
        "Launch checklist",
        "Agent registered",
        "Install link / credential path",
        "Active checkout readiness",
        "Discovery manifest / status endpoint",
        "Paid action lifecycle evidence",
        "Failure / refund path",
        "Action status overview",
        "quoted → checkout → paid → executing → succeeded/failed",
        "Quoted without checkout",
        "Checkout created but payment not marked paid",
        "Paid but no execution/fulfillment evidence yet",
        "Payment cleared and work has started",
        "Fulfillment evidence recorded",
        "Failed or refund path active",
    ]:
        assert label in dashboard.text
    for stage in [">quoted<", ">checkout<", ">paid<", ">executing<", ">succeeded<", ">failed<"]:
        assert stage in dashboard.text
    assert forbidden_secret not in dashboard.text
    assert "provider_session_id" not in dashboard.text
    assert "idempotency_key" not in dashboard.text

    agent_id = dashboard.text.split("data-agent-id='", 1)[1].split("'", 1)[0]
    detail = client.get(f"/dashboard/agents/{agent_id}")
    assert detail.status_code == 200
    assert "Agent action status" in detail.text
    for stage in [">quoted<", ">checkout<", ">paid<", ">executing<", ">succeeded<", ">failed<"]:
        assert stage in detail.text
    assert forbidden_secret not in detail.text


def test_dashboard_scopes_action_status_to_signed_in_account_agents(client, engine):
    assert client.post("/auth/register", data={"email": "owner-a@example.com", "password": "correct horse battery staple"}, follow_redirects=False).status_code == 303
    client.post(
        "/dashboard/agents/register",
        data={"name": "Owner A Agent", "platform": "discord", "bot_id": "owner-a-bot", "default_currency": "USD"},
    )
    with Session(engine) as session:
        session.add(Quote(id="quote_owner_a", bot_id="owner-a-bot", external_user_id="user-a", request_summary="owner a private task", request_hash="hash-owner-a", amount_minor=100, currency="USD", cost_breakdown=[{"label": "work", "amount_minor": 100}], quote_hash="qh-owner-a", status="paid"))
        session.commit()
    owner_a_dashboard = client.get("/dashboard")
    assert "owner a private task" in owner_a_dashboard.text

    client.post("/auth/logout", follow_redirects=False)
    assert client.post("/auth/register", data={"email": "owner-b@example.com", "password": "correct horse battery staple"}, follow_redirects=False).status_code == 303
    client.post(
        "/dashboard/agents/register",
        data={"name": "Owner B Agent", "platform": "discord", "bot_id": "owner-b-bot", "default_currency": "USD"},
    )
    with Session(engine) as session:
        session.add(Quote(id="quote_owner_b", bot_id="owner-b-bot", external_user_id="user-b", request_summary="owner b visible task", request_hash="hash-owner-b", amount_minor=200, currency="USD", cost_breakdown=[{"label": "work", "amount_minor": 200}], quote_hash="qh-owner-b", status="paid"))
        session.commit()

    owner_b_dashboard = client.get("/dashboard")
    assert owner_b_dashboard.status_code == 200
    assert "owner b visible task" in owner_b_dashboard.text
    assert "owner a private task" not in owner_b_dashboard.text
    assert "quote_owner_a" not in owner_b_dashboard.text


def test_root_landing_is_public_and_dashboard_shows_how_paid_currency_safe(client, engine):
    landing = client.get("/")
    assert landing.status_code == 200
    assert "Payment-gate agent actions" in landing.text
    assert "Register your agent" in landing.text

    assert client.post("/auth/register", data={"email": "dev@example.com", "password": "correct horse battery staple"}, follow_redirects=False).status_code == 303
    with Session(engine) as session:
        account = session.exec(select(Account).where(Account.email == "dev@example.com")).one()
        session.add(AgentProfile(id="agent_dashboard_safe", owner_id=account.id, bot_id="bot-1", name="Dashboard Agent", platform="discord"))
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
