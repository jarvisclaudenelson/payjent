from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from payjent.sdk import PayjentClient


def request_hash_for(envelope: dict[str, Any]) -> str:
    """Stable hash for a bot execution envelope.

    Bot integrations should hash the command/tool inputs they intend to resume,
    store that hash with Payjent, and reject any post-payment attempt whose bot,
    user, or request hash differs from the original pending request.
    """

    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class PendingRequest:
    id: str
    bot_id: str
    external_user_id: str
    request_hash: str
    summary: str
    execution_envelope: dict[str, Any]
    quote_id: str
    payment_session_id: str
    checkout_url: str | None
    status: str = "checkout_created"
    expires_at: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    grant_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fulfilled_at: str | None = None
    fulfillment_id: str | None = None
    failure: str | None = None


class PendingRequestStore(Protocol):
    def save(self, pending: PendingRequest) -> None: ...
    def get(self, pending_id: str) -> PendingRequest | None: ...


class MemoryPendingRequestStore:
    def __init__(self) -> None:
        self._items: dict[str, PendingRequest] = {}

    def save(self, pending: PendingRequest) -> None:
        self._items[pending.id] = pending

    def get(self, pending_id: str) -> PendingRequest | None:
        return self._items.get(pending_id)


class JsonFilePendingRequestStore:
    """Tiny durable store suitable for demos and single-process bot prototypes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def save(self, pending: PendingRequest) -> None:
        data = self._read_all()
        data[pending.id] = asdict(pending)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def get(self, pending_id: str) -> PendingRequest | None:
        raw = self._read_all().get(pending_id)
        return PendingRequest(**raw) if raw else None


class PayjentBotGate:
    """Adapter for Discord/Hermes/C3PO-style bots.

    The gate stores a pending request before payment and resumes only from that
    stored execution envelope after Payjent verifies and consumes a paid grant.
    """

    def __init__(self, client: PayjentClient, store: PendingRequestStore | None = None, *, checkout_base_url: str | None = None) -> None:
        self.client = client
        self.store = store or MemoryPendingRequestStore()
        self.checkout_base_url = checkout_base_url or client.base_url

    def quote_pending_request(
        self,
        *,
        bot_id: str,
        external_user_id: str,
        summary: str,
        execution_envelope: dict[str, Any],
        amount_minor: int,
        currency: str,
        cost_breakdown: list[dict[str, Any]],
        channel_id: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        ttl_seconds: int = 3600,
    ) -> PendingRequest:
        req_hash = request_hash_for(execution_envelope)
        quote = self.client.create_quote(
            bot_id=bot_id,
            external_user_id=external_user_id,
            request_summary=summary,
            request_hash=req_hash,
            amount_minor=amount_minor,
            currency=currency,
            cost_breakdown=cost_breakdown,
            execution_envelope=execution_envelope,
        )
        checkout = self.client.create_checkout(quote["id"], idempotency_key=req_hash)
        checkout_url = checkout.get("checkout_url")
        if checkout_url and checkout_url.startswith("/"):
            checkout_url = f"{self.checkout_base_url}{checkout_url}"
        pending = PendingRequest(
            id=quote["id"],
            bot_id=bot_id,
            external_user_id=external_user_id,
            request_hash=req_hash,
            summary=summary,
            execution_envelope=execution_envelope,
            quote_id=quote["id"],
            payment_session_id=checkout["id"],
            checkout_url=checkout_url,
            status="awaiting_payment",
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        self.store.save(pending)
        return pending

    def poll_status(self, pending_id: str) -> PendingRequest:
        pending = self._require_pending(pending_id)
        payment = self.client.get_payment_session(pending.payment_session_id)
        quote = self.client.get_quote(pending.quote_id)
        pending.status = quote.get("status") or payment.get("status") or pending.status
        self.store.save(pending)
        return pending

    def resume_paid_request(
        self,
        pending_id: str,
        *,
        grant_id: str,
        bot_id: str,
        external_user_id: str,
        request_hash: str,
    ) -> dict[str, Any]:
        pending = self._require_pending(pending_id)
        if (bot_id, external_user_id, request_hash) != (pending.bot_id, pending.external_user_id, pending.request_hash):
            raise PermissionError("pending request context mismatch")
        presentation = {"bot_id": pending.bot_id, "external_user_id": pending.external_user_id, "request_hash": pending.request_hash}
        verified = self.client.verify_grant(grant_id, **presentation)
        if verified.get("payload", {}).get("quote_id") != pending.quote_id:
            raise PermissionError("grant quote_id does not match pending request")
        if not verified.get("consumed"):
            self.client.consume_grant(grant_id, **presentation)
        pending.grant_id = grant_id
        pending.status = "grant_consumed"
        self.store.save(pending)
        return {"pending_id": pending.id, "quote_id": pending.quote_id, "execution_envelope": pending.execution_envelope}

    def record_fulfillment(self, pending_id: str, status: str, metadata: dict[str, Any] | None = None) -> PendingRequest:
        pending = self._require_pending(pending_id)
        result = self.client.record_fulfillment(pending.quote_id, status, metadata or {})
        pending.status = result["status"]
        pending.fulfillment_id = result["id"]
        pending.fulfilled_at = datetime.now(timezone.utc).isoformat() if status == "fulfilled" else pending.fulfilled_at
        pending.failure = (metadata or {}).get("error") if status == "failed" else None
        self.store.save(pending)
        return pending

    def _require_pending(self, pending_id: str) -> PendingRequest:
        pending = self.store.get(pending_id)
        if not pending:
            raise KeyError(f"pending request not found: {pending_id}")
        return pending
