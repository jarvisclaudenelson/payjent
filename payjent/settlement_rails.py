"""Settlement rail catalog for Payjent's agent spend-control layer.

Payjent is the policy/readiness/ledger control plane. These rail manifests do
not imply Payjent custody or live settlement by themselves; they describe which
external rail an agent may spend through once a human-approved task budget/grant
exists and readiness checks pass.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

TOP_SETTLEMENT_RAILS: tuple[str, ...] = (
    "circle_nanopayments",
    "x402_cdp",
    "stripe_machine_payments",
    "google_ap2",
    "crossmint_wallet",
    "moonpay_agents",
    "visa_tap",
)

SETTLEMENT_RAILS: dict[str, dict[str, Any]] = {
    "circle_nanopayments": {
        "rail": "circle_nanopayments",
        "display_name": "Circle Nanopayments",
        "category": "stablecoin_nanopayment",
        "status": "available_when_configured",
        "mode": "external_runtime",
        "spend_authorization_rail": "circle_nanopayments",
        "currencies": ["USDC"],
        "networks": ["circle_gateway"],
        "min_amount_minor": 0,
        "min_amount_decimal": "0.000001",
        "supports_microspend": True,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": True,
        "requires_human_checkout": False,
        "readiness_checks": ["gateway_balance", "signing_capability", "supported_chain", "merchant_address"],
        "execution_boundary": "agent_or_runtime signs EIP-3009/Gateway authorization; Payjent records budget, grant, spend authorization, and fulfillment evidence only.",
    },
    "x402_cdp": {
        "rail": "x402_cdp",
        "display_name": "Coinbase CDP x402",
        "category": "http_402_stablecoin",
        "status": "available_when_configured",
        "mode": "external_runtime",
        "spend_authorization_rail": "x402_cdp",
        "currencies": ["USDC"],
        "networks": ["base", "base_sepolia", "polygon", "arbitrum", "world", "solana", "solana_devnet"],
        "facilitator_url": "https://api.cdp.coinbase.com/platform/v2/x402",
        "min_amount_minor": 1,
        "min_amount_decimal": "0.01",
        "supports_microspend": True,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": True,
        "requires_human_checkout": False,
        "readiness_checks": ["wallet", "network", "facilitator", "resource_402_discovery"],
        "execution_boundary": "agent pays the HTTP 402 resource through its configured wallet/facilitator; Payjent does not satisfy downstream 402 challenges with Stripe checkout.",
    },
    "stripe_machine_payments": {
        "rail": "stripe_machine_payments",
        "display_name": "Stripe Machine Payments / MPP",
        "category": "machine_payment",
        "status": "frontier_available_when_configured",
        "mode": "external_runtime",
        "spend_authorization_rail": "stripe_machine_payments",
        "currencies": ["USDC", "stripe_supported_fiat"],
        "networks": ["base_x402", "solana_mpp", "tempo_mpp", "stripe_card_networks_mpp"],
        "min_amount_minor": 1,
        "min_amount_decimal": "0.01",
        "supports_microspend": True,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": True,
        "requires_human_checkout": False,
        "readiness_checks": ["stripe_machine_payments_enabled", "wallet_or_mpp_session", "merchant_endpoint"],
        "execution_boundary": "agent uses Stripe-supported MPP/x402/card-network machine payment; Payjent controls budget and records reconciliation metadata.",
    },
    "google_ap2": {
        "rail": "google_ap2",
        "display_name": "Google AP2 Mandates",
        "category": "authorization_mandate",
        "status": "compatible_scaffold",
        "mode": "mandate_layer",
        "spend_authorization_rail": "google_ap2",
        "currencies": ["rail_dependent"],
        "networks": ["cards", "stablecoins", "crypto", "bank_transfers"],
        "min_amount_minor": 0,
        "min_amount_decimal": "rail_dependent",
        "supports_microspend": False,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": False,
        "requires_human_checkout": "mandate_dependent",
        "readiness_checks": ["intent_mandate", "cart_or_execution_mandate", "payment_rail_binding"],
        "execution_boundary": "AP2 proves intent/accountability; settlement still routes through a concrete rail such as x402, Stripe, Circle, card networks, or bank transfer.",
    },
    "crossmint_wallet": {
        "rail": "crossmint_wallet",
        "display_name": "Crossmint Agent Wallet/Card",
        "category": "wallet_card_orchestration",
        "status": "available_when_configured",
        "mode": "external_provider",
        "spend_authorization_rail": "crossmint_wallet",
        "currencies": ["stablecoins", "fiat"],
        "networks": ["x402", "visa_virtual_card", "stablecoin_wallet"],
        "min_amount_minor": 1,
        "min_amount_decimal": "provider_dependent",
        "supports_microspend": True,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": True,
        "requires_human_checkout": False,
        "readiness_checks": ["crossmint_agent_wallet", "spend_limits", "merchant_or_resource", "approval_policy"],
        "execution_boundary": "Crossmint provides wallet/card execution and guardrails; Payjent remains source of budget, grant, and fulfillment/refund ledger.",
    },
    "moonpay_agents": {
        "rail": "moonpay_agents",
        "display_name": "MoonPay Agents / MoonAgents Card",
        "category": "wallet_onramp_card",
        "status": "available_when_configured",
        "mode": "external_provider",
        "spend_authorization_rail": "moonpay_agents",
        "currencies": ["stablecoins", "fiat"],
        "networks": ["moonpay_wallet", "virtual_account", "mastercard_card"],
        "min_amount_minor": 1,
        "min_amount_decimal": "provider_dependent",
        "supports_microspend": False,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": True,
        "requires_human_checkout": "kyc_or_card_dependent",
        "readiness_checks": ["moonpay_agent_wallet", "virtual_account_or_card", "kyc_if_required", "spend_policy"],
        "execution_boundary": "MoonPay handles wallet/onramp/card execution; Payjent authorizes bounded spend and records non-secret provider evidence.",
    },
    "visa_tap": {
        "rail": "visa_tap",
        "display_name": "Visa Trusted Agent Protocol",
        "category": "card_network_agent_trust",
        "status": "partner_or_sandbox_required",
        "mode": "trust_layer",
        "spend_authorization_rail": "visa_tap",
        "currencies": ["card_network_supported"],
        "networks": ["visa"],
        "min_amount_minor": 1,
        "min_amount_decimal": "card_network_dependent",
        "supports_microspend": False,
        "supports_budget_reservation": True,
        "supports_auto_resume": True,
        "requires_wallet": False,
        "requires_human_checkout": "merchant_dependent",
        "readiness_checks": ["trusted_agent_identity", "consumer_visibility", "merchant_checkout", "payment_binding"],
        "execution_boundary": "Visa TAP helps merchants trust agent traffic and card intent; Payjent supplies mandate/budget/grant policy and records fulfillment evidence.",
    },
}

ALIASES: dict[str, str] = {
    "circle": "circle_nanopayments",
    "circle_gateway": "circle_nanopayments",
    "nanopayments": "circle_nanopayments",
    "x402_payment": "x402_cdp",
    "coinbase_x402": "x402_cdp",
    "cdp_x402": "x402_cdp",
    "mpp": "stripe_machine_payments",
    "stripe_mpp": "stripe_machine_payments",
    "stripe_machine": "stripe_machine_payments",
    "ap2": "google_ap2",
    "google_mandates": "google_ap2",
    "crossmint": "crossmint_wallet",
    "moonpay": "moonpay_agents",
    "moonagents": "moonpay_agents",
    "visa": "visa_tap",
    "tap": "visa_tap",
}


def normalize_settlement_rail(rail: str) -> str:
    normalized = rail.strip().lower().replace("-", "_")
    canonical = ALIASES.get(normalized, normalized)
    if canonical not in SETTLEMENT_RAILS:
        supported = ", ".join(SETTLEMENT_RAILS)
        aliases = ", ".join(f"{alias}->{target}" for alias, target in sorted(ALIASES.items()))
        raise ValueError(f"unsupported settlement rail '{rail}'; supported rails: {supported}; aliases: {aliases}")
    return canonical


def settlement_rail_manifest(rail: str) -> dict[str, Any]:
    return deepcopy(SETTLEMENT_RAILS[normalize_settlement_rail(rail)])


def list_settlement_rail_manifests() -> list[dict[str, Any]]:
    return [settlement_rail_manifest(rail) for rail in TOP_SETTLEMENT_RAILS]


def settlement_spend_rails() -> tuple[str, ...]:
    return tuple(manifest["spend_authorization_rail"] for manifest in list_settlement_rail_manifests())
