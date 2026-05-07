from payjent.rails import normalize_spend_rail


def _register_agent(client, operator_headers):
    response = client.post(
        "/api/v1/agents/register",
        json={"name": "Rail Agent", "platform": "test", "bot_id": "bot-1"},
        headers=operator_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["agent"]


def test_lists_top_seven_settlement_rail_manifests(client):
    response = client.get("/api/v1/settlement-rails")
    assert response.status_code == 200
    rails = response.json()
    names = [rail["rail"] for rail in rails]
    assert names == [
        "circle_nanopayments",
        "x402_cdp",
        "stripe_machine_payments",
        "google_ap2",
        "crossmint_wallet",
        "moonpay_agents",
        "visa_tap",
    ]
    assert all(rail["supports_budget_reservation"] for rail in rails)
    assert {rail["spend_authorization_rail"] for rail in rails} == set(names)


def test_agent_can_report_and_discover_any_top_settlement_rail(client, bot_headers):
    for rail in ["circle", "coinbase_x402", "mpp", "ap2", "crossmint", "moonpay", "visa"]:
        response = client.post(
            "/api/v1/agents/bot-1/settlement-rails/report",
            json={
                "rail": rail,
                "status": "active",
                "mode": "agent_reported",
                "enabled": True,
                "config": {"network": "testnet", "spend_limit_minor": 250},
            },
            headers=bot_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "active"
        assert "api_key" not in data["config"]

    discovered = client.get("/api/v1/agents/bot-1/settlement-rails", headers=bot_headers)
    assert discovered.status_code == 200
    connections = discovered.json()["connections"]
    for rail in ["circle_nanopayments", "x402_cdp", "stripe_machine_payments", "google_ap2", "crossmint_wallet", "moonpay_agents", "visa_tap"]:
        assert connections[rail]["status"] == "active"
    assert "spend-authorizations" in discovered.json()["spend_instruction"]


def test_operator_can_configure_settlement_rail_for_agent(client, operator_headers):
    agent = _register_agent(client, operator_headers)
    response = client.post(
        f"/api/v1/agents/{agent['id']}/settlement-rails",
        json={"rail": "circle_nanopayments", "status": "enabled", "mode": "external_runtime", "config": {"network": "gateway-sandbox"}},
        headers=operator_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rail"] == "circle_nanopayments"
    assert data["config"]["network"] == "gateway-sandbox"


def test_spend_authorization_accepts_top_settlement_rails(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    start = client.post(
        f"/api/v1/agent-actions/{q['id']}/start",
        json={"payment_token": client.get(f"/api/v1/agent-actions/{q['id']}/status", headers=bot_headers).json()["payment_token"], "presentation": {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": q["request_hash"]}},
        headers=bot_headers,
    )
    assert start.status_code == 200, start.text
    for index, rail in enumerate(["circle", "coinbase_x402", "mpp", "ap2", "crossmint", "moonpay", "visa"]):
        response = client.post(
            f"/api/v1/grants/{grant['id']}/spend-authorizations",
            json={
                "operation_id": f"op-{index}",
                "presentation": {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": q["request_hash"]},
                "tool": "provider.tool",
                "vendor": "provider",
                "rail": rail,
                "amount_minor": 1,
                "currency": "USD",
                "reason": "bounded rail spend",
            },
            headers=bot_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["rail"] == normalize_spend_rail(rail)
