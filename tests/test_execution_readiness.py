from payjent.models import PaymentSession
from sqlmodel import Session, select


def _x402_payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "request_summary": "paid x402 call",
        "request_hash": "ready-hash",
        "amount_minor": 100,
        "currency": "USD",
        "cost_breakdown": [{"label": "quote", "amount_minor": 100}],
        "service_url": "https://example.com/api",
        "body": {"q": "hello"},
    }
    payload.update(overrides)
    return payload


def test_paysponge_unauthenticated_blocked_no_payment_session(client, bot_headers, engine):
    r = client.post("/api/v1/premium-actions/pay-sh", json=_x402_payload(readiness_mode="enforced"), headers=bot_headers)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["ready_for_payment"] is False
    assert detail["charge_allowed"] is False
    assert "setup" in detail["setup_guidance"].lower() or "configure" in detail["setup_guidance"].lower()
    with Session(engine) as session:
        assert session.exec(select(PaymentSession)).all() == []


def test_ready_metadata_allows_checkout(client, bot_headers):
    payload = _x402_payload(
        request_hash="ready-metadata-hash",
        execution_readiness={"can_execute_without_device_auth": True, "labels": ["pay-sponge-runtime"]},
    )
    r = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["payment_session_id"].startswith("ps_")
    assert data["status"] == "awaiting_payment"


def test_provider_preset_requires_agent_side_readiness(client, bot_headers, engine):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "amount_minor": 300,
        "currency": "USD",
        "cost_breakdown": [{"label": "quote", "amount_minor": 300}],
        "input": {"query": "agent payments"},
        "request_hash": "exa-not-ready",
        "readiness_mode": "enforced",
    }
    blocked = client.post("/api/v1/premium-action-presets/exa.deep_search/actions", json=payload, headers=bot_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["ready_for_payment"] is False
    payload["execution_readiness"] = {"provider_connected": True, "labels": ["exa-sdk-configured"]}
    payload["request_hash"] = "exa-ready"
    ready = client.post("/api/v1/premium-action-presets/exa.deep_search/actions", json=payload, headers=bot_headers)
    assert ready.status_code == 200, ready.text
    with Session(engine) as session:
        assert len(session.exec(select(PaymentSession)).all()) == 1


def test_readiness_rejects_secret_leakage(client, bot_headers):
    payload = _x402_payload(execution_readiness={"can_execute_without_device_auth": True, "SPONGE_API_KEY": "sk_live_bad"})
    r = client.post("/api/v1/premium-actions/pay-sh", json=payload, headers=bot_headers)
    assert r.status_code == 422
    text = r.text.lower()
    assert "secret" in text
    assert "sk_live_bad" not in text


def test_readiness_route_records_safe_status_not_secrets(client, bot_headers):
    r = client.post(
        "/api/v1/agents/bot-1/execution-readiness",
        json={"provider": "firecrawl", "status": "ready", "provider_connected": True, "labels": ["firecrawl-worker"]},
        headers=bot_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ready_for_payment"] is True
    assert data["charge_allowed"] is True
    assert "api_key" not in str(data).lower()
