import os
import subprocess
import sys


def _presentation(q):
    return {"bot_id": q["bot_id"], "external_user_id": q["external_user_id"], "request_hash": q["request_hash"]}


def test_spend_authorization_capture_happy_path(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json={
            "presentation": _presentation(q),
            "tool": "premium-research-tool",
            "vendor": "premium-mcp-demo",
            "rail": "x402",
            "amount_minor": 125,
            "currency": "USD",
            "reason": "premium lookup",
            "capture": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "captured"
    assert data["total_authorized"] == 125
    assert data["total_captured"] == 125
    assert data["remaining_budget"] == q["amount_minor"] - 125


def test_over_budget_spend_rejected_and_not_captured(client, paid_grant, bot_headers):
    q, _ps, grant = paid_grant
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json={
            "presentation": _presentation(q),
            "tool": "premium-research-tool",
            "vendor": "premium-mcp-demo",
            "rail": "x402",
            "amount_minor": q["amount_minor"] + 1,
            "currency": "USD",
            "reason": "too much",
            "capture": True,
        },
    )
    assert resp.status_code == 409
    ok = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json={
            "presentation": _presentation(q),
            "tool": "premium-research-tool",
            "vendor": "premium-mcp-demo",
            "rail": "x402",
            "amount_minor": q["amount_minor"],
            "currency": "USD",
            "reason": "full budget",
            "capture": True,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["total_captured"] == q["amount_minor"]


def test_wrong_presentation_cannot_spend(client, paid_grant, bot_headers):
    _q, _ps, grant = paid_grant
    resp = client.post(
        f"/api/v1/grants/{grant['id']}/spend-authorizations",
        headers=bot_headers,
        json={
            "presentation": {"bot_id": "bot-1", "external_user_id": "wrong-user", "request_hash": "hash-1"},
            "tool": "premium-research-tool",
            "vendor": "premium-mcp-demo",
            "rail": "x402",
            "amount_minor": 1,
            "currency": "USD",
            "reason": "bad actor",
            "capture": True,
        },
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
    assert "x402_spend_status=captured" in out
    assert "remaining_budget=USD 6.50" in out
    assert "final_status=fulfilled" in out
