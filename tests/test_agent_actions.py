def _create_action(client, quote_payload, bot_headers):
    return client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers)


def _presentation(quote_payload, **overrides):
    data = {
        "bot_id": quote_payload["bot_id"],
        "external_user_id": quote_payload["external_user_id"],
        "request_hash": quote_payload["request_hash"],
    }
    data.update(overrides)
    return data


def _pay_sh_payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "premium forecast via pay.sh",
        "request_hash": "pay-sh-hash-1",
        "amount_minor": 450,
        "currency": "USD",
        "cost_breakdown": [{"label": "premium pay.sh call", "amount_minor": 450}],
        "service_url": "https://api.weather.ai/forecast",
        "method": "POST",
        "body": {"city": "Lisbon"},
        "description": "Weather forecast",
    }
    payload.update(overrides)
    return payload


def _premium_payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "generate premium image via external provider",
        "request_hash": "generic-premium-hash-1",
        "amount_minor": 725,
        "currency": "USD",
        "cost_breakdown": [{"label": "provider quote", "amount_minor": 725}],
        "provider": "fal_ai",
        "target_url": "https://queue.fal.run/fal-ai/flux/schnell",
        "method": "POST",
        "body": {"prompt": "Lisbon at sunset"},
        "headers": {"Content-Type": "application/json"},
        "description": "Create image through fal.ai",
        "provider_metadata": {"model": "fal-ai/flux/schnell"},
    }
    payload.update(overrides)
    return payload


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


def test_generic_premium_action_endpoint_creates_non_pay_sh_provider_action(client, bot_headers, operator_headers):
    payload = _premium_payload()
    r = client.post("/api/v1/premium-actions", json=payload, headers=bot_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "fal_ai"
    assert data["premium_provider"] == "fal_ai"
    assert data["command_preview"].startswith("POST https://queue.fal.run/fal-ai/flux/schnell")
    assert data["execution_boundary"] == "agent_executes_after_payjent_authorization"
    paid = client.post(f"/api/v1/payment-sessions/{data['payment_session_id']}/mock-pay", headers=operator_headers).json()
    consumed = client.post(
        f"/api/v1/agent-actions/{data['action_id']}/consume",
        json={"payment_token": paid["grant"]["id"], "presentation": _presentation(payload)},
        headers=bot_headers,
    )
    assert consumed.status_code == 200
    envelope = consumed.json()["execution_envelope"]
    assert envelope["provider"] == "fal_ai"
    assert envelope["kind"] == "premium_action"
    assert envelope["target_url"] == payload["target_url"]
    assert envelope["settlement"] == "provider_external_runtime"
    assert envelope["boundary"] == "agent_executes_after_payjent_authorization"
    assert envelope["payjent_fulfillment_callback"] is False
    assert envelope["payjent_managed_execution"] is False


def test_generic_premium_action_rejects_unsafe_url_before_checkout(client, bot_headers):
    r = client.post("/api/v1/premium-actions", json=_premium_payload(target_url="http://127.0.0.1:8000/private"), headers=bot_headers)
    assert r.status_code == 422
    assert "https" in r.text.lower() or "public" in r.text.lower()


def test_generic_premium_action_rejects_secret_header_before_checkout(client, bot_headers):
    r = client.post("/api/v1/premium-actions", json=_premium_payload(headers={"Authorization": "Bearer nope"}), headers=bot_headers)
    assert r.status_code == 422
    assert "secret-like" in r.text


def test_generic_premium_action_allows_non_url_metadata_backed_action(client, bot_headers):
    payload = _premium_payload(
        request_hash="generic-premium-hash-2",
        target_url=None,
        service_url=None,
        body={},
        provider_metadata={"provider_action_id": "quote_123", "model": "premium-model"},
    )
    r = client.post("/api/v1/premium-actions", json=payload, headers=bot_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "fal_ai"
    assert data["command_preview"].startswith("POST provider:fal_ai")
    assert data["provider_metadata"]["provider_action_id"] == "quote_123"


def test_generic_premium_action_rejects_true_payjent_execution_flags(client, bot_headers):
    r = client.post("/api/v1/premium-actions", json=_premium_payload(payjent_managed_execution=True), headers=bot_headers)
    assert r.status_code == 422
    assert "does not execute" in r.text


def test_pay_sh_premium_action_endpoint_creates_provider_action(client, bot_headers):
    r = client.post("/api/v1/premium-actions/pay-sh", json=_pay_sh_payload(), headers=bot_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "pay_sh"
    assert data["premium_provider"] == "pay_sh"
    assert data["command_preview"].startswith("paycurl -X POST https://api.weather.ai/forecast")
    assert "Lisbon" in data["command_preview"]
    assert data["status"] == "awaiting_payment"


def test_pay_sh_consumed_envelope_returns_provider_metadata(client, bot_headers, operator_headers):
    payload = _pay_sh_payload()
    action = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    r = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": paid["grant"]["id"], "presentation": _presentation(payload)},
        headers=bot_headers,
    )
    assert r.status_code == 200
    envelope = r.json()["execution_envelope"]
    assert envelope["provider"] == "pay_sh"
    assert envelope["kind"] == "premium_api_call"
    assert envelope["service_url"] == payload["service_url"]
    assert envelope["settlement"] == "external_x402_runtime"
    assert envelope["command_preview"] == action["command_preview"]


def test_pay_sh_premium_action_endpoint_rejects_missing_target(client, bot_headers):
    payload = _pay_sh_payload(service_url=None, service_fqn=None, resource=None)
    r = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers)
    assert r.status_code == 422
    assert "service_url" in r.text


def test_pay_sh_premium_action_rejects_fal_site_root_before_checkout(client, bot_headers):
    payload = _pay_sh_payload(
        service_url="https://fal.ai",
        body={},
        description="Create an image through fal.ai via pay.sh",
    )
    r = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers)
    assert r.status_code == 422
    assert "not an executable pay.sh/x402 gateway endpoint" in r.text
    assert "paysponge/fal" in r.text
    assert "fal-ai/flux/schnell" in r.text


def test_paysponge_fal_envelope_uses_sponge_wallet_runtime_not_plain_paycurl(client, bot_headers, operator_headers):
    payload = _pay_sh_payload(
        request_hash="paysponge-fal-hash-1",
        service_url="https://fal.x402.paysponge.com/fal-ai/fast-sdxl",
        service_fqn="paysponge/fal",
        resource="fal-ai/fast-sdxl",
        body={"prompt": "Lisbon at sunset"},
    )
    action = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers)
    assert action.status_code == 200
    created = action.json()
    assert created["command_preview"].startswith("npx spongewallet pay fetch")
    assert "fal.x402.paysponge.com/fal-ai/fast-sdxl" in created["command_preview"]
    assert "paycurl" not in created["command_preview"]

    paid = client.post(f"/api/v1/payment-sessions/{created['payment_session_id']}/mock-pay", headers=operator_headers).json()
    consumed = client.post(
        f"/api/v1/agent-actions/{created['action_id']}/consume",
        json={"payment_token": paid["grant"]["id"], "presentation": _presentation(payload)},
        headers=bot_headers,
    )
    assert consumed.status_code == 200
    envelope = consumed.json()["execution_envelope"]
    assert envelope["x402_runtime"] == "sponge"
    assert envelope["settlement"] == "external_x402_runtime"
    assert envelope["agent_runtime_requirements"]["credential"] == "SPONGE_API_KEY in the agent runtime only"
    assert "does not satisfy the downstream x402 HTTP 402 challenge" in envelope["agent_runtime_requirements"]["execution_note"]


def test_unpaid_agent_action_cannot_start(client, quote_payload, bot_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    r = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": "grant_missing", "presentation": _presentation(quote_payload)},
        headers=bot_headers,
    )
    assert r.status_code == 409


def test_paid_agent_action_consumes_once_and_binds_request_hash(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    token = paid["grant"]["id"]

    bad = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": token, "presentation": _presentation(quote_payload, request_hash="wrong")},
        headers=bot_headers,
    )
    assert bad.status_code == 403

    wrong_user = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": token, "presentation": _presentation(quote_payload, external_user_id="wrong-user")},
        headers=bot_headers,
    )
    assert wrong_user.status_code == 403

    wrong_bot = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": token, "presentation": _presentation(quote_payload, bot_id="wrong-bot")},
        headers=bot_headers,
    )
    assert wrong_bot.status_code == 403

    ok = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={
            "payment_token": token,
            "presentation": _presentation(quote_payload),
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
        json={"payment_token": token, "presentation": _presentation(quote_payload)},
        headers=bot_headers,
    )
    assert replay.status_code == 409


def test_paid_agent_action_consume_requires_presentation(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()

    r = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": paid["grant"]["id"]},
        headers=bot_headers,
    )
    assert r.status_code == 422


def test_agent_action_complete_records_fulfillment(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    started = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/start",
        json={"payment_token": paid["grant"]["id"], "presentation": _presentation(quote_payload)},
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


def test_agent_action_status_requires_bot_auth_and_scope(client, engine, quote_payload, bot_headers):
    from sqlmodel import Session
    from payjent.auth import create_bot_credential
    from payjent.config import get_settings

    action = _create_action(client, quote_payload, bot_headers).json()
    assert client.get(f"/api/v1/agent-actions/{action['action_id']}").status_code in {401, 403}
    with Session(engine) as session:
        create_bot_credential(session, "bot-2", "wrong-bot-key", get_settings().signing_secret)
    wrong = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers={"Authorization": "Bearer wrong-bot-key"})
    assert wrong.status_code == 403


def test_agent_action_status_token_lifecycle(client, quote_payload, bot_headers, operator_headers):
    action = _create_action(client, quote_payload, bot_headers).json()
    unpaid = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert unpaid["payment_status"] == "checkout_created"
    assert unpaid["status"] == "awaiting_payment"
    assert unpaid["payment_token"] is None
    assert unpaid["payment_token_status"] == "unissued"

    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    ready = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert ready["payment_status"] == "paid"
    assert ready["status"] == "ready"
    assert ready["payment_token"] == paid["grant"]["id"]
    assert ready["payment_token_status"] == "available"

    consumed = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        json={"payment_token": ready["payment_token"], "presentation": _presentation(quote_payload)},
        headers=bot_headers,
    )
    assert consumed.status_code == 200
    after = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert after["status"] == "consumed"
    assert after["payment_token"] is None
    assert after["payment_token_status"] == "consumed"
