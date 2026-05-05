import os
import re
import subprocess
import sys

from sqlmodel import Session, select

from payjent.auth import hash_api_key
from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.models import AgentProfile, BotCredential


def _enable_bootstrap(token="bootstrap-test-token"):
    app.dependency_overrides[get_settings] = lambda: Settings(bootstrap_token=token)
    return token


def test_hosted_smoke_bootstrap_disabled_and_requires_token(client):
    payload = {"bot_id": "smoke-bot"}
    assert client.post("/api/v1/bootstrap/hosted-smoke", json=payload).status_code == 404

    token = _enable_bootstrap()
    try:
        assert client.post("/api/v1/bootstrap/hosted-smoke", json=payload).status_code == 401
        assert client.post("/api/v1/bootstrap/hosted-smoke", json=payload, headers={"X-Payjent-Bootstrap-Token": "wrong"}).status_code == 401
        ok = client.post("/api/v1/bootstrap/hosted-smoke", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_hosted_smoke_bootstrap_mints_hashed_credentials_and_is_repeatable(client, engine):
    token = _enable_bootstrap()
    payload = {"bot_id": "repeat-bot", "operator_id": "repeat-operator"}
    try:
        first = client.post("/api/v1/bootstrap/hosted-smoke", json=payload, headers={"X-Payjent-Bootstrap-Token": token})
        second = client.post("/api/v1/bootstrap/hosted-smoke", json=payload, headers={"X-Payjent-Bootstrap-Token": token})
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    a = first.json()
    b = second.json()
    assert a["agent"]["id"] == b["agent"]["id"]
    assert a["bot_api_key"] != b["bot_api_key"]
    assert a["operator_api_key"] != b["operator_api_key"]
    assert "stores only hashes" in a["key_warning"]

    settings = Settings()
    with Session(engine) as session:
        assert session.exec(select(AgentProfile).where(AgentProfile.bot_id == "repeat-bot")).first()
        creds = session.exec(select(BotCredential).where(BotCredential.bot_id.in_(["repeat-bot", "repeat-operator"]))).all()
        hashes = {c.key_hash for c in creds}
    assert a["bot_api_key"] not in hashes
    assert a["operator_api_key"] not in hashes
    assert hash_api_key(a["bot_api_key"], settings.signing_secret) in hashes
    assert hash_api_key(a["operator_api_key"], settings.signing_secret) in hashes


def test_hosted_smoke_bootstrap_cli_exports_and_run_smoke_redacts(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PAYJENT_DATABASE_URL"}
    env["PYTHONPATH"] = os.getcwd()
    env["PAYJENT_BOOTSTRAP_TOKEN"] = "cli-bootstrap-token"
    exports = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "hosted-smoke-bootstrap", "--in-process", "--base-url", "http://testserver"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exports.returncode == 0, exports.stderr
    assert "export PAYJENT_BASE_URL='http://testserver'" in exports.stdout
    assert "export PAYJENT_BOT_KEY='payjent_" in exports.stdout
    assert "export PAYJENT_OPERATOR_KEY='payjent_" in exports.stdout

    smoke = subprocess.run(
        [sys.executable, "-m", "payjent.demo", "hosted-smoke-bootstrap", "--in-process", "--base-url", "http://testserver", "--run-smoke"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert "Payjent hosted agent-owner smoke completed." in smoke.stdout
    assert "export PAYJENT_BOT_KEY" not in smoke.stdout
    assert "export PAYJENT_OPERATOR_KEY" not in smoke.stdout
    assert not re.search(r"payjent_[A-Za-z0-9_-]{20,}", smoke.stdout)
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", smoke.stdout)
