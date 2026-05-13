import json


def _flatten_strings(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _flatten_strings(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_strings(item)
    elif value is not None:
        yield str(value)


def test_public_status_is_safe_operational_summary(client):
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    data = response.json()

    assert data["product"] == "Payjent"
    assert data["public_base_url"] == "https://payjent.com"
    assert data["checkout"]["provider_safe_mode"] is True
    assert data["checkout"]["provider"] == "mock"
    assert data["toolbox_count"] > 0
    assert data["premium_preset_count"] >= 6
    assert set(data["managed_provider_configured"]) == {"fal", "exa", "firecrawl", "elevenlabs"}
    assert all(isinstance(v, bool) for v in data["managed_provider_configured"].values())
    assert data["database_mode"] in {"sqlite", "postgres", "unknown"}
    assert data["exact_quote_policy"]["rule"] == "exact_provider_quote_required"
    assert data["exact_quote_policy"]["unknown_price_behavior"] == "fail_closed_await_exact_provider_quote"
    assert data["production_guardrails"]["safe"] is True

    body = json.dumps(data).lower()
    forbidden = [
        "secret",
        "token",
        "api_key",
        "apikey",
        "password",
        "payjent_database_url",
        "sqlite://",
        "postgres://",
        "postgresql://",
        "localhost",
        "vercel",
        "stripe minimum",
        "top-up",
        "top up",
    ]
    for marker in forbidden:
        assert marker not in body


def test_pytest_forces_safe_env_even_if_shell_is_hostile(client):
    data = client.get("/api/v1/status").json()
    assert data["public_base_url"] == "https://payjent.com"
    assert data["checkout"]["provider"] == "mock"
    assert data["database_mode"] == "sqlite"
