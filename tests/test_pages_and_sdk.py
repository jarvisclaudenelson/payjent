import httpx

from examples import discord_bot_flow
from payjent.sdk import PayjentClient


def _create_checkout(client, quote_payload, bot_headers):
    quote = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    session = client.post(f"/api/v1/quotes/{quote['id']}/checkout", headers=bot_headers).json()
    return quote, session


def test_pay_page_shows_quote_and_authenticated_dev_mock_instructions(client, quote_payload, bot_headers):
    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)

    response = client.get(f"/pay/{payment_session['id']}")

    assert response.status_code == 200
    assert "Payjent checkout" in response.text
    assert "do a thing" in response.text
    assert "2.50 USD" in response.text
    assert "Dev mock payment" in response.text
    assert "curl -X POST" in response.text
    assert "/api/v1/payment-sessions/" in response.text
    assert payment_session["id"] in response.text


def test_browser_mock_pay_post_action_is_not_available(client, quote_payload, bot_headers):
    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)

    response = client.post(f"/pay/{payment_session['id']}/mock-pay")
    status = client.get(f"/status/{payment_session['id']}")

    assert response.status_code == 404
    assert "checkout_created" in status.text


def test_status_pages_show_payment_grant_and_fulfillment(client, quote_payload, bot_headers, operator_headers):
    quote, payment_session = _create_checkout(client, quote_payload, bot_headers)
    paid = client.post(f"/api/v1/payment-sessions/{payment_session['id']}/mock-pay", headers=operator_headers).json()
    client.post(f"/api/v1/grants/{paid['grant']['id']}/consume", headers=bot_headers, json={"bot_id": quote_payload["bot_id"]})
    client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {"message_id": "m1"}})

    index = client.get("/status")
    status = client.get(f"/status/{payment_session['id']}")

    assert index.status_code == 200
    assert "Payjent status" in index.text
    assert status.status_code == 200
    assert "Payment status" in status.text
    assert "paid" in status.text
    assert paid["grant"]["id"] in status.text
    assert "fulfilled" in status.text


def test_example_module_imports():
    assert callable(discord_bot_flow.main)


def test_sdk_helper_methods_use_expected_http_calls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization"), request.headers.get("idempotency-key")))
        if request.url.path == "/api/v1/quotes" and request.method == "POST":
            return httpx.Response(200, json={"id": "quote_1", "request_hash": "hash-1"})
        if request.url.path == "/api/v1/quotes/quote_1" and request.method == "GET":
            return httpx.Response(200, json={"id": "quote_1"})
        if request.url.path == "/api/v1/quotes/quote_1/checkout" and request.method == "POST":
            return httpx.Response(200, json={"id": "ps_1", "quote_id": "quote_1"})
        if request.url.path == "/api/v1/grants/grant_1/verify" and request.method == "POST":
            return httpx.Response(200, json={"valid": True, "grant_id": "grant_1", "consumed": False, "payload": {}})
        if request.url.path == "/api/v1/grants/grant_1/consume" and request.method == "POST":
            return httpx.Response(200, json={"valid": True, "grant_id": "grant_1", "consumed": True, "payload": {}})
        if request.url.path == "/api/v1/quotes/quote_1/fulfillment" and request.method == "POST":
            return httpx.Response(200, json={"id": "ful_1", "quote_id": "quote_1", "status": "fulfilled", "metadata": {}})
        return httpx.Response(404, json={"detail": "not found"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
    sdk = PayjentClient(base_url="http://testserver", api_key="bot-key", client=http_client)

    quote = sdk.create_quote(
        bot_id="bot",
        external_user_id="user",
        request_summary="work",
        amount_minor=100,
        currency="USD",
        cost_breakdown=[{"label": "work", "amount_minor": 100}],
    )
    assert quote["id"] == "quote_1"
    assert sdk.get_quote("quote_1")["id"] == "quote_1"
    assert sdk.create_checkout("quote_1", idempotency_key="idem-1")["id"] == "ps_1"
    assert sdk.verify_grant("grant_1", bot_id="bot")["valid"] is True
    assert sdk.consume_grant("grant_1", bot_id="bot")["consumed"] is True
    assert sdk.record_fulfillment("quote_1", "fulfilled")["status"] == "fulfilled"
    assert all(call[2] == "Bearer bot-key" for call in calls if call[1] != "/api/v1/quotes/quote_1")
    assert ("POST", "/api/v1/quotes/quote_1/checkout", "Bearer bot-key", "idem-1") in calls
