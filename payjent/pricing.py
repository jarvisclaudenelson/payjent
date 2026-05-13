from __future__ import annotations

from typing import Any

OPERATOR_FEE_LABEL_MARKERS = (
    "operator fee",
    "agent operator",
    "service fee",
    "agent fee",
)
PAYJENT_FEE_LABEL_MARKERS = (
    "payjent fee",
    "payjent platform fee",
    "platform fee",
)
PROVIDER_LABEL_MARKERS = (
    "provider",
    "merchant",
    "quoted total",
    "exact quote",
    "quote",
    "runtime",
    "premium",
    "x402",
    "pay.sh",
)

FEE_POLICY: dict[str, Any] = {
    "operator_fees": "optional_explicit_line_items_only",
    "requirements": [
        "fees must be explicit line items in cost_breakdown",
        "total amount_minor must equal provider/merchant exact quote plus explicit operator fee line items",
        "operator fees are not provider prices and must be labeled separately",
        "fail closed if amount_minor and cost_breakdown mismatch",
        "no default or hidden fees are added for existing agents",
        "future settlement/payout is ledgered separately; this slice records transparent fee allocation metadata only",
    ],
    "operator_fee_label_examples": list(OPERATOR_FEE_LABEL_MARKERS),
    "hidden_fee_behavior": "unlabeled extras are not treated as operator fees and no fees are injected silently",
    "secrets": "public_policy_only_no_secrets",
}


def _item_label(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("label", ""))
    return str(getattr(item, "label", ""))


def _item_amount(item: Any) -> int:
    if isinstance(item, dict):
        return int(item.get("amount_minor", 0))
    return int(getattr(item, "amount_minor"))


def _matches(label: str, markers: tuple[str, ...]) -> bool:
    normalized = label.lower()
    return any(marker in normalized for marker in markers)


def classify_cost_breakdown(cost_breakdown: list[Any]) -> dict[str, Any]:
    """Classify transparent cost_breakdown lines into non-secret allocation metadata."""
    allocation = {
        "provider_merchant_subtotal_minor": 0,
        "operator_fee_subtotal_minor": 0,
        "payjent_platform_fee_subtotal_minor": 0,
        "other_subtotal_minor": 0,
        "total_minor": 0,
        "currency": None,
        "line_items": [],
        "policy": {
            "operator_fees_must_be_explicit": True,
            "no_hidden_or_default_fees": True,
            "amount_breakdown_mismatch_behavior": "fail_closed",
            "settlement": "metadata_only_future_ledgered_separately",
        },
    }
    for item in cost_breakdown:
        label = _item_label(item)
        amount = _item_amount(item)
        if _matches(label, OPERATOR_FEE_LABEL_MARKERS):
            category = "operator_fee"
            allocation["operator_fee_subtotal_minor"] += amount
        elif _matches(label, PAYJENT_FEE_LABEL_MARKERS):
            category = "payjent_platform_fee"
            allocation["payjent_platform_fee_subtotal_minor"] += amount
        elif _matches(label, PROVIDER_LABEL_MARKERS):
            category = "provider_merchant"
            allocation["provider_merchant_subtotal_minor"] += amount
        else:
            category = "other"
            allocation["other_subtotal_minor"] += amount
        allocation["total_minor"] += amount
        allocation["line_items"].append({"label": label, "amount_minor": amount, "category": category})
    return allocation


def attach_pricing_allocation(execution_envelope: dict[str, Any] | None, cost_breakdown: list[Any]) -> dict[str, Any]:
    envelope = dict(execution_envelope or {})
    payjent_pricing = dict(envelope.get("payjent_pricing") or {})
    payjent_pricing["pricing_allocation"] = classify_cost_breakdown(cost_breakdown)
    envelope["payjent_pricing"] = payjent_pricing
    return envelope


def pricing_allocation_from_envelope(execution_envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    pricing = (execution_envelope or {}).get("payjent_pricing") or {}
    allocation = pricing.get("pricing_allocation")
    return allocation if isinstance(allocation, dict) else None
