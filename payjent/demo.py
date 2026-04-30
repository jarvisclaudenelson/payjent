"""Local demo helpers and CLI for Payjent.

Commands:
  python -m payjent.demo seed
  python -m payjent.demo run-flow
  python -m payjent.demo link-purchase
  python -m payjent.demo reset-db
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import httpx
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session

from .auth import create_bot_credential, generate_api_key
from .config import get_settings
from .db import engine, get_session, init_db, make_engine
from .main import app
from .providers.link import LinkApproval

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


def _demo_link_purchase_payload() -> dict[str, Any]:
    return {
        "merchant_url": "https://merchant.example/checkout/demo-order-123",
        "credential_type": "card",
        "purpose": "Demo: agent-mediated merchant purchase for a bounded item",
        "metadata": {"demo": True, "merchant": "merchant.example"},
    }


def _fake_link_spend_request(_payload: Any) -> LinkApproval:
    provider_session_id = "sr_payjent_demo_link_purchase"
    return LinkApproval(
        approval_url=f"https://link.example/approve/{provider_session_id}",
        provider_session_id=provider_session_id,
        polling_command=["link-cli", "spend-request", "retrieve", provider_session_id, "--format", "json"],
        raw={"id": provider_session_id, "demo": True},
    )


def run_link_purchase_with_client(
    client: Any,
    *,
    bot_id: str,
    bot_key: str,
    operator_key: str,
    real_link: bool = False,
) -> dict[str, Any]:
    """Exercise quote -> Link checkout -> Link spend request without settlement.

    By default this injects a deterministic fake Link approval so the demo never
    calls link-cli, npm, MCP, or the network. Pass ``real_link=True`` only for an
    intentional operator-driven Link integration check.
    """
    bot_headers = {"X-Payjent-Bot-Key": bot_key}
    operator_headers = {"X-Payjent-Bot-Key": operator_key}
    quote_payload = {
        **_demo_quote_payload(bot_id),
        "request_summary": "Demo: agent-mediated merchant purchase",
        "request_hash": "demo-link-purchase-request-hash-1",
        "amount_minor": 1299,
        "cost_breakdown": [{"label": "merchant purchase", "amount_minor": 1299}],
        "execution_envelope": {"merchant_url": "https://merchant.example/checkout/demo-order-123", "item": "demo item"},
    }
    quote = _raise_for_demo_response(client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers), "create quote")
    checkout = _raise_for_demo_response(
        client.post(
            f"/api/v1/quotes/{quote['id']}/checkout",
            headers={**bot_headers, "X-Payjent-Provider": "link", "Idempotency-Key": "payjent-demo-link-purchase-1"},
        ),
        "create Link checkout",
    )
    if real_link:
        approval = _raise_for_demo_response(
            client.post(f"/api/v1/payment-sessions/{checkout['id']}/link/spend-request", json=_demo_link_purchase_payload(), headers=operator_headers),
            "create Link spend request",
        )
    else:
        import payjent.main as main_module
        original = main_module.create_link_provider_spend_request
        main_module.create_link_provider_spend_request = _fake_link_spend_request
        try:
            approval = _raise_for_demo_response(
                client.post(f"/api/v1/payment-sessions/{checkout['id']}/link/spend-request", json=_demo_link_purchase_payload(), headers=operator_headers),
                "create fake Link spend request",
            )
        finally:
            main_module.create_link_provider_spend_request = original
    current_session = _raise_for_demo_response(client.get(f"/api/v1/payment-sessions/{checkout['id']}"), "fetch payment session")
    return {"quote": quote, "payment_session": current_session, "link_approval": approval}


@contextmanager
def _isolated_demo_session() -> Iterator[tuple[Any, Any]]:
    """Yield a TestClient and engine backed by a temporary SQLite database.

    The default Link purchase demo is meant to prove the in-process flow. It
    should not be broken by, or unexpectedly mutate, an operator's stale local
    ./payjent.db unless they explicitly opt in with PAYJENT_DATABASE_URL.
    """
    with tempfile.TemporaryDirectory(prefix="payjent-link-demo-") as tmpdir:
        temp_engine = make_engine(f"sqlite:///{tmpdir}/payjent-demo.db")
        SQLModel.metadata.create_all(temp_engine)

        def override_session():
            with Session(temp_engine) as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        try:
            with TestClient(app) as client:
                yield client, temp_engine
        finally:
            app.dependency_overrides.pop(get_session, None)
            temp_engine.dispose()


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


def print_link_purchase_summary(result: dict[str, Any]) -> None:
    approval = result["link_approval"]
    payment_session = result["payment_session"]
    polling = approval.get("polling_command") or f"POST /api/v1/payment-sessions/{payment_session['id']}/link/poll"
    if isinstance(polling, list):
        polling = " ".join(polling)
    print("Payjent Link purchase demo created an approval request.")
    print(f"quote_id={result['quote']['id']}")
    print(f"payment_session_id={payment_session['id']}")
    print(f"approval_url={approval['approval_url']}")
    print(f"polling_hint={polling}")
    print(f"payment_session_status={payment_session['status']}")
    print("Settlement boundary: the Payjent session remains checkout_created/unpaid; no receipt or grant is issued by Link approval or credential creation alone.")
    print("Payjent must fail closed until verified terminal payment evidence is mapped, such as a successful merchant charge or future Link MPP/payment confirmation.")


def reset_dev_database() -> None:
    """Drop and recreate all SQLModel tables for a disposable pre-live database."""
    settings = get_settings()
    settings.ensure_db_reset_allowed()
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


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

    link = sub.add_parser("link-purchase", help="run a local Link approval demo without marking Payjent paid")
    link.add_argument("--bot-id", default=os.getenv("PAYJENT_DEMO_BOT_ID", DEFAULT_BOT_ID))
    link.add_argument("--bot-key", default=os.getenv("PAYJENT_BOT_KEY"))
    link.add_argument("--operator-key", default=os.getenv("PAYJENT_OPERATOR_KEY"))
    link.add_argument("--real-link", action="store_true", default=os.getenv("PAYJENT_DEMO_REAL_LINK", "").lower() in {"1", "true", "yes"}, help="opt in to the real Link provider path; default uses a deterministic fake approval and makes no external calls")

    sub.add_parser(
        "reset-db",
        help="drop/recreate all Payjent tables for a disposable local/dev database (pre-live only)",
    )
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
    if args.command == "link-purchase":
        if "PAYJENT_DATABASE_URL" in os.environ:
            init_db()
            credentials = seed_credentials(bot_id=args.bot_id) if not args.bot_key or not args.operator_key else None
            bot_key = args.bot_key or credentials.bot_key
            operator_key = args.operator_key or credentials.operator_key
            with TestClient(app) as client:
                result = run_link_purchase_with_client(client, bot_id=args.bot_id, bot_key=bot_key, operator_key=operator_key, real_link=args.real_link)
        else:
            with _isolated_demo_session() as (client, temp_engine):
                credentials = None
                if not args.bot_key or not args.operator_key:
                    with Session(temp_engine) as session:
                        credentials = seed_credentials(session=session, bot_id=args.bot_id)
                bot_key = args.bot_key or credentials.bot_key
                operator_key = args.operator_key or credentials.operator_key
                result = run_link_purchase_with_client(client, bot_id=args.bot_id, bot_key=bot_key, operator_key=operator_key, real_link=args.real_link)
        print_link_purchase_summary(result)
        return 0
    if args.command == "reset-db":
        reset_dev_database()
        print("Payjent disposable dev database reset complete. This command is pre-live/local only.")
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
