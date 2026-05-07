import json

from sqlmodel import Session, select

from payjent.models import PaymentSession, Quote, ToolExecution


def _payload(arguments=None, **extra):
    body = {"bot_id": "bot-1", "external_user_id": "user-1", "arguments": arguments or {"prompt": "a robot", "quantity": 1}}
    body.update(extra)
    return body


def _assert_no_secret_value(value, secret="payjent-test-secret-value-never-return"):
    serialized = json.dumps(value).lower()
    assert secret not in serialized
    for marker in ("target_url", "service_url", "callback", "webhook", "api_url"):
        assert marker not in serialized


def test_toolbox_checkout_fal_creates_quote_and_payment_session(client, bot_headers, engine):
    response = client.post(
        "/api/v1/toolbox/fal.image.generate/checkout",
        json=_payload({"prompt": "a robot", "quantity": 1}),
        headers={**bot_headers, "Idempotency-Key": "toolbox-fal-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "checkout_created"
    assert body["toolbox_quote"]["amount_minor"] == 80
    assert body["quote"]["request_summary"] == "Toolbox action: fal.image.generate"
    assert body["quote"]["execution_envelope"]["tool_id"] == "fal.image.generate"
    assert body["quote"]["execution_envelope"]["arbitrary_url_execution"] is False
    assert body["payment_session"]["status"] == "checkout_created"
    assert body["payment_url"]
    _assert_no_secret_value(body)

    with Session(engine) as session:
        assert len(session.exec(select(Quote)).all()) == 1
        assert len(session.exec(select(PaymentSession)).all()) == 1


def test_toolbox_checkout_sub_50_managed_requires_task_budget_without_payment_session(client, bot_headers, engine):
    response = client.post(
        "/api/v1/toolbox/exa.deep_search/checkout",
        json=_payload({"query": "micropayments"}),
        headers=bot_headers,
    )
    assert response.status_code == 402
    body = response.json()
    assert body["status"] == "task_budget_required"
    assert body["guidance"]["task_budget_required"] is True
    assert body["toolbox_quote"]["amount_minor"] < 50
    with Session(engine) as session:
        assert session.exec(select(PaymentSession)).all() == []


def test_toolbox_checkout_sub_50_paysh_requires_micro_rail_without_payment_session(client, bot_headers, engine):
    response = client.post(
        "/api/v1/toolbox/paysh.search/checkout",
        json=_payload({"instructions": "search latest docs"}),
        headers=bot_headers,
    )
    assert response.status_code == 402
    body = response.json()
    assert body["status"] == "micro_rail_required"
    assert body["guidance"]["required_rail"] in {"pay_sh", "x402"}
    assert body["guidance"]["pay_sh"]["arbitrary_url_execution"] is False
    with Session(engine) as session:
        assert session.exec(select(PaymentSession)).all() == []


def test_toolbox_checkout_rejects_request_hash_tampering(client, bot_headers):
    response = client.post(
        "/api/v1/toolbox/fal.image.generate/checkout",
        json=_payload({"prompt": "a robot", "quantity": 1}, request_hash="tampered"),
        headers=bot_headers,
    )
    assert response.status_code == 409


def test_toolbox_execution_create_get_complete_fail_lifecycle_safe_and_route_order(client, bot_headers, operator_headers, engine):
    created = client.post(
        "/api/v1/toolbox/exa.deep_search/executions",
        json=_payload({"query": "safe lifecycle"}),
        headers=bot_headers,
    )
    assert created.status_code == 200
    execution = created.json()
    assert execution["status"] == "payment_required"
    assert execution["tool_id"] == "exa.deep_search"

    # Route order regression: this must hit the execution endpoint, not /toolbox/{tool_id}.
    fetched = client.get(f"/api/v1/toolbox/executions/{execution['id']}", headers=bot_headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == execution["id"]

    scoped = client.get(f"/api/v1/toolbox/executions/{execution['id']}", headers=operator_headers)
    assert scoped.status_code == 200

    completed_unpaid = client.post(
        f"/api/v1/toolbox/executions/{execution['id']}/complete",
        json={"result_metadata": {"public": "ok"}},
        headers=bot_headers,
    )
    assert completed_unpaid.status_code == 409

    failed = client.post(
        f"/api/v1/toolbox/executions/{execution['id']}/fail",
        json={"error_metadata": {"message": "provider failed", "token": "payjent-test-secret-value-never-return"}},
        headers=bot_headers,
    )
    assert failed.status_code == 200
    fail_body = failed.json()
    assert fail_body["status"] == "failed"
    assert fail_body["error_metadata_json"]["token"] == "redacted"
    _assert_no_secret_value(fail_body)

    with Session(engine) as session:
        assert len(session.exec(select(ToolExecution)).all()) == 1


def test_toolbox_execution_ready_when_paid_payment_session_supplied(client, bot_headers, operator_headers):
    checkout = client.post(
        "/api/v1/toolbox/fal.image.generate/checkout",
        json=_payload({"prompt": "paid robot", "quantity": 1}),
        headers=bot_headers,
    ).json()
    ps_id = checkout["payment_session"]["id"]
    paid = client.post(f"/api/v1/payment-sessions/{ps_id}/mock-pay", headers=operator_headers)
    assert paid.status_code == 200

    created = client.post(
        "/api/v1/toolbox/fal.image.generate/executions",
        json=_payload({"prompt": "paid robot", "quantity": 1}, payment_session_id=ps_id),
        headers=bot_headers,
    )
    assert created.status_code == 200
    assert created.json()["status"] == "ready_to_execute"

    completed = client.post(
        f"/api/v1/toolbox/executions/{created.json()['id']}/complete",
        json={"result_metadata": {"public": "ok", "api_key": "payjent-test-secret-value-never-return", "nested": {"Authorization": "payjent-test-secret-value-never-return"}}},
        headers=bot_headers,
    )
    assert completed.status_code == 200
    complete_body = completed.json()
    assert complete_body["status"] == "succeeded"
    assert complete_body["result_metadata_json"]["api_key"] == "redacted"
    assert complete_body["result_metadata_json"]["nested"]["Authorization"] == "redacted"
    _assert_no_secret_value(complete_body)


def test_toolbox_rejects_secret_argument_keys_recursively(client, bot_headers):
    response = client.post(
        "/api/v1/toolbox/fal.image.generate/checkout",
        json=_payload({"prompt": "safe", "nested": {"api_key": "payjent-test-secret-value-never-return"}}),
        headers=bot_headers,
    )
    assert response.status_code == 422
    _assert_no_secret_value(response.json())


def test_toolbox_firecrawl_url_is_summarized_not_persisted(client, bot_headers, engine):
    raw_url = "https://example.com/private/path?safe=1"
    response = client.post(
        "/api/v1/toolbox/firecrawl.scrape/checkout",
        json=_payload({"url": raw_url}),
        headers=bot_headers,
    )
    assert response.status_code == 402
    response_text = json.dumps(response.json()).lower()
    assert "canonical_url" not in response_text
    assert raw_url.lower() not in response_text
    assert "private/path" not in response_text
    assert response.json()["guidance"]["task_budget_required"] is True
    _assert_no_secret_value(response.json())

    checkout = client.post(
        "/api/v1/toolbox/firecrawl.scrape/executions",
        json=_payload({"url": raw_url}),
        headers=bot_headers,
    )
    assert checkout.status_code == 200
    body = checkout.json()
    assert body["arguments_json"] == {"url": {"scheme": "https", "host": "example.com"}}
    body_text = json.dumps(body).lower()
    assert "canonical_url" not in body_text
    assert raw_url.lower() not in body_text
    assert "private/path" not in body_text
    _assert_no_secret_value(body)

    with Session(engine) as session:
        stored = session.get(ToolExecution, body["id"])
        assert stored.arguments_json["url"]["canonical_url"] == raw_url


def test_toolbox_firecrawl_rejects_secret_like_query_keys_before_quote_or_execution(client, bot_headers, engine):
    raw_url = "https://example.com/page?api_key=redacted"
    quote = client.post(
        "/api/v1/toolbox/firecrawl.scrape/quote",
        json=_payload({"url": raw_url}),
        headers=bot_headers,
    )
    assert quote.status_code == 422
    assert "redacted" not in json.dumps(quote.json()).lower()

    execution = client.post(
        "/api/v1/toolbox/firecrawl.scrape/executions",
        json=_payload({"url": raw_url}),
        headers=bot_headers,
    )
    assert execution.status_code == 422
    assert "redacted" not in json.dumps(execution.json()).lower()
    with Session(engine) as session:
        assert session.exec(select(Quote)).all() == []
        assert session.exec(select(ToolExecution)).all() == []


def test_toolbox_checkout_failure_rolls_back_quote_and_payment_session(client, bot_headers, engine):
    response = client.post(
        "/api/v1/toolbox/fal.image.generate/checkout",
        json=_payload({"prompt": "rollback robot", "quantity": 1}),
        headers={**bot_headers, "X-Payjent-Provider": "unsupported-provider"},
    )
    assert response.status_code == 422
    with Session(engine) as session:
        assert session.exec(select(Quote)).all() == []
        assert session.exec(select(PaymentSession)).all() == []


def test_toolbox_checkout_idempotency_returns_existing_session(client, bot_headers, engine):
    headers = {**bot_headers, "Idempotency-Key": "toolbox-idem-regression"}
    payload = _payload({"prompt": "same robot", "quantity": 1})
    first = client.post("/api/v1/toolbox/fal.image.generate/checkout", json=payload, headers=headers)
    second = client.post("/api/v1/toolbox/fal.image.generate/checkout", json=payload, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["payment_session"]["id"] == second.json()["payment_session"]["id"]
    with Session(engine) as session:
        assert len(session.exec(select(Quote)).all()) == 1
        assert len(session.exec(select(PaymentSession)).all()) == 1


def test_toolbox_execution_rejects_invalid_or_mismatched_quote_id(client, bot_headers):
    missing = client.post(
        "/api/v1/toolbox/fal.image.generate/executions",
        json=_payload({"prompt": "safe", "quantity": 1}, quote_id="quote_missing"),
        headers=bot_headers,
    )
    assert missing.status_code == 404
