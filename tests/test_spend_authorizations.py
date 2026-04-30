import os
import subprocess
import sys


def _presentation(q):
    return {"bot_id": q["bot_id"], "external_user_id": q["external_user_id"], "request_hash": q["request_hash"]}


def _consume(client, q, grant, bot_headers):
    resp = client.post(f"/api/v1/grants/{grant['id']}/consume", headers=bot_headers, json=_presentation(q))
    assert resp.status_code == 200, resp.text


def _spend_payload(q, operation_id, *, amount_minor=125, presentation=None, capture=True):
    return {
        "operation_id": operation_id,
        "presentation": presentation or _presentation(q),
        "tool": "premium-research-tool",
        "vendor": "premium-mcp-demo",
        "rail": "x402",
        "amount_minor": amount_minor,
        "currency": "USD",
        "reason": "premium lookup",
        "capture": capture,
    }


def test_spend_authorization_capture_happy_path(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    _consume(client, q, grant, bot_headers)
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(q, "happy-path-call-1"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["operation_id"] == "happy-path-call-1"
    assert data["status"] == "captured"
    assert data["total_authorized"] == 125
    assert data["total_captured"] == 125
    assert data["remaining_budget"] == q["amount_minor"] - 125


def test_spend_after_grant_consume_succeeds(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    _consume(client, q, grant, bot_headers)
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(q, "post-consume-call-1", amount_minor=1),
    )
    assert resp.status_code == 200, resp.text


def test_spend_before_grant_consume_is_rejected(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(q, "pre-consume-call-1", amount_minor=1),
    )
    assert resp.status_code == 409


def test_duplicate_operation_id_is_idempotent_and_does_not_double_count(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    _consume(client, q, grant, bot_headers)
    payload = {**_spend_payload(q, "retryable-premium-call-1"), "provider_reference": "provider-ref-1"}
    first = client.post(f"/api/v1/grants/{grant['id']}/spend-authorizations", headers=bot_headers, json=payload)
    retry = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json={**payload, "amount_minor": 200},
    )
    assert first.status_code == 200, first.text
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["operation_id"] == "retryable-premium-call-1"
    assert retry.json()["total_authorized"] == 125
    assert retry.json()["remaining_budget"] == q["amount_minor"] - 125


def test_over_budget_spend_rejected_and_not_captured(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    _consume(client, q, grant, bot_headers)
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(q, "over-budget-call-1", amount_minor=q["amount_minor"] + 1),
    )
    assert resp.status_code == 409
    ok = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(q, "full-budget-call-1", amount_minor=q["amount_minor"]),
    )
    assert ok.status_code == 200
    assert ok.json()["total_captured"] == q["amount_minor"]


def test_wrong_presentation_cannot_spend(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    _consume(client, q, grant, bot_headers)
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json=_spend_payload(
            q,
            "wrong-presentation-call-1",
            amount_minor=1,
            presentation={"bot_id": "bot-1", "external_user_id": "wrong-user", "request_hash": "hash-1"},
        ),
    )
    assert resp.status_code == 403


def test_discord_aggregator_demo_succeeds_without_env_keys_and_ignores_stale_db(tmp_path):
    stale_db = tmp_path / "payjent.db"
    stale_db.write_text("not a sqlite database")
    env = os.environ.copy()
    env.pop("PAYJENT_DATABASE_URL", None)
    env.pop("PAYJENT_BOT_KEY", None)
    env.pop("PAYJENT_OPERATOR_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "discord-aggregator"],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": os.getcwd()},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout
    assert "DISCORD_COMMAND: /research-with-paid-tools" in out
    assert "PAYMENT_PROMPT:" in out
    assert "checkout_url=http://testserver/pay/" in out
    assert "Stripe funding rail placeholder" in out
    assert "operator mock-pay is local/dev-only" in out
    assert "grant_consumed_before_x402_spend=True" in out
    assert "x402_operation_id=discord-demo-x402-premium-call-1" in out
    assert "x402_spend_status=captured" in out
    assert "remaining_budget=USD 6.50" in out
    assert "final_status=fulfilled" in out
