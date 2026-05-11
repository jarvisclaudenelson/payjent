import json

from payjent.toolbox import build_tool_quote, get_tool


def _quote_payload(arguments=None):
    return {"bot_id": "bot-1", "external_user_id": "user-1", "arguments": arguments or {"query": "micropayments"}, "amount_minor": 35, "currency": "USD"}


def _assert_no_secrets(value):
    serialized = json.dumps(value).lower()
    forbidden = ["api_key", "apikey", "secret", "token", "authorization", "cookie", "private_key", "grant"]
    assert not any(word in serialized for word in forbidden)


def test_toolbox_list_and_detail_are_public_and_secret_free(client):
    listing = client.get("/api/v1/toolbox")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] >= 9
    tool_ids = {tool["tool_id"] for tool in body["tools"]}
    assert {"exa.deep_search", "firecrawl.scrape", "fal.image.generate", "elevenlabs.text_to_speech", "paysh.search"}.issubset(tool_ids)
    _assert_no_secrets(body)
    serialized = json.dumps(body)
    assert "base_amount_minor" not in serialized
    assert "min_amount_minor" not in serialized
    assert "max_amount_minor" not in serialized

    detail = client.get("/api/v1/toolbox/paysh.search")
    assert detail.status_code == 200
    tool = detail.json()
    assert tool["provider_type"] == "trusted_paysh"
    assert tool["trusted_metadata"]["allowlisted"] is True
    assert tool["trusted_metadata"]["arbitrary_url_execution"] is False
    assert tool["trusted_metadata"]["live_settlement_claim"] is False
    _assert_no_secrets(tool)


def test_toolbox_unknown_tool_404(client, bot_headers):
    assert client.get("/api/v1/toolbox/not.real").status_code == 404
    assert client.post("/api/v1/toolbox/not.real/quote", json=_quote_payload(), headers=bot_headers).status_code == 404


def test_toolbox_quote_requires_bot_auth(client):
    response = client.post("/api/v1/toolbox/exa.deep_search/quote", json=_quote_payload())
    assert response.status_code in {401, 403}


def test_toolbox_quote_enforces_bot_scope(client, bot_headers):
    response = client.post(
        "/api/v1/toolbox/exa.deep_search/quote",
        json={"bot_id": "different-bot", "external_user_id": "user-1", "arguments": {"query": "scope check"}, "amount_minor": 35, "currency": "USD"},
        headers=bot_headers,
    )
    assert response.status_code == 403


def test_toolbox_public_metadata_has_no_executable_urls(client):
    body = client.get("/api/v1/toolbox").json()
    serialized = json.dumps(body).lower()
    forbidden_fields = ["target_url", "service_url", "callback_url", "webhook_url", "api_url"]
    assert not any(field in serialized for field in forbidden_fields)


def test_sub_50_managed_quote_has_no_stripe_minimum_or_topup(client, bot_headers):
    response = client.post("/api/v1/toolbox/exa.deep_search/quote", json={**_quote_payload({"query": "small search"}), "amount_minor": 10}, headers=bot_headers)
    assert response.status_code == 200
    quote = response.json()
    assert quote["amount_minor"] < 50
    assert quote["provider_type"] == "managed_api"
    assert quote["recommended_payment_rail"] == "task_budget"
    assert quote["stripe_minimum_applies"] is False
    assert "stripe" not in {option["rail"] for option in quote["payment_options"]}
    decal = next(option for option in quote["payment_options"] if option["rail"] == "decal")
    assert decal["status"] == "available"
    assert decal["minimum_amount_minor"] is None


def test_trusted_paysh_sub_50_quote_recommends_pay_sh_or_x402(client, bot_headers):
    response = client.post("/api/v1/toolbox/paysh.search/quote", json={**_quote_payload({"instructions": "search latest docs"}), "amount_minor": 10}, headers=bot_headers)
    assert response.status_code == 200
    quote = response.json()
    assert quote["amount_minor"] < 50
    assert quote["provider_type"] == "trusted_paysh"
    assert quote["recommended_payment_rail"] in {"pay_sh", "x402"}
    assert any(option["rail"] in {"pay_sh", "x402"} and option["recommended"] for option in quote["payment_options"])
    assert "does not execute arbitrary URLs" in quote["execution_caveat"]


def test_fal_managed_quote_uses_decal_without_stripe_minimum(client, bot_headers):
    response = client.post("/api/v1/toolbox/fal.image.generate/quote", json={**_quote_payload({"prompt": "a robot", "quantity": 1}), "amount_minor": 80}, headers=bot_headers)
    assert response.status_code == 200
    quote = response.json()
    assert quote["amount_minor"] == 80
    decal = next(option for option in quote["payment_options"] if option["rail"] == "decal")
    assert decal["status"] == "available"
    assert quote["recommended_payment_rail"] == "decal"
    assert decal["recommended"] is True
    assert quote["stripe_minimum_applies"] is False


def test_fal_runtime_amount_can_be_sub_50_and_uses_decal_without_minimum():
    tool = get_tool("fal.image.generate")
    assert tool is not None
    assert "min_amount_minor" not in tool
    assert tool["pricing_source"] == "agent_runtime"

    quote = build_tool_quote(tool, bot_id="bot-1", external_user_id="user-1", arguments={"prompt": "a robot", "quantity": 1}, amount_minor=10)

    assert quote["amount_minor"] == 10
    assert quote["recommended_payment_rail"] == "decal"
    assert quote["stripe_minimum_applies"] is False
    assert "stripe" not in {option["rail"] for option in quote["payment_options"]}
    decal = next(option for option in quote["payment_options"] if option["rail"] == "decal")
    assert decal["minimum_amount_minor"] is None


def test_stablecoin_option_is_scaffold_not_live_settlement(client, bot_headers):
    response = client.post("/api/v1/toolbox/exa.deep_search/quote", json={**_quote_payload({"query": "small search"}), "amount_minor": 10}, headers=bot_headers)
    assert response.status_code == 200
    stablecoin = next(option for option in response.json()["payment_options"] if option["rail"] == "stablecoin")
    assert stablecoin["status"] == "beta_scaffold"
    assert stablecoin["live_settlement"] is False
    assert "no live settlement claim" in stablecoin["note"]


def test_toolbox_request_hash_changes_when_arguments_change(client, bot_headers):
    one = client.post("/api/v1/toolbox/exa.deep_search/quote", json=_quote_payload({"query": "alpha"}), headers=bot_headers).json()
    two = client.post("/api/v1/toolbox/exa.deep_search/quote", json=_quote_payload({"query": "beta"}), headers=bot_headers).json()
    assert one["request_hash"] != two["request_hash"]
    assert one["tool_quote_id"] != two["tool_quote_id"]


def test_well_known_manifest_exposes_toolbox_url_or_count(client):
    response = client.get("/.well-known/payjent-tools.json")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["toolbox_url"] == "https://payjent.com/api/v1/toolbox"
    assert manifest["toolbox_tool_count"] >= 9
    toolbox_tool_ids = {tool.get("tool_id") for tool in manifest["tools"] if tool.get("tool_id")}
    assert "fal.image.generate" in toolbox_tool_ids
    fal_descriptor = next(tool for tool in manifest["tools"] if tool.get("tool_id") == "fal.image.generate")
    assert fal_descriptor["endpoint"] == "/api/v1/toolbox/fal.image.generate"
    assert fal_descriptor["execution_mode"] == "agent_managed_provider_runtime"
