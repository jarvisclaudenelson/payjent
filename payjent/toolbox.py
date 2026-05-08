from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .money import quote_hash

STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY = {"USD": 50}

TRUSTED_PAYSH_EXECUTION_CAVEAT = (
    "Trusted allowlisted pay.sh/x402 metadata only. Payjent does not execute arbitrary URLs "
    "and does not claim live crypto settlement; the agent executes externally with its own "
    "funded pay.sh/x402 runtime after Payjent approval."
)

MANAGED_EXECUTION_CAVEAT = (
    "Managed provider action metadata only. Payjent quotes and gates a bounded task budget; "
    "provider execution happens in the agent/provider runtime without exposing sensitive values."
)

_TOOLBOX: dict[str, dict[str, Any]] = {
    "exa.deep_search": {
        "tool_id": "exa.deep_search",
        "display_name": "Exa Deep Search",
        "description": "Premium web research using Exa search APIs.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "num_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]},
        "pricing_model": "per_search",
        "base_amount_minor": 35,
        "min_amount_minor": 25,
        "max_amount_minor": 2000,
        "currency": "USD",
        "supported_payment_rails": ["task_budget", "stripe", "stablecoin"],
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
        "pricing_model": "per_page",
        "base_amount_minor": 20,
        "min_amount_minor": 10,
        "max_amount_minor": 1000,
        "currency": "USD",
        "supported_payment_rails": ["task_budget", "stripe", "stablecoin"],
        "recommended_payment_rail": "task_budget",
        "risk_level": "medium",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
    "fal.image.generate": {
        "tool_id": "fal.image.generate",
        "display_name": "FAL Image Generate",
        "description": "Generate an image using an agent-managed FAL provider integration.",
        "provider_type": "managed_api",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 4}}, "required": ["prompt"]},
        "pricing_model": "per_image",
        "base_amount_minor": 80,
        "min_amount_minor": 0,
        "max_amount_minor": 2000,
        "currency": "USD",
        "supported_payment_rails": ["task_budget", "stripe", "stablecoin"],
        "recommended_payment_rail": "stripe",
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
        "pricing_model": "per_request",
        "base_amount_minor": 45,
        "min_amount_minor": 30,
        "max_amount_minor": 3000,
        "currency": "USD",
        "supported_payment_rails": ["task_budget", "stripe", "stablecoin"],
        "recommended_payment_rail": "task_budget",
        "risk_level": "low",
        "execution_mode": "agent_managed_provider_runtime",
        "settlement_caveat": MANAGED_EXECUTION_CAVEAT,
    },
}

for tool_id, name, desc, amount in [
    ("paysh.fal_image", "Trusted pay.sh FAL Image", "Allowlisted pay.sh/x402 image generation metadata.", 35),
    ("paysh.web_scrape", "Trusted pay.sh Web Scrape", "Allowlisted pay.sh/x402 web scraping metadata.", 15),
    ("paysh.search", "Trusted pay.sh Search", "Allowlisted pay.sh/x402 search metadata.", 10),
    ("paysh.data_extract", "Trusted pay.sh Data Extract", "Allowlisted pay.sh/x402 data extraction metadata.", 25),
    ("paysh.file_convert", "Trusted pay.sh File Convert", "Allowlisted pay.sh/x402 file conversion metadata.", 20),
]:
    _TOOLBOX[tool_id] = {
        "tool_id": tool_id,
        "display_name": name,
        "description": desc,
        "provider_type": "trusted_paysh",
        "status": "enabled",
        "input_schema": {"type": "object", "properties": {"instructions": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["instructions"]},
        "pricing_model": "allowlisted_x402_microcharge",
        "base_amount_minor": amount,
        "min_amount_minor": amount,
        "max_amount_minor": 500,
        "currency": "USD",
        "supported_payment_rails": ["pay_sh", "x402", "task_budget", "stablecoin", "stripe"],
        "recommended_payment_rail": "pay_sh",
        "risk_level": "medium",
        "execution_mode": "external_trusted_paysh_x402_runtime",
        "trusted_metadata": {"allowlisted": True, "arbitrary_url_execution": False, "live_settlement_claim": False},
        "settlement_caveat": TRUSTED_PAYSH_EXECUTION_CAVEAT,
    }


def list_tools() -> list[dict[str, Any]]:
    return [public_tool(tool) for tool in _TOOLBOX.values()]


def get_tool(tool_id: str) -> dict[str, Any] | None:
    tool = _TOOLBOX.get(tool_id)
    return public_tool(tool) if tool else None


def public_tool(tool: dict[str, Any]) -> dict[str, Any]:
    # Pricing basis is public metadata; no credentials, grants, API keys, or executable URLs are included.
    return deepcopy(tool)


def compute_tool_amount(tool: dict[str, Any], arguments: dict[str, Any]) -> int:
    quantity = arguments.get("quantity", arguments.get("units", 1))
    try:
        quantity_int = int(quantity)
    except (TypeError, ValueError):
        quantity_int = 1
    quantity_int = max(1, min(quantity_int, 10))
    amount = int(tool["base_amount_minor"]) * quantity_int
    return max(int(tool["min_amount_minor"]), min(amount, int(tool["max_amount_minor"])))


def choose_payment_options(tool: dict[str, Any], amount_minor: int, currency: str) -> tuple[list[dict[str, Any]], str, bool]:
    currency = currency.upper()
    stripe_min = STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY.get(currency, 50)
    stripe_minimum_applies = amount_minor < stripe_min
    trusted = tool["provider_type"] in {"trusted_paysh", "trusted_x402"}
    recommended = "pay_sh" if trusted and amount_minor < stripe_min else ("task_budget" if amount_minor < stripe_min else "stripe")
    options: list[dict[str, Any]] = []
    for rail in tool["supported_payment_rails"]:
        option = {"rail": rail, "status": "available", "recommended": rail == recommended}
        if rail == "stripe" and stripe_minimum_applies:
            option.update({"status": "unavailable", "reason": "minimum_applies", "fallback_requires_bundle": True, "minimum_amount_minor": stripe_min})
        if rail == "stablecoin":
            option.update({"status": "beta_scaffold", "live_settlement": False, "note": "External/runtime rail metadata only; no live settlement claim."})
        if rail in {"pay_sh", "x402"}:
            option.update({"execution_boundary": "agent_external_runtime", "arbitrary_url_execution": False})
        options.append(option)
    return options, recommended, stripe_minimum_applies


def build_tool_quote(tool: dict[str, Any], *, bot_id: str, external_user_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    amount_minor = compute_tool_amount(tool, arguments)
    currency = tool["currency"].upper()
    request_hash = quote_hash({"tool_id": tool["tool_id"], "bot_id": bot_id, "external_user_id": external_user_id, "arguments": arguments, "amount_minor": amount_minor, "currency": currency})
    payment_options, recommended, stripe_minimum_applies = choose_payment_options(tool, amount_minor, currency)
    return {
        "tool_quote_id": f"tool_quote_{request_hash[:24]}",
        "quote_id": f"tool_quote_{request_hash[:24]}",
        "tool_id": tool["tool_id"],
        "amount_minor": amount_minor,
        "currency": currency,
        "request_hash": request_hash,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "payment_options": payment_options,
        "recommended_payment_rail": recommended,
        "stripe_minimum_applies": stripe_minimum_applies,
        "execution_mode": tool["execution_mode"],
        "provider_type": tool["provider_type"],
        "execution_caveat": tool["settlement_caveat"],
    }
