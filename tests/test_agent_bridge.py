import pytest

from payjent.agent_bridge import AgentPayjentBridge, MemoryPendingPremiumRequestStore
from payjent.c3po_adapter import C3POPayjentBridge
from payjent.sdk import PayjentClient


def _bridge(client):
    sdk = PayjentClient("http://testserver", api_key="test-bot-key", client=client)
    store = MemoryPendingPremiumRequestStore()
    return AgentPayjentBridge(sdk, bot_id="bot-1", store=store, public_base_url="http://testserver"), store


def _request(bridge):
    return bridge.request_pay_sh_data(
        agent_user_id="user-1",
        request_summary="premium forecast via an agent",
        amount_minor=450,
        service_url="https://api.weather.ai/forecast",
        method="POST",
        body={"city": "Lisbon"},
        description="premium weather",
    )


def test_request_pay_sh_data_returns_payment_prompt_and_stores_pending(client):
    bridge, store = _bridge(client)
    pending, message = _request(bridge)

    assert pending.action_id.startswith("quote_")
    assert pending.payment_session_id.startswith("ps_")
    assert pending.payment_url.startswith("http://testserver/pay/")
    assert pending.command_preview.startswith("paycurl -X POST https://api.weather.ai/forecast")
    assert "Lisbon" in pending.command_preview
    assert "Payment required" in message
    assert "your agent can poll Payjent" in message
    assert pending.action_id in message
    assert store.get(pending.action_id) == pending


def test_resume_rejects_wrong_user_hash_or_missing_token(client, operator_headers):
    bridge, _ = _bridge(client)
    pending, _ = _request(bridge)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()

    with pytest.raises(ValueError):
        bridge.resume_after_payment(pending_id=pending.action_id, agent_user_id="user-1", payment_token="")
    with pytest.raises(PermissionError):
        bridge.resume_after_payment(pending_id=pending.action_id, agent_user_id="wrong-user", payment_token=paid["grant"]["id"])
    with pytest.raises(PermissionError):
        bridge.resume_after_payment(pending_id=pending.action_id, agent_user_id="user-1", payment_token=paid["grant"]["id"], request_hash="wrong")


def test_successful_resume_when_paid_polls_token_consumes_and_mark_fulfilled(client, operator_headers):
    bridge, store = _bridge(client)
    pending, _ = _request(bridge)

    unpaid = bridge.resume_when_paid(pending_id=pending.action_id, agent_user_id="user-1", timeout_seconds=0)
    assert unpaid["status"] == "awaiting_payment"
    assert unpaid["payment_token"] is None

    client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers)
    resumed = bridge.resume_when_paid(pending_id=pending.action_id, agent_user_id="user-1", timeout_seconds=0)

    envelope = resumed["execution_envelope"]
    assert envelope["provider"] == "pay_sh"
    assert envelope["settlement"] == "external_x402_runtime"
    assert envelope["command_preview"] == pending.command_preview
    assert store.get(pending.action_id).status == "consumed"

    consumed = bridge.check_payment(pending.action_id)
    assert consumed["payment_token"] is None
    assert consumed["payment_token_status"] == "consumed"

    fulfilled = bridge.mark_fulfilled(pending.action_id, "fulfilled", {"result": "ok"})
    assert fulfilled.status == "fulfilled"
    assert fulfilled.metadata["result"] == "ok"
    assert store.get(pending.action_id).fulfilled_at


def test_c3po_adapter_remains_compatibility_alias(client):
    sdk = PayjentClient("http://testserver", api_key="test-bot-key", client=client)
    bridge = C3POPayjentBridge(sdk, bot_id="bot-1", store=MemoryPendingPremiumRequestStore(), public_base_url="http://testserver")
    pending, message = bridge.request_pay_sh_data(
        community_user_id="user-1",
        summary="legacy C3PO-compatible request",
        amount_minor=450,
        service_url="https://api.weather.ai/forecast",
    )
    assert pending.community_user_id == "user-1"
    assert pending.summary == "legacy C3PO-compatible request"
    assert "your agent can poll Payjent" in message
