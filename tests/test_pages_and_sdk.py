import re

import httpx
from sqlmodel import Session

from examples import discord_bot_flow
from payjent.models import PaymentSession, Quote
from payjent.config import get_settings
from payjent.sdk import PayjentClient


def _create_checkout(client, quote_payload, bot_headers):
    quote = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    session = client.post(f"/api/v1/quotes/{quote['id']}/checkout", headers=bot_headers).json()
    return quote, session


def test_pay_page_shows_quote_and_public_mock_payment_cta(client, quote_payload, bot_headers):
    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)

    response = client.get(f"/pay/{payment_session['id']}")

    assert response.status_code == 200
    assert "Payjent checkout" in response.text
    assert "do a thing" in response.text
    assert "2.50 USD" in response.text
    assert "Complete payment" in response.text
    assert "Approve and pay 2.50 USD" in response.text
    assert "operator credentials" in response.text
    assert "curl -X POST" not in response.text
    assert "/api/v1/payment-sessions/" not in response.text
    assert payment_session["id"] in response.text


def test_pay_page_is_human_approval_document_without_tokens(client, quote_payload, bot_headers, operator_headers):
    action = client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()

    response = client.get(f"/pay/{action['payment_session_id']}")

    assert response.status_code == 200
    assert "Human approval document" in response.text
    assert "should this agent resume this exact paid action" in response.text
    assert "Auto-resume" in response.text
    assert "one-time grant" in response.text
    assert "Task budget" in response.text
    assert "Execution readiness" in response.text
    assert "Auto-resume" in response.text
    assert "refund by default" in response.text
    assert "Downstream rails" in response.text
    assert paid["grant"]["id"] not in response.text
    assert "payment_token" not in response.text


def test_browser_mock_pay_post_action_pays_and_redirects_without_tokens(client, quote_payload, bot_headers):
    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)
    pay_page = client.get(f"/pay/{payment_session['id']}")

    assert "Approve and pay 2.50 USD" in pay_page.text
    assert f'action="/pay/{payment_session["id"]}/mock-pay"' in pay_page.text

    response = client.post(f"/pay/{payment_session['id']}/mock-pay", follow_redirects=False)

    assert response.status_code in {302, 303}
    assert response.headers["location"] == f"/pay/{payment_session['id']}"

    paid_page = client.get(f"/pay/{payment_session['id']}")
    status = client.get(f"/status/{payment_session['id']}")
    bot_status = client.get(f"/api/v1/payment-sessions/{payment_session['id']}").json()

    assert paid_page.status_code == 200
    assert status.status_code == 200
    assert bot_status["status"] == "paid"
    assert "Paid — one-time grant issued" in paid_page.text
    assert "Access" in status.text
    assert "Access granted" in status.text
    for html in (paid_page.text, status.text):
        assert payment_session["id"] in html
        assert "payment_token" not in html
        assert not re.search(r"grant_[A-Za-z0-9_-]+", html)
        assert not re.search(r"eyJ[A-Za-z0-9_-]+", html)


def test_browser_mock_pay_cta_unavailable_when_runtime_env_is_production(client, quote_payload, bot_headers):
    settings = get_settings()
    original_env = settings.env
    original_mock_provider_enabled = settings.mock_provider_enabled
    try:
        settings.env = "production"
        settings.mock_provider_enabled = False
        quote = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
        response = client.post(f"/api/v1/quotes/{quote['id']}/checkout", headers=bot_headers)

        assert response.status_code == 503
        assert response.json()["detail"] == "active checkout provider not configured"
    finally:
        settings.env = original_env
        settings.mock_provider_enabled = original_mock_provider_enabled


def test_browser_mock_pay_endpoint_cannot_complete_existing_mock_session_in_production(client, quote_payload, bot_headers, engine):
    from sqlmodel import Session, select
    from payjent.models import Grant

    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)
    settings = get_settings()
    original_env = settings.env
    original_dev_mode = settings.dev_mode
    original_mock_provider_enabled = settings.mock_provider_enabled
    original_hosted_smoke = settings.hosted_smoke_test_rail_enabled
    try:
        settings.env = "production"
        settings.dev_mode = True
        settings.mock_provider_enabled = True
        settings.hosted_smoke_test_rail_enabled = True

        pay_page = client.get(f"/pay/{payment_session['id']}")
        response = client.post(f"/pay/{payment_session['id']}/mock-pay", follow_redirects=False)

        assert "Approve and pay 2.50 USD" not in pay_page.text
        assert response.status_code in {403, 404}
        unchanged = client.get(f"/api/v1/payment-sessions/{payment_session['id']}").json()
        assert unchanged["status"] == "checkout_created"
        with Session(engine) as session:
            grants = session.exec(select(Grant).where(Grant.quote_id == payment_session["quote_id"])).all()
        assert grants == []
    finally:
        settings.env = original_env
        settings.dev_mode = original_dev_mode
        settings.mock_provider_enabled = original_mock_provider_enabled
        settings.hosted_smoke_test_rail_enabled = original_hosted_smoke


def test_browser_mock_pay_returns_404_for_non_mock_provider(client, quote_payload, bot_headers, engine):
    from sqlmodel import Session
    from payjent.models import PaymentSession

    _quote, payment_session = _create_checkout(client, quote_payload, bot_headers)
    with Session(engine) as session:
        ps = session.get(PaymentSession, payment_session["id"])
        ps.provider = "link"
        session.add(ps)
        session.commit()

    pay_page = client.get(f"/pay/{payment_session['id']}")
    response = client.post(f"/pay/{payment_session['id']}/mock-pay", follow_redirects=False)

    assert "Approve and pay 2.50 USD" not in pay_page.text
    assert response.status_code == 404


def test_status_pages_show_payment_access_and_fulfillment_without_grant_id(client, quote_payload, bot_headers, operator_headers):
    quote, payment_session = _create_checkout(client, quote_payload, bot_headers)
    paid = client.post(f"/api/v1/payment-sessions/{payment_session['id']}/mock-pay", headers=operator_headers).json()
    client.post(f"/api/v1/grants/{paid['grant']['id']}/consume", headers=bot_headers, json={"bot_id": quote_payload["bot_id"]})
    client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {"message_id": "m1"}})

    index = client.get("/status")
    status = client.get(f"/status/{payment_session['id']}")

    assert index.status_code == 200
    assert "Payjent status" in index.text
    assert status.status_code == 200
    assert "Payment" in status.text
    assert "Payment complete" in status.text
    assert "payjent.com" in status.text
    assert "viewport" in status.text
    assert "paid" in status.text
    assert "Access" in status.text
    assert paid["grant"]["id"] not in status.text
    assert "Fulfilled" in status.text
    assert "Agent resumed" in status.text


def _assert_public_status_page_safe(html, paid=None):
    forbidden = [
        "payment_token",
        "provider_session_id",
        "callback payload",
        "request_hash",
        "idempotency",
        "api_key",
        "DATABASE_URL",
        "Traceback",
        "true",
        "false",
    ]
    for token in forbidden:
        assert token not in html
    assert not re.search(r"grant_[A-Za-z0-9_-]+", html)
    assert not re.search(r"eyJ[A-Za-z0-9_-]+", html)
    if paid is not None:
        assert paid["grant"]["id"] not in html


def test_status_page_lifecycle_labels_failure_refund_and_public_safety(client, quote_payload, bot_headers, operator_headers, engine):
    quote, payment_session = _create_checkout(client, quote_payload, bot_headers)

    awaiting = client.get(f"/status/{payment_session['id']}")
    assert awaiting.status_code == 200
    assert "Awaiting payment" in awaiting.text
    assert "Quoted" in awaiting.text
    _assert_public_status_page_safe(awaiting.text)

    paid = client.post(f"/api/v1/payment-sessions/{payment_session['id']}/mock-pay", headers=operator_headers).json()
    complete = client.get(f"/status/{payment_session['id']}")
    assert "Payment complete" in complete.text
    assert "Copy prompt" in complete.text
    assert f"session {payment_session['id']}" in complete.text
    assert "Return to your agent" in complete.text or "Tell your agent to resume" in complete.text
    _assert_public_status_page_safe(complete.text, paid)

    client.post(f"/api/v1/grants/{paid['grant']['id']}/consume", headers=bot_headers, json={"bot_id": quote_payload["bot_id"]})
    in_progress = client.get(f"/status/{payment_session['id']}")
    assert "Agent resumed" in in_progress.text
    assert "action in progress" in in_progress.text
    _assert_public_status_page_safe(in_progress.text, paid)

    client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {"message_id": "m2"}})
    succeeded = client.get(f"/status/{payment_session['id']}")
    assert "Fulfilled" in succeeded.text or "Succeeded" in succeeded.text
    assert "action succeeded" in succeeded.text or "action fulfilled" in succeeded.text
    _assert_public_status_page_safe(succeeded.text, paid)

    client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "failed", "metadata": {"reason": "expected-test-failure"}})
    failed = client.get(f"/status/{payment_session['id']}")
    assert "Action needs attention" in failed.text
    _assert_public_status_page_safe(failed.text, paid)

    with Session(engine) as session:
        ps = session.get(PaymentSession, payment_session["id"])
        q = session.get(Quote, quote["id"])
        ps.status = "refund_pending"
        q.status = "refund_requested"
        session.add(ps)
        session.add(q)
        session.commit()

    refund = client.get(f"/status/{payment_session['id']}")
    assert "Refund requested" in refund.text
    assert "pending" in refund.text.lower()
    _assert_public_status_page_safe(refund.text, paid)


def test_public_pay_and_status_hide_paid_payment_token_but_bot_polling_returns_it(client, quote_payload, bot_headers, operator_headers):
    action = client.post("/api/v1/agent-actions", json=quote_payload, headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{action['payment_session_id']}/mock-pay", headers=operator_headers).json()
    token = paid["grant"]["id"]

    pay_page = client.get(f"/pay/{action['payment_session_id']}")
    status_page = client.get(f"/status/{action['payment_session_id']}")
    bot_status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()

    assert pay_page.status_code == 200
    assert status_page.status_code == 200
    for html in (pay_page.text, status_page.text):
        assert token not in html
        assert "payment_token" not in html
        assert not re.search(r"grant_[A-Za-z0-9_-]+", html)
    assert "agent will resume automatically" in pay_page.text
    assert "Access granted" in status_page.text
    assert bot_status["payment_token"] == token
    assert bot_status["payment_token_status"] == "available"


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
