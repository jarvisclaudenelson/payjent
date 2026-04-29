"""Pending request resume demo for a Discord-style bot.

No Discord token is required. Run a local Payjent server, seed credentials with
`python -m payjent.demo seed`, export PAYJENT_BOT_KEY and PAYJENT_OPERATOR_KEY,
then run:

    python examples/discord_resume_flow.py

The script creates a pending request, prints the checkout URL, performs a dev
operator mock payment, resumes from the stored execution envelope only after
verifying/consuming the Payjent grant, and records fulfillment.
"""

from __future__ import annotations

import os

from payjent.bot_adapter import JsonFilePendingRequestStore, PayjentBotGate
from payjent.sdk import PayjentClient


def execute_expensive_thing(envelope: dict[str, object]) -> dict[str, object]:
    # A real bot would dispatch tools from this stored envelope, not from fresh
    # prompt text supplied after payment.
    return {"result": f"brief generated for topic={envelope['topic']}", "words": envelope["max_words"]}


def main() -> None:
    base_url = os.getenv("PAYJENT_BASE_URL", "http://127.0.0.1:8000")
    bot_key = os.getenv("PAYJENT_BOT_KEY", "test-bot-key")
    operator_key = os.getenv("PAYJENT_OPERATOR_KEY", "test-operator-key")

    bot_client = PayjentClient(base_url=base_url, api_key=bot_key)
    operator_client = PayjentClient(base_url=base_url, api_key=operator_key)
    gate = PayjentBotGate(bot_client, JsonFilePendingRequestStore(".payjent_pending_demo.json"), checkout_base_url=base_url)

    envelope = {"command": "/expensive-brief", "topic": "agent payment gates", "max_words": 500}
    pending = gate.quote_pending_request(
        bot_id="bot-1",
        external_user_id="discord-user-123",
        channel_id="channel-456",
        message_id="message-789",
        summary="Generate an expensive research brief",
        execution_envelope=envelope,
        amount_minor=750,
        currency="USD",
        cost_breakdown=[{"label": "research brief", "amount_minor": 750}],
    )
    print(f"Bot reply: please pay here: {pending.checkout_url}")
    print(f"Stored pending request {pending.id} for request_hash={pending.request_hash}")

    paid = operator_client._request("POST", f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay", headers=operator_client._headers())
    grant_id = paid["grant"]["id"]
    print(f"Operator/mock payment issued grant: {grant_id}")

    resume = gate.resume_paid_request(
        pending.id,
        grant_id=grant_id,
        bot_id=pending.bot_id,
        external_user_id=pending.external_user_id,
        request_hash=pending.request_hash,
    )
    result = execute_expensive_thing(resume["execution_envelope"])
    fulfilled = gate.record_fulfillment(pending.id, "fulfilled", {"discord_message_id": "reply-001", **result})
    print(f"Fulfillment recorded: status={fulfilled.status} fulfillment_id={fulfilled.fulfillment_id}")


if __name__ == "__main__":
    main()
