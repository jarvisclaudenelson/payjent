from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .money import quote_hash

STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY: dict[str, int] = {}

TRUSTED_PAYSH_EXECUTION_CAVEAT = (
    "Trusted allowlisted pay.sh/x402 metadata only. Payjent does not execute arbitrary URLs "
    "and does not claim live crypto settlement; the agent executes externally with its own "
    "funded pay.sh/x402 runtime after Payjent approval."
)

MANAGED_EXECUTION_CAVEAT = (
    "Payjent-managed provider runtime. Payjent quotes and gates a bounded task budget; "
    "provider execution uses Payjent-managed provider credentials/runtime without exposing sensitive values."
)

FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD = "external_runtime"
FAL_EXTERNAL_RUNTIME_GUIDANCE = (
    "paysh.fal_image is an external pay.sh/x402 fallback for advanced agents only. "
    "Use fal.image.generate for Payjent-managed FAL image generation, or pass "
    "external_runtime=true to explicitly opt into the external runtime."
)

_TOOLBOX: dict[str, dict[str, Any]] = {
    "exa.deep_search": {
        "tool_id": "exa.deep_search",
        "display_name": "Exa Deep Search",
        "description": "Premium web research using Exa search APIs.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "num_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]},
        "pricing_model": "agent_runtime_quote",
        "pricing_source": "agent_runtime",
        "supported_payment_rails": ["task_budget", "decal", "stablecoin"],
        "recommended_payment_rail": "task_budget",
        "risk_level": "low",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
    "firecrawl.scrape": {
        "tool_id": "firecrawl.scrape",
        "display_name": "Firecrawl Scrape",
        "description": "Fetch and extract a single public web page via Firecrawl.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string", "format": "uri"}}, "required": ["url"]},
        "pricing_model": "agent_runtime_quote",
        "pricing_source": "agent_runtime",
        "supported_payment_rails": ["task_budget", "decal", "stablecoin"],
        "recommended_payment_rail": "task_budget",
        "risk_level": "medium",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
    "fal.image.generate": {
        "tool_id": "fal.image.generate",
        "display_name": "FAL Image Generate",
        "description": "Canonical/default FAL image generation through Payjent-managed FAL provider runtime.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 4}}, "required": ["prompt"]},
        "pricing_model": "agent_runtime_quote",
        "pricing_source": "agent_runtime",
        "supported_payment_rails": ["task_budget", "decal", "stablecoin"],
        "recommended_payment_rail": "decal",
        "default_for": ["fal", "fal.image", "fal.image.generate", "image_generation"],
        "agent_recommendation": "Use this Payjent-managed tool for normal/default FAL image generation. Do not route FAL image requests to paysh.fal_image unless the caller explicitly opts into external_runtime=true.",
        "risk_level": "medium",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
    "elevenlabs.text_to_speech": {
        "tool_id": "elevenlabs.text_to_speech",
        "display_name": "ElevenLabs Text to Speech",
        "description": "Generate speech audio from text using an agent-managed ElevenLabs account.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "voice": {"type": "string"}}, "required": ["text"]},
        "pricing_model": "agent_runtime_quote",
        "pricing_source": "agent_runtime",
        "supported_payment_rails": ["task_budget", "decal", "stablecoin"],
        "recommended_payment_rail": "task_budget",
        "risk_level": "low",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
}

for tool_id, name, desc in [
    ("paysh.fal_image", "External pay.sh FAL Image Fallback", "Advanced external pay.sh/x402 FAL image fallback metadata; not the default FAL path."),
    ("paysh.web_scrape", "Trusted pay.sh Web Scrape", "Allowlisted pay.sh/x402 web scraping metadata."),
    ("paysh.search", "Trusted pay.sh Search", "Allowlisted pay.sh/x402 search metadata."),
    ("paysh.data_extract", "Trusted pay.sh Data Extract", "Allowlisted pay.sh/x402 data extraction metadata."),
    ("paysh.file_convert", "Trusted pay.sh File Convert", "Allowlisted pay.sh/x402 file conversion metadata."),
]:
    _TOOLBOX[tool_id] = {
        "tool_id": tool_id,
        "display_name": name,
        "description": desc,
        "provider_type": "trusted_paysh",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"instructions": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["instructions"]},
        "pricing_model": "agent_runtime_quote",
        "pricing_source": "agent_runtime",
        "supported_payment_rails": ["pay_sh", "x402", "task_budget", "decal", "stablecoin"],
        "recommended_payment_rail": "pay_sh",
        "risk_level": "medium",
        "execution_mode": "external_trusted_paysh_x402_runtime",
        "trusted_metadata": {"allowlisted": True, "arbitrary_url_execution": False, "live_settlement_claim": False},
        "settlement_caveat": TRUSTED_PAYSH_EXECUTION_CAVEAT,
    }

_TOOLBOX["paysh.fal_image"].update(
    {
        "status": "advanced_external_fallback",
        "default_for": [],
        "external_fallback_for": "fal.image.generate",
        "requires_explicit_opt_in": FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD,
        "agent_recommendation": FAL_EXTERNAL_RUNTIME_GUIDANCE,
        "input_schema": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD: {"type": "boolean", "const": True, "description": "Required to use the external pay.sh/x402 FAL fallback instead of Payjent-managed fal.image.generate."},
            },
            "required": ["instructions", FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD],
        },
    }
)


def list_tools() -> list[dict[str, Any]]:
    return [public_tool(tool) for tool in _TOOLBOX.values()]


def get_tool(tool_id: str) -> dict[str, Any] | None:
    tool = _TOOLBOX.get(tool_id)
    return public_tool(tool) if tool else None


def public_tool(tool: dict[str, Any]) -> dict[str, Any]:
    # Pricing basis is public metadata; no credentials, grants, API keys, or executable URLs are included.
    return deepcopy(tool)


def normalize_cost_breakdown(cost_breakdown: list[dict[str, Any]] | None, amount_minor: int) -> list[dict[str, Any]]:
    if cost_breakdown is None:
        return [{"label": "Agent runtime quoted toolbox action", "amount_minor": amount_minor}]
    normalized: list[dict[str, Any]] = []
    total = 0
    for item in cost_breakdown:
        label = str(item.get("label") or "Toolbox action")
        line_amount = int(item.get("amount_minor", 0))
        if line_amount < 0:
            raise ValueError("cost_breakdown amount_minor must be >= 0")
        normalized.append({"label": label, "amount_minor": line_amount})
        total += line_amount
    if total != amount_minor:
        raise ValueError("cost_breakdown must sum to amount_minor")
    return normalized


def choose_payment_options(tool: dict[str, Any], amount_minor: int, currency: str) -> tuple[list[dict[str, Any]], str, bool]:
    currency = currency.upper()
    stripe_minimum_applies = False
    trusted = tool["provider_type"] in {"trusted_paysh", "trusted_x402"}
    recommended = "pay_sh" if trusted else (tool.get("recommended_payment_rail") or "decal")
    options: list[dict[str, Any]] = []
    for rail in tool["supported_payment_rails"]:
        option = {"rail": rail, "status": "available", "recommended": rail == recommended}
        if rail == "decal":
            option.update({"checkout_provider": "decal", "minimum_amount_minor": None})
        if rail == "stablecoin":
            option.update({"status": "beta_scaffold", "live_settlement": False, "note": "External/runtime rail metadata only; no live settlement claim."})
        if rail in {"pay_sh", "x402"}:
            option.update({"execution_boundary": "agent_external_runtime", "arbitrary_url_execution": False})
        options.append(option)
    return options, recommended, stripe_minimum_applies


def build_tool_quote(tool: dict[str, Any], *, bot_id: str, external_user_id: str, arguments: dict[str, Any], amount_minor: int, currency: str = "USD", cost_breakdown: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    amount_minor = int(amount_minor)
    if amount_minor < 0:
        raise ValueError("amount_minor must be >= 0")
    currency = currency.upper()
    normalized_breakdown = normalize_cost_breakdown(cost_breakdown, amount_minor)
    request_hash = quote_hash({"tool_id": tool["tool_id"], "bot_id": bot_id, "external_user_id": external_user_id, "arguments": arguments, "amount_minor": amount_minor, "currency": currency, "cost_breakdown": normalized_breakdown})
    payment_options, recommended, stripe_minimum_applies = choose_payment_options(tool, amount_minor, currency)
    return {
        "tool_quote_id": f"tool_quote_{request_hash[:24]}",
        "quote_id": f"tool_quote_{request_hash[:24]}",
        "tool_id": tool["tool_id"],
        "amount_minor": amount_minor,
        "currency": currency,
        "request_hash": request_hash,
        "cost_breakdown": normalized_breakdown,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "payment_options": payment_options,
        "recommended_payment_rail": recommended,
        "stripe_minimum_applies": stripe_minimum_applies,
        "execution_mode": tool["execution_mode"],
        "provider_type": tool["provider_type"],
        "execution_caveat": tool["settlement_caveat"],
    }
