def _premium_payload(amount=25, budget_id=None, request_hash="micro-1"):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "micro premium call",
        "request_hash": request_hash,
        "amount_minor": amount,
        "currency": "USD",
        "cost_breakdown": [{"label": "provider", "amount_minor": amount}],
        "provider": "exa",
        "target_url": "https://payjent.com/provider/action",
    }
    if budget_id:
        payload["task_budget_id"] = budget_id
    return payload


def _fund_budget(client, bot_headers, operator_headers, max_amount=100):
    created = client.post("/api/v1/task-budgets", json={
        "bot_id": "bot-1", "external_user_id": "user-1", "task_id": "task-1",
        "max_amount_minor": max_amount, "currency": "USD",
    }, headers=bot_headers)
    assert created.status_code == 200
    budget_id = created.json()["id"]
    checkout = client.post(f"/api/v1/task-budgets/{budget_id}/checkout", headers=bot_headers)
    assert checkout.status_code == 200
    funded = client.post(f"/api/v1/task-budgets/{budget_id}/mock-fund", headers=operator_headers)
    assert funded.status_code == 200
    return funded.json()


def test_sub_50_premium_action_without_budget_rejected(client, bot_headers):
    response = client.post("/api/v1/premium-actions", json=_premium_payload(), headers=bot_headers)
    assert response.status_code == 402
    assert "task_budget_id" in response.json()["detail"]


def test_active_budget_reserves_micro_action(client, bot_headers, operator_headers):
    budget = _fund_budget(client, bot_headers, operator_headers, max_amount=100)
    response = client.post("/api/v1/premium-actions", json=_premium_payload(budget_id=budget["id"]), headers=bot_headers)
    assert response.status_code == 200
    assert response.json()["payment_url"] is None
    status = client.get(f"/api/v1/task-budgets/{budget['id']}", headers=bot_headers).json()
    assert status["available_minor"] == 75
    assert status["reserved_minor"] == 25


def test_insufficient_budget_rejected(client, bot_headers, operator_headers):
    budget = _fund_budget(client, bot_headers, operator_headers, max_amount=10)
    response = client.post("/api/v1/premium-actions", json=_premium_payload(budget_id=budget["id"]), headers=bot_headers)
    assert response.status_code == 422
    assert "sufficient" in response.json()["detail"]


def test_budget_capture_and_release_unused(client, bot_headers, operator_headers):
    budget = _fund_budget(client, bot_headers, operator_headers, max_amount=100)
    action = client.post("/api/v1/premium-actions", json=_premium_payload(budget_id=budget["id"]), headers=bot_headers).json()
    status_response = client.get(f"/api/v1/agent-actions/{action['action_id']}/status", headers=bot_headers).json()
    client.post(f"/api/v1/agent-actions/{action['action_id']}/start", json={"payment_token": status_response["payment_token"], "presentation": {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": action["request_hash"]}}, headers=bot_headers)
    complete = client.post(f"/api/v1/agent-actions/{action['action_id']}/complete", json={"status": "fulfilled", "metadata": {}}, headers=bot_headers)
    assert complete.status_code == 200
    status = client.get(f"/api/v1/task-budgets/{budget['id']}", headers=bot_headers).json()
    assert status["reserved_minor"] == 0
    assert status["captured_minor"] == 25
    released = client.post(f"/api/v1/task-budgets/{budget['id']}/release-unused", headers=bot_headers).json()
    assert released["available_minor"] == 0
    assert released["released_minor"] == 75
    assert released["status"] == "closed"


def test_failed_budget_action_releases_once(client, bot_headers, operator_headers):
    budget = _fund_budget(client, bot_headers, operator_headers, max_amount=100)
    action = client.post("/api/v1/premium-actions", json=_premium_payload(budget_id=budget["id"]), headers=bot_headers).json()
    first = client.post(f"/api/v1/agent-actions/{action['action_id']}/fail", json={"reason": "provider_failed"}, headers=bot_headers)
    second = client.post(f"/api/v1/agent-actions/{action['action_id']}/fail", json={"reason": "provider_failed"}, headers=bot_headers)
    assert first.status_code == 200
    assert second.status_code in {200, 409}
    status = client.get(f"/api/v1/task-budgets/{budget['id']}", headers=bot_headers).json()
    assert status["available_minor"] == 100
    assert status["reserved_minor"] == 0
    assert status["released_minor"] == 25
