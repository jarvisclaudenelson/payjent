def _create_action(client, quote_payload, bot_headers):
    return client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers)


def test_create_agent_action_returns_payment_prompt_and_action_id(client, quote_payload, bot_headers):
    r = _create_action(client, quote_payload, bot_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["action_id"] == data["quote_id"]
    assert data["payment_session_id"].startswith("ps_")
    assert data["payment_url"].startswith("/pay/")
    assert data["status"] == "awaiting_payment"
    assert data["request_hash"] == quote_payload["request_hash"]
    assert data["payment_prompt"]["action_id"] == data["action_id"]
    assert "Payment required" in data["message"]


def test_unpaid_agent_action_cannot_start(client, quote_payload, bot_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    r = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": "grant_missing"},
        headers=bot_headers,
    )
    assert r.status_code == 409


def test_paid_agent_action_consumes_once_and_binds_request_hash(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    token = paid["grant"]["id"]

    bad = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": token, "presentation": {"request_hash": "wrong"}},
        headers=bot_headers,
    )
    assert bad.status_code == 403

    ok = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={
            "payment_token": token,
            "presentation": {
                "bot_id": quote_payload["bot_id"],
                "external_user_id": quote_payload["external_user_id"],
                "request_hash": quote_payload["request_hash"],
            },
        },
        headers=bot_headers,
    )
    assert ok.status_code == 200
    envelope = ok.json()
    assert envelope["action_id"] == action["action_id"]
    assert envelope["execution_envelope"] == quote_payload["execution_envelope"]
    assert envelope["request_hash"] == quote_payload["request_hash"]

    replay = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": token},
        headers=bot_headers,
    )
    assert replay.status_code == 409


def test_agent_action_complete_records_fulfillment(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    started = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/start",
        json={"payment_token": paid["grant"]["id"]},
        headers=bot_headers,
    )
    assert started.status_code == 200
    r = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/complete",
        json={"status": "fulfilled", "metadata": {"result": "ok"}},
        headers=bot_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["action_id"] == action["action_id"]
    assert data["status"] == "fulfilled"
    assert data["metadata"]["result"] == "ok"
