"""Dry Discord-bot style flow for Payjent.

This does not connect to Discord. It shows the sequence a Discord bot handler would
run around a paid action. Start Payjent locally and provide PAYJENT_BOT_KEY to run
against a live dev server, or read it as integration pseudocode.
"""

from __future__ import annotations

import os

from payjent.sdk import PayjentClient


def main() -> None:
    base_url = os.getenv("PAYJENT_BASE_URL", "http://127.0.0.1:8000")
    bot_key = os.getenv("PAYJENT_BOT_KEY", "dev-bot-key")
    client = PayjentClient(base_url=base_url, api_key=bot_key)

    quote = client.create_quote(
        bot_id="discord-bot-demo",
        external_user_id="discord-user-123",
        request_summary="Generate a one-page research brief",
        request_hash="demo-request-hash",
        amount_minor=500,
        currency="USD",
        cost_breakdown=[{"label": "research brief", "amount_minor": 500}],
        execution_envelope={"command": "/brief", "max_words": 700},
    )
    checkout = client.create_checkout(quote["id"], idempotency_key="discord-demo-brief-1")

    print("Send this checkout link to the Discord user:")
    print(f"{base_url}/pay/{checkout['id']}")
    print("After the dev mock payment, the bot would verify and consume the issued grant.")

    # In a real bot handler, discover the grant from webhook/state after payment.
    # The public status page is useful for local demo: {base_url}/status/{checkout['id']}.
    print(f"Status page: {base_url}/status/{checkout['id']}")

    # Example post-payment calls once a grant_id is known:
    # presentation = {"bot_id": quote["bot_id"], "external_user_id": quote["external_user_id"], "request_hash": quote["request_hash"]}
    # client.verify_grant(grant_id, **presentation)
    # client.consume_grant(grant_id, **presentation)
    # client.record_fulfillment(quote["id"], "fulfilled", {"discord_message_id": "..."})


if __name__ == "__main__":
    main()
