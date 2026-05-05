import hashlib
import json
from typing import Any


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_breakdown(amount_minor: int, cost_breakdown: list[Any]) -> None:
    total = 0
    for item in cost_breakdown:
        total += int(item.amount_minor if hasattr(item, "amount_minor") else item["amount_minor"])
    if total != amount_minor:
        raise ValueError("cost_breakdown total must equal amount_minor")


def quote_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
