from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from payjent.sdk import PayjentClient


@dataclass
class PendingPremiumRequest:
    action_id: str
    bot_id: str
    community_user_id: str
    payment_session_id: str
    payment_url: str | None
    request_hash: str
    command_preview: str | None
    status: str = "awaiting_payment"
    summary: str | None = None
    payment_message: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fulfilled_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PendingPremiumRequestStore(Protocol):
    def save(self, pending: PendingPremiumRequest) -> None: ...
    def get(self, action_id: str) -> PendingPremiumRequest | None: ...


class MemoryPendingPremiumRequestStore:
    def __init__(self) -> None:
        self._items: dict[str, PendingPremiumRequest] = {}

    def save(self, pending: PendingPremiumRequest) -> None:
        self._items[pending.action_id] = pending

    def get(self, action_id: str) -> PendingPremiumRequest | None:
        return self._items.get(action_id)


class JsonFilePendingPremiumRequestStore:
    """Tiny durable store suitable for a single C3PO/community-agent process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def save(self, pending: PendingPremiumRequest) -> None:
        data = self._read_all()
        data[pending.action_id] = asdict(pending)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def get(self, action_id: str) -> PendingPremiumRequest | None:
        raw = self._read_all().get(action_id)
        return PendingPremiumRequest(**raw) if raw else None


def pay_sh_request_hash(*, bot_id: str, community_user_id: str, target: dict[str, Any], body: Any = None, summary: str | None = None) -> str:
    canonical = json.dumps(
        {"bot_id": bot_id, "community_user_id": community_user_id, "target": target, "body": body, "summary": summary},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class C3POPayjentBridge:
    """Community-agent bridge for Payjent-gated pay.sh premium data.

    This adapter creates and resumes Payjent actions only. It never shells out,
    invokes paycurl, or settles pay.sh. C3PO should execute the returned envelope
    in its own pay.sh runtime after successful resume.
    """

    def __init__(self, client: PayjentClient, *, bot_id: str, store: PendingPremiumRequestStore | None = None, public_base_url: str | None = None) -> None:
        self.client = client
        self.bot_id = bot_id
        self.store = store or MemoryPendingPremiumRequestStore()
        self.public_base_url = (public_base_url or client.base_url).rstrip("/")

    def request_pay_sh_data(
        self,
        *,
        community_user_id: str,
        summary: str,
        amount_minor: int,
        currency: str = "USD",
        cost_breakdown: list[dict[str, Any]] | None = None,
        service_url: str | None = None,
        service_fqn: str | None = None,
        resource: str | None = None,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        description: str | None = None,
        request_hash: str | None = None,
        **extra: Any,
    ) -> tuple[PendingPremiumRequest, str]:
        target = {"service_url": service_url, "service_fqn": service_fqn, "resource": resource, "method": method}
        req_hash = request_hash or pay_sh_request_hash(
            bot_id=self.bot_id, community_user_id=community_user_id, target=target, body=body, summary=summary
        )
        payload = {
            "bot_id": self.bot_id,
            "external_user_id": community_user_id,
            "request_summary": summary,
            "request_hash": req_hash,
            "amount_minor": amount_minor,
            "currency": currency,
            "cost_breakdown": cost_breakdown or [{"label": description or "premium pay.sh data", "amount_minor": amount_minor}],
            "service_url": service_url,
            "service_fqn": service_fqn,
            "resource": resource,
            "method": method,
            "body": body,
            "headers": headers,
            "description": description,
            **extra,
        }
        action = self.client.create_pay_sh_premium_action(**{k: v for k, v in payload.items() if v is not None})
        payment_url = action.get("payment_url")
        if payment_url and payment_url.startswith("/"):
            payment_url = f"{self.public_base_url}{payment_url}"
        pending = PendingPremiumRequest(
            action_id=action["action_id"],
            bot_id=self.bot_id,
            community_user_id=community_user_id,
            payment_session_id=action["payment_session_id"],
            payment_url=payment_url,
            request_hash=action.get("request_hash", req_hash),
            command_preview=action.get("command_preview"),
            status=action.get("status", "awaiting_payment"),
            summary=summary,
            payment_message=action.get("message"),
        )
        self.store.save(pending)
        message = self.payment_prompt_for(pending)
        return pending, message

    def payment_prompt_for(self, pending: PendingPremiumRequest) -> str:
        return (
            f"Payment required for premium pay.sh data: {pending.summary}\n"
            f"Pay here: {pending.payment_url}\n"
            f"Action: {pending.action_id}\n"
            f"After payment, return the payment token so C3PO can resume the stored request."
        )

    def resume_after_payment(self, *, action_id: str, community_user_id: str, payment_token: str, request_hash: str | None = None) -> dict[str, Any]:
        if not payment_token:
            raise ValueError("payment_token is required")
        pending = self._require_pending(action_id)
        if community_user_id != pending.community_user_id:
            raise PermissionError("pending request user mismatch")
        if request_hash is not None and request_hash != pending.request_hash:
            raise PermissionError("pending request hash mismatch")
        result = self.client.consume_agent_action(
            action_id,
            payment_token,
            presentation={"bot_id": pending.bot_id, "external_user_id": pending.community_user_id, "request_hash": pending.request_hash},
        )
        pending.status = "consumed"
        self.store.save(pending)
        return result

    def mark_fulfilled(self, action_id: str, status: str = "fulfilled", metadata: dict[str, Any] | None = None) -> PendingPremiumRequest:
        pending = self._require_pending(action_id)
        result = self.client.complete_agent_action(action_id, status, metadata or {})
        pending.status = result.get("status", status)
        pending.metadata = result.get("metadata", metadata or {})
        if pending.status == "fulfilled":
            pending.fulfilled_at = datetime.now(timezone.utc).isoformat()
        self.store.save(pending)
        return pending

    def _require_pending(self, action_id: str) -> PendingPremiumRequest:
        pending = self.store.get(action_id)
        if not pending:
            raise KeyError(f"pending premium request not found: {action_id}")
        return pending
