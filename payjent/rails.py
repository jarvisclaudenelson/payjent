"""Spend rail catalog and normalization helpers.

This module intentionally validates only new spend authorization writes. Historical
ledger rows may contain free-form rail labels from earlier versions.
"""

from .settlement_rails import settlement_spend_rails, normalize_settlement_rail

SUPPORTED_SPEND_RAILS: tuple[str, ...] = (
    "stripe_funding",
    "x402_payment",
    "link_credential",
    "card_credential",
    *settlement_spend_rails(),
)

_SPEND_RAIL_ALIASES: dict[str, str] = {
    "stripe": "stripe_funding",
    "x402": "x402_payment",
    "coinbase_x402": "x402_cdp",
    "circle": "circle_nanopayments",
    "mpp": "stripe_machine_payments",
    "stripe_mpp": "stripe_machine_payments",
    "ap2": "google_ap2",
    "crossmint": "crossmint_wallet",
    "moonpay": "moonpay_agents",
    "visa": "visa_tap",
}


def normalize_spend_rail(rail: str) -> str:
    """Return the canonical spend rail name or raise ValueError.

    Normalization is intentionally small and backwards-compatible: legacy API
    callers can keep sending ``x402`` and ``stripe`` while new ledger entries are
    stored using canonical rail-neutral categories.
    """

    normalized = rail.strip().lower().replace("-", "_")
    canonical = _SPEND_RAIL_ALIASES.get(normalized, normalized)
    if canonical not in SUPPORTED_SPEND_RAILS:
        try:
            canonical = normalize_settlement_rail(canonical)
        except ValueError:
            supported = ", ".join(SUPPORTED_SPEND_RAILS)
            aliases = ", ".join(f"{alias}->{target}" for alias, target in sorted(_SPEND_RAIL_ALIASES.items()))
            raise ValueError(f"unsupported spend rail '{rail}'; supported rails: {supported}; aliases: {aliases}")
    return canonical


def is_supported_spend_rail(rail: str) -> bool:
    try:
        normalize_spend_rail(rail)
    except ValueError:
        return False
    return True
