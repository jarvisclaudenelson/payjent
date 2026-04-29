import pytest

from payjent.bot_adapter import MemoryPendingRequestStore, PayjentBotGate
from payjent.sdk import PayjentClient


def make_gate(client):
    return PayjentBotGate(PayjentClient(api_key="test-bot-key", client=client), MemoryPendingRequestStore())


def quote_pending(gate):
    return gate.quote_pending_request(
        bot_id="bot-1",
        external_user_id="user-1",
        summary="expensive bot thing",
        execution_envelope={"tool": "brief", "topic": "payments"},
        amount_minor=300,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 300}],
        channel_id="chan-1",
        message_id="msg-1",
    )


def test_unpaid_pending_request_cannot_execute(client):
    gate = make_gate(client)
    pending = quote_pending(gate)

    polled = gate.poll_status(pending.id)
    assert polled.status == "quoted"
    with pytest.raises(Exception):
        gate.resume_paid_request(
            pending.id,
            grant_id="grant_missing",
            bot_id=pending.bot_id,
            external_user_id=pending.external_user_id,
            request_hash=pending.request_hash,
        )


def test_paid_unconsumed_grant_is_consumed_then_execution_envelope_resumes(client, operator_headers):
    gate = make_gate(client)
    pending = quote_pending(gate)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()
    grant_id = paid["grant"]["id"]

    resumed = gate.resume_paid_request(
        pending.id,
        grant_id=grant_id,
        bot_id=pending.bot_id,
        external_user_id=pending.external_user_id,
        request_hash=pending.request_hash,
    )

    assert resumed["execution_envelope"] == {"tool": "brief", "topic": "payments"}
    verify = client.post(
        f"/api/v1/grants/{grant_id}/verify",
        headers={"Authorization": "Bearer test-bot-key"},
        json={"bot_id": pending.bot_id, "external_user_id": pending.external_user_id, "request_hash": pending.request_hash},
    ).json()
    assert verify["consumed"] is True


def test_wrong_user_bot_or_request_hash_cannot_resume(client, operator_headers):
    gate = make_gate(client)
    pending = quote_pending(gate)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()
    grant_id = paid["grant"]["id"]

    for overrides in (
        {"bot_id": "other-bot"},
        {"external_user_id": "other-user"},
        {"request_hash": "other-hash"},
    ):
        kwargs = {"bot_id": pending.bot_id, "external_user_id": pending.external_user_id, "request_hash": pending.request_hash, **overrides}
        with pytest.raises(PermissionError):
            gate.resume_paid_request(pending.id, grant_id=grant_id, **kwargs)


def test_fulfilled_status_recorded(client, operator_headers):
    gate = make_gate(client)
    pending = quote_pending(gate)
    paid = client.post(f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_headers).json()
    gate.resume_paid_request(
        pending.id,
        grant_id=paid["grant"]["id"],
        bot_id=pending.bot_id,
        external_user_id=pending.external_user_id,
        request_hash=pending.request_hash,
    )

    fulfilled = gate.record_fulfillment(pending.id, "fulfilled", {"discord_message_id": "reply-1"})

    assert fulfilled.status == "fulfilled"
    assert fulfilled.fulfillment_id.startswith("ful_")
    assert client.get(f"/api/v1/quotes/{pending.quote_id}").json()["status"] == "fulfilled"
