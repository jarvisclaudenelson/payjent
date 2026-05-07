from sqlmodel import Session, select

from payjent.auth import create_bot_credential
from payjent.config import get_settings
from payjent.models import FulfillmentEvent


def _preset_payload(**overrides):
    payload = {
        "bot_id": "bot-1",
        "external_user_id": "user-1",
        "amount_minor": 300,
        "currency": "USD",
        "cost_breakdown": [{"label": "exact provider quote", "amount_minor": 300}],
        "input": {"query": "agent payments"},
        "request_hash": "preset-hash",
        "execution_readiness": {"provider_connected": True},
    }
    payload.update(overrides)
    return payload


def _presentation(payload):
    return {"bot_id": payload["bot_id"], "external_user_id": payload["external_user_id"], "request_hash": payload["request_hash"]}


def test_premium_preset_catalog_lists_provider_boundaries(client, bot_headers):
    r = client.get("/api/v1/premium-action-presets", headers=bot_headers)
    assert r.status_code == 200
    presets = r.json()["presets"]
    ids = {p["id"] for p in presets}
    assert {"exa.deep_search", "firecrawl.scrape", "elevenlabs.text_to_speech"} <= ids
    for preset in presets:
        assert preset["execution_boundary"] == "agent_executes_after_payjent_authorization"
        assert preset["secret_policy"]["payjent_stores_provider_secrets"] is False
        assert preset["endpoint"].startswith("https://")
        assert preset["method"] == "POST"


def test_each_preset_creates_safe_payment_gated_envelope(client, bot_headers, operator_headers):
    cases = [
        ("exa.deep_search", _preset_payload(request_hash="exa-hash", input={"query": "payjent", "num_results": 3}), "https://api.exa.ai/search"),
        ("firecrawl.scrape", _preset_payload(request_hash="fire-hash", input={"url": "https://example.com", "formats": ["markdown"], "only_main_content": True}), "https://api.firecrawl.dev/v2/scrape"),
        ("elevenlabs.text_to_speech", _preset_payload(request_hash="tts-hash", input={"text": "hello", "voice_id": "voice_123", "model_id": "eleven_turbo_v2"}), "https://api.elevenlabs.io/v1/text-to-speech/voice_123"),
    ]
    for preset_id, payload, endpoint in cases:
        created = client.post(f"/api/v1/premium-action-presets/{preset_id}/actions", json=payload, headers=bot_headers)
        assert created.status_code == 200, created.text
        action = created.json()
        assert action["status"] == "awaiting_payment"
        paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
        consumed = client.post(f"/api/v1/agent-actions/{action['action_id']}/consume", json={"payment_token": paid["grant"]["id"], "presentation": _presentation(payload)}, headers=bot_headers)
        assert consumed.status_code == 200, consumed.text
        env = consumed.json()["execution_envelope"]
        assert env["service_url"] == endpoint
        assert env["target_url"] == endpoint
        assert env["method"] == "POST"
        assert env["headers"] == {}
        assert "auth_instructions" in env
        assert "api" not in " ".join(env["headers"].values()).lower()
        assert env["boundary"] == "agent_executes_after_payjent_authorization"


def test_firecrawl_rejects_unsafe_target_url(client, bot_headers):
    for url in [
        "http://127.0.0.1:8000/private",
        "https://172.16.0.1/private",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/private",
        "https://localhost/private",
    ]:
        payload = _preset_payload(request_hash=f"unsafe-{url}", input={"url": url})
        r = client.post("/api/v1/premium-action-presets/firecrawl.scrape/actions", json=payload, headers=bot_headers)
        assert r.status_code == 422
        assert "public https" in r.text.lower()


def test_elevenlabs_voice_clone_fields_rejected(client, bot_headers):
    payload = _preset_payload(input={"text": "hello", "voice_id": "v1", "voice_clone": True})
    r = client.post("/api/v1/premium-action-presets/elevenlabs.text_to_speech/actions", json=payload, headers=bot_headers)
    assert r.status_code == 422
    assert "voice cloning" in r.text.lower()


def test_elevenlabs_voice_id_rejects_path_or_query_injection(client, bot_headers):
    for voice_id in ["voice/../../escape", "voice?model=bad", "voice#frag"]:
        payload = _preset_payload(request_hash=f"bad-voice-{voice_id}", input={"text": "hello", "voice_id": voice_id})
        r = client.post("/api/v1/premium-action-presets/elevenlabs.text_to_speech/actions", json=payload, headers=bot_headers)
        assert r.status_code == 422
        assert "voice_id" in r.text


def test_fail_endpoint_records_failed_event_and_mock_refund_idempotent(client, engine, bot_headers, operator_headers):
    payload = _preset_payload(request_hash="fail-refund-hash", input={"query": "refund"})
    action = client.post("/api/v1/premium-action-presets/exa.deep_search/actions", json=payload, headers=bot_headers).json()
    client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    failed = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/fail",
        json={
            "metadata": {
                "error_code": "provider_500",
                "api_key": "***",
                "nested": {"token": "secret", "safe": "kept"},
                "items": [{"authorization": "Bearer bad", "message": "ok"}, {"cookie": "bad"}],
            },
        },
        headers=bot_headers,
    )
    assert failed.status_code == 200, failed.text
    data = failed.json()
    assert data["status"] == "failed"
    assert data["refund_status"] == "succeeded"
    assert data["payment_status"] == "refunded"
    assert data["quote_status"] == "refunded"
    assert "api_key" not in data["metadata"]
    assert data["metadata"]["nested"] == {"safe": "kept"}
    assert data["metadata"]["items"] == [{"message": "ok"}, {}]
    duplicate = client.post(f"/api/v1/agent-actions/{action['action_id']}/fail", json={}, headers=bot_headers)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["refund_status"] == "already_refunded"
    with Session(engine) as session:
        refund_events = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == action["action_id"], FulfillmentEvent.status == "refunded")).all()
    assert len(refund_events) == 1


def test_fail_endpoint_can_explicitly_opt_out_of_default_refund(client, engine, bot_headers, operator_headers):
    payload = _preset_payload(request_hash="fail-no-refund-hash", input={"query": "no refund"})
    action = client.post("/api/v1/premium-action-presets/exa.deep_search/actions", json=payload, headers=bot_headers).json()
    client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)

    failed = client.post(f"/api/v1/agent-actions/{action['action_id']}/fail", json={"refund": False}, headers=bot_headers)

    assert failed.status_code == 200, failed.text
    data = failed.json()
    assert data["refund_status"] == "not_requested"
    assert data["payment_status"] == "paid"
    assert data["quote_status"] == "failed"
    with Session(engine) as session:
        refund_events = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == action["action_id"], FulfillmentEvent.status == "refunded")).all()
    assert refund_events == []


def test_fail_endpoint_cross_bot_scope_blocked(client, engine, bot_headers, operator_headers):
    with Session(engine) as session:
        create_bot_credential(session, "bot-2", "test-bot-2-key", get_settings().signing_secret)
    payload = _preset_payload(bot_id="bot-2", request_hash="bot2-hash", input={"query": "scope"})
    action = client.post("/api/v1/premium-action-presets/exa.deep_search/actions", json=payload, headers={"Authorization": "Bearer test-bot-2-key"}).json()
    client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers)
    r = client.post(f"/api/v1/agent-actions/{action['action_id']}/fail", json={"refund": False}, headers=bot_headers)
    assert r.status_code == 403
