"""Thin paid agent action facade over Payjent quotes, checkouts, grants, and fulfillment."""

from __future__ import annotations

from typing import Any

from .models import FulfillmentEvent, Grant, PaymentSession, Quote


def action_id_for_quote(quote: Quote | str) -> str:
    """Return the client-facing agent action id.

    MVP note: action_id is the Quote id, intentionally aliased while Payjent
    keeps the durable payment/request binding in the existing Quote row.
    """
    return quote if isinstance(quote, str) else quote.id


def build_payment_prompt(*, action_id: str, request_summary: str, amount_minor: int, currency: str, payment_url: str | None) -> dict[str, Any]:
    amount = f"{amount_minor / 100:.2f} {currency.upper()}"
    message = f"Payment required to run this agent action: {request_summary}. Pay {amount}: {payment_url}"
    return {
        "action_id": action_id,
        "payment_url": payment_url,
        "amount_minor": amount_minor,
        "currency": currency.upper(),
        "message": message,
    }


def create_paid_action_response(*, quote: Quote, payment_session: PaymentSession) -> dict[str, Any]:
    prompt = build_payment_prompt(
        action_id=action_id_for_quote(quote),
        request_summary=quote.request_summary,
        amount_minor=quote.amount_minor,
        currency=quote.currency,
        payment_url=payment_session.checkout_url,
    )
    return {
        "action_id": action_id_for_quote(quote),
        "quote_id": quote.id,
        "payment_session_id": payment_session.id,
        "payment_url": payment_session.checkout_url,
        "amount_minor": quote.amount_minor,
        "currency": quote.currency,
        "status": "awaiting_payment" if payment_session.status != "paid" else "paid",
        "request_hash": quote.request_hash,
        "payment_prompt": prompt,
        "message": prompt["message"],
    }


def execution_envelope_for_action(*, quote: Quote, grant: Grant) -> dict[str, Any]:
    return {
        "action_id": action_id_for_quote(quote),
        "quote_id": quote.id,
        "grant_id": grant.id,
        "payment_token": grant.id,
        "request_hash": quote.request_hash,
        "external_user_id": quote.external_user_id,
        "bot_id": quote.bot_id,
        "execution_envelope": quote.execution_envelope,
        "status": "ready_to_execute",
    }


def action_result_response(event: FulfillmentEvent) -> dict[str, Any]:
    return {
        "action_id": action_id_for_quote(event.quote_id),
        "quote_id": event.quote_id,
        "fulfillment_id": event.id,
        "status": event.status,
        "metadata": event.metadata_json,
    }
