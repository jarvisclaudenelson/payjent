from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select

from .models import RailConnection

SECRET_MARKERS = ("api_key", "apikey", "authorization", "cookie", "token", "secret", "password", "credential", "sponge_api_key")
PRESET_PROVIDERS = {"exa", "firecrawl", "elevenlabs"}
X402_PROVIDERS = {"pay_sh", "paysh", "x402"}


def contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            lk = str(k).lower()
            if any(marker in lk for marker in SECRET_MARKERS):
                return True
            if contains_secret_key(v):
                return True
    elif isinstance(value, list):
        return any(contains_secret_key(v) for v in value)
    return False


def safe_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    if contains_secret_key(value):
        raise HTTPException(status_code=422, detail={
            "ready_for_payment": False,
            "charge_allowed": False,
            "reason": "readiness metadata must not include API keys, Authorization, Cookie, tokens, or secrets",
            "setup_guidance": "Send only booleans/status/labels proving the agent runtime is already connected.",
        })
    return dict(value)


def _truthy_ready(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"ready", "connected", "active", "ok", "available"}:
        return True
    return False


def _rail_ready(rail: RailConnection | None) -> bool:
    if not rail or rail.status not in {"active", "ready", "connected"}:
        return False
    cfg = rail.config_json or {}
    return bool(cfg.get("enabled", True)) and any(_truthy_ready(cfg.get(k)) for k in ("runtime_ready", "ready", "can_execute_without_device_auth", "connected"))


def _payload_ready(metadata: dict[str, Any], *, provider: str) -> bool:
    nested = metadata.get("execution_readiness") or metadata.get("readiness") or metadata.get("provider_readiness")
    evidence = nested if isinstance(nested, dict) else metadata
    if provider in X402_PROVIDERS:
        return any(_truthy_ready(evidence.get(k)) for k in ("runtime_ready", "can_execute_without_device_auth", "ready", "connected", "status"))
    return any(_truthy_ready(evidence.get(k)) for k in ("provider_connected", "connected", "ready", "status"))


def readiness_record(session: Session, bot_id: str, provider: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = (provider or "generic").lower().replace("-", "_")
    metadata = safe_metadata(metadata)
    rail_names = ["x402", "pay_sh"] if provider in X402_PROVIDERS else [f"provider:{provider}", provider]
    rails = session.exec(select(RailConnection).where(RailConnection.bot_id == bot_id, RailConnection.rail.in_(rail_names))).all()
    rail = next((r for r in rails if _rail_ready(r)), None)
    ready = bool(rail) or _payload_ready(metadata, provider=provider)
    guidance = "Runtime ready." if ready else (
        "Configure the agent runtime/rail and report only safe readiness evidence before requesting payment. "
        "Do not send provider API keys, SPONGE_API_KEY, Authorization, Cookie, tokens, or secrets to Payjent."
    )
    return {
        "bot_id": bot_id,
        "provider": provider,
        "ready_for_payment": ready,
        "charge_allowed": ready,
        "readiness_mode": "enforced",
        "evidence": {
            "rail_connection": bool(rail),
            "agent_metadata": _payload_ready(metadata, provider=provider),
            "labels": metadata.get("labels", []) if isinstance(metadata.get("labels", []), list) else [],
        },
        "setup_guidance": guidance,
    }


def enforce_readiness(session: Session, *, bot_id: str, provider: str, readiness_mode: str = "enforced", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    mode = (readiness_mode or "enforced").lower()
    record = readiness_record(session, bot_id, provider, metadata)
    record["readiness_mode"] = mode
    if mode == "advisory":
        record["charge_allowed"] = True
        return record
    if not record["ready_for_payment"]:
        raise HTTPException(status_code=409, detail=record)
    return record
