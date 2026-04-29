from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


_DISALLOWED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("credential theft", "credential theft requests are not allowed"),
    ("steal password", "credential theft requests are not allowed"),
    ("password stealer", "credential theft requests are not allowed"),
    ("phishing", "phishing requests are not allowed"),
    ("phish", "phishing requests are not allowed"),
    ("malware", "malware requests are not allowed"),
    ("ransomware", "malware requests are not allowed"),
    ("keylogger", "malware requests are not allowed"),
    ("illegal harm", "illegal harm requests are not allowed"),
    ("make a bomb", "illegal harm requests are not allowed"),
    ("credit card dump", "financial abuse requests are not allowed"),
)


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    return str(value)


def assess_checkout_risk(request_summary: str, execution_envelope: dict[str, Any]) -> RiskDecision:
    text = f"{request_summary} {_flatten(execution_envelope)}".lower()
    for pattern, reason in _DISALLOWED_PATTERNS:
        if pattern in text:
            return RiskDecision(False, reason)
    return RiskDecision(True)
