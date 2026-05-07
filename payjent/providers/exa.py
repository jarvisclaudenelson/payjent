from __future__ import annotations

from typing import Any, Callable

import httpx

EXA_DEEP_SEARCH_URL = "https://api.exa.ai/search"
MAX_QUERY_LENGTH = 500
MAX_RESULT_TEXT_LENGTH = 1000


class ExaProviderNotConfigured(RuntimeError):
    pass


class ExaProviderError(RuntimeError):
    pass


Transport = Callable[[dict[str, Any], str], dict[str, Any]]


def _is_secret_like_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    disallowed_secret_keys = {"api_key", "apikey", "authorization", "token", "secret", "password"}
    return normalized in disallowed_secret_keys


def _contains_secret_like_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_is_secret_like_key(key) or _contains_secret_like_key(nested) for key, nested in value.items())
    if isinstance(value, list):
        return any(_contains_secret_like_key(item) for item in value)
    return False


def validate_deep_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if _contains_secret_like_key(arguments):
        raise ValueError("provider credentials are not accepted in toolbox arguments")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
    raw_num_results = arguments.get("num_results", 5)
    try:
        num_results = int(raw_num_results)
    except (TypeError, ValueError):
        raise ValueError("num_results must be an integer") from None
    if num_results < 1 or num_results > 10:
        raise ValueError("num_results must be between 1 and 10")
    return {"query": query, "num_results": num_results}


def _default_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    response = httpx.post(
        EXA_DEEP_SEARCH_URL,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={
            "query": payload["query"],
            "numResults": payload["num_results"],
            "type": "auto",
            "contents": {"text": {"maxCharacters": MAX_RESULT_TEXT_LENGTH}},
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ExaProviderError("provider returned an invalid response")
    return data


def _safe_str(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def sanitize_exa_response(data: dict[str, Any], *, limit: int) -> dict[str, Any]:
    raw_results = data.get("results") if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []
    results: list[dict[str, Any]] = []
    for item in raw_results[:limit]:
        if not isinstance(item, dict):
            continue
        safe: dict[str, Any] = {}
        for key, max_len in (("title", 300), ("url", 1000), ("publishedDate", 64), ("author", 200), ("text", MAX_RESULT_TEXT_LENGTH), ("snippet", MAX_RESULT_TEXT_LENGTH)):
            value = _safe_str(item.get(key), max_len)
            if value is not None:
                safe["published_date" if key == "publishedDate" else key] = value
        score = item.get("score")
        if isinstance(score, (int, float)):
            safe["score"] = float(score)
        if safe:
            results.append(safe)
    return {"provider": "exa", "tool_id": "exa.deep_search", "result_count": len(results), "results": results}


def run_deep_search(arguments: dict[str, Any], *, api_key: str | None, transport: Transport | None = None) -> dict[str, Any]:
    if not api_key:
        raise ExaProviderNotConfigured("provider_not_configured")
    payload = validate_deep_search_arguments(arguments)
    try:
        data = (transport or _default_transport)(payload, api_key)
    except ExaProviderNotConfigured:
        raise
    except Exception as exc:
        raise ExaProviderError("provider_execution_failed") from exc
    return sanitize_exa_response(data, limit=payload["num_results"])
