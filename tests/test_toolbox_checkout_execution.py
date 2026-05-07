import json

from sqlmodel import Session, select

from payjent.models import PaymentSession, Quote, ToolExecution


def _payload(arguments=None, **extra):
    body = {"bot_id": "bot-1", "external_user_id": "user-1", "arguments": arguments or {"prompt": "a robot", "quantity": 1}}
    body.update(extra)
    return body


def _assert_no_secret_value(value, secret="super-secret-value"):
    serialized = json.dumps(value).lower()
    assert secret not in serialized


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

    completed = client.post(
        f"/api/v1/toolbox/executions/{execution['id']}/complete",
        json={"result_metadata": {"public": "ok", "api_key": "super-secret-value", "nested": {"Authorization": "super-secret-value"}}},
        headers=bot_headers,
    )
    assert completed.status_code == 200
    complete_body = completed.json()
    assert complete_body["status"] == "succeeded"
    assert complete_body["result_metadata_json"]["api_key"] == "redacted"
    assert complete_body["result_metadata_json"]["nested"]["Authorization"] == "redacted"
    _assert_no_secret_value(complete_body)

    failed = client.post(
        f"/api/v1/toolbox/executions/{execution['id']}/fail",
        json={"error_metadata": {"message": "provider failed", "token": "super-secret-value"}},
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
