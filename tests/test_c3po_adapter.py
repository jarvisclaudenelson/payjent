import pytest

from payjent.c3po_adapter import C3POPayjentBridge, MemoryPendingPremiumRequestStore
from payjent.sdk import PayjentClient


def _bridge(client):
    sdk = PayjentClient("http://testserver", api_key="test-bot-key", client=client)
    store = MemoryPendingPremiumRequestStore()
    return C3POPayjentBridge(sdk, bot_id="bot-1", store=store, public_base_url="http://testserver"), store


def _request(bridge):
    return bridge.request_pay_sh_data(
        community_user_id="user-1",
        summary="premium forecast via C3PO",
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
    assert pending.action_id in message
    assert store.get(pending.action_id) == pending


def test_resume_rejects_wrong_user_hash_or_missing_token(client, operator_headers):
    bridge, _ = _bridge(client)
    pending, _ = _request(bridge)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()

    with pytest.raises(ValueError):
        bridge.resume_after_payment(action_id=pending.action_id, community_user_id="user-1", payment_token="")
    with pytest.raises(PermissionError):
        bridge.resume_after_payment(action_id=pending.action_id, community_user_id="wrong-user", payment_token=paid["grant"]["id"])
    with pytest.raises(PermissionError):
        bridge.resume_after_payment(action_id=pending.action_id, community_user_id="user-1", payment_token=paid["grant"]["id"], request_hash="wrong")


def test_successful_resume_returns_pay_sh_command_preview_and_mark_fulfilled(client, operator_headers):
    bridge, store = _bridge(client)
    pending, _ = _request(bridge)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()

    resumed = bridge.resume_after_payment(action_id=pending.action_id, community_user_id="user-1", payment_token=paid["grant"]["id"])

    envelope = resumed["execution_envelope"]
    assert envelope["provider"] == "pay_sh"
    assert envelope["settlement"] == "external_pay_sh_runtime"
    assert envelope["command_preview"] == pending.command_preview
    assert store.get(pending.action_id).status == "consumed"

    fulfilled = bridge.mark_fulfilled(pending.action_id, "fulfilled", {"result": "ok"})
    assert fulfilled.status == "fulfilled"
    assert fulfilled.metadata["result"] == "ok"
    assert store.get(pending.action_id).fulfilled_at
