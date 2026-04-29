"""Local demo helpers and CLI for Payjent.

Commands:
  python -m payjent.demo seed
  python -m payjent.demo run-flow
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session

from .auth import create_bot_credential, generate_api_key
from .config import get_settings
from .db import engine, init_db
from .main import app

DEFAULT_BOT_ID = "discord-bot-1"
DEFAULT_OPERATOR_ID = "operator-1"
DEFAULT_EXTERNAL_USER_ID = "demo-user-1"
DEFAULT_REQUEST_HASH = "demo-request-hash-1"


@dataclass(frozen=True)
class DemoCredentials:
    bot_id: str
    operator_id: str
    bot_key: str
    operator_key: str


def seed_credentials(
    *,
    session: Session | None = None,
    bot_id: str = DEFAULT_BOT_ID,
    operator_id: str = DEFAULT_OPERATOR_ID,
) -> DemoCredentials:
    """Create local demo bot/operator credentials and return plaintext keys once."""
    settings = get_settings()
    bot_key = generate_api_key()
    operator_key = generate_api_key()

    def _create(s: Session) -> None:
        create_bot_credential(s, bot_id, bot_key, settings.signing_secret, role="bot")
        create_bot_credential(s, operator_id, operator_key, settings.signing_secret, role="operator")

    if session is None:
        init_db()
        with Session(engine) as owned_session:
            _create(owned_session)
    else:
        _create(session)

    return DemoCredentials(bot_id=bot_id, operator_id=operator_id, bot_key=bot_key, operator_key=operator_key)


def print_seed_exports(credentials: DemoCredentials) -> None:
    print("Created Payjent demo credentials. Plaintext keys are shown once; store them in your shell.", file=sys.stderr)
    print(f"export PAYJENT_DEMO_BOT_ID={credentials.bot_id!r}")
    print(f"export PAYJENT_BOT_KEY={credentials.bot_key!r}")
    print(f"export PAYJENT_OPERATOR_KEY={credentials.operator_key!r}")


def _demo_quote_payload(bot_id: str) -> dict[str, Any]:
    return {
        "bot_id": bot_id,
        "external_user_id": DEFAULT_EXTERNAL_USER_ID,
        "request_summary": "Demo: summarize a short PDF",
        "request_hash": DEFAULT_REQUEST_HASH,
        "amount_minor": 500,
        "currency": "USD",
        "cost_breakdown": [{"label": "analysis", "amount_minor": 500}],
        "execution_envelope": {"tool": "summarizer", "max_pages": 10},
    }


def _raise_for_demo_response(response: Any, step: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"{step} failed: HTTP {response.status_code}: {response.text}")
    return response.json()


def run_flow_with_client(client: Any, *, bot_id: str, bot_key: str, operator_key: str) -> dict[str, Any]:
    """Exercise quote -> checkout -> mock-pay -> verify -> consume -> fulfill."""
    bot_headers = {"X-Payjent-Bot-Key": bot_key}
    operator_headers = {"X-Payjent-Bot-Key": operator_key}

    quote = _raise_for_demo_response(
        client.post("/api/v1/quotes", json=_demo_quote_payload(bot_id), headers=bot_headers),
        "create quote",
    )
    checkout = _raise_for_demo_response(
        client.post(
            f"/api/v1/quotes/{quote['id']}/checkout",
            headers={**bot_headers, "Idempotency-Key": "payjent-demo-flow-1"},
        ),
        "checkout",
    )
    paid = _raise_for_demo_response(
        client.post(f"/api/v1/payment-sessions/{checkout['id']}/mock-pay", headers=operator_headers),
        "mock pay",
    )
    grant = paid["grant"]
    presentation = {
        "bot_id": bot_id,
        "external_user_id": quote["external_user_id"],
        "request_hash": quote["request_hash"],
    }
    verified = _raise_for_demo_response(
        client.post(f"/api/v1/grants/{grant['id']}/verify", json=presentation, headers=bot_headers),
        "verify grant",
    )
    consumed = _raise_for_demo_response(
        client.post(f"/api/v1/grants/{grant['id']}/consume", json=presentation, headers=bot_headers),
        "consume grant",
    )
    fulfillment = _raise_for_demo_response(
        client.post(
            f"/api/v1/quotes/{quote['id']}/fulfillment",
            json={"status": "fulfilled", "metadata": {"demo": True}},
            headers=bot_headers,
        ),
        "record fulfillment",
    )
    return {
        "quote": quote,
        "payment_session": checkout,
        "grant": grant,
        "verified": verified,
        "consumed": consumed,
        "fulfillment": fulfillment,
    }


def run_local_flow(*, bot_id: str, bot_key: str, operator_key: str, base_url: str | None = None) -> dict[str, Any]:
    if base_url:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0) as client:
            return run_flow_with_client(client, bot_id=bot_id, bot_key=bot_key, operator_key=operator_key)
    init_db()
    with TestClient(app) as client:
        return run_flow_with_client(client, bot_id=bot_id, bot_key=bot_key, operator_key=operator_key)


def print_flow_summary(result: dict[str, Any]) -> None:
    print("Payjent demo flow completed.")
    print(f"quote_id={result['quote']['id']}")
    print(f"payment_session_id={result['payment_session']['id']}")
    print(f"checkout_url={result['payment_session']['checkout_url']}")
    print(f"grant_id={result['grant']['id']}")
    print(f"fulfillment_id={result['fulfillment']['id']}")
    print(f"final_status={result['fulfillment']['status']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Payjent local demo utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create demo bot/operator credentials")
    seed.add_argument("--bot-id", default=DEFAULT_BOT_ID)
    seed.add_argument("--operator-id", default=DEFAULT_OPERATOR_ID)

    flow = sub.add_parser("run-flow", help="run the local quote-to-fulfillment demo flow")
    flow.add_argument("--bot-id", default=os.getenv("PAYJENT_DEMO_BOT_ID", DEFAULT_BOT_ID))
    flow.add_argument("--bot-key", default=os.getenv("PAYJENT_BOT_KEY"))
    flow.add_argument("--operator-key", default=os.getenv("PAYJENT_OPERATOR_KEY"))
    flow.add_argument("--base-url", default=os.getenv("PAYJENT_BASE_URL"), help="optional running Payjent API base URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seed":
        credentials = seed_credentials(bot_id=args.bot_id, operator_id=args.operator_id)
        print_seed_exports(credentials)
        return 0
    if args.command == "run-flow":
        if not args.bot_key or not args.operator_key:
            raise SystemExit("PAYJENT_BOT_KEY and PAYJENT_OPERATOR_KEY are required; run `python -m payjent.demo seed` first.")
        result = run_local_flow(
            bot_id=args.bot_id,
            bot_key=args.bot_key,
            operator_key=args.operator_key,
            base_url=args.base_url,
        )
        print_flow_summary(result)
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
