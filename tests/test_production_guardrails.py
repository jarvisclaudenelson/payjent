import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from payjent import demo
from payjent.auth import create_bot_credential
from payjent.config import Settings, get_settings
from payjent.db import get_session
from payjent.main import app, mock_pay
from payjent.models import BotCredential, PaymentSession, Quote


def test_production_guardrails_reject_default_secret_and_http_url():
    settings = Settings(env="production", public_base_url="http://payjent.example")
    with pytest.raises(RuntimeError) as excinfo:
        settings.validate_runtime_guardrails()
    message = str(excinfo.value)
    assert "PAYJENT_SIGNING_SECRET" in message
    assert "PAYJENT_PUBLIC_BASE_URL" in message


def test_production_guardrails_require_stripe_secrets_when_stripe_selected():
    settings = Settings(
        env="production",
        signing_secret="<replace-with-strong-secret>",
        public_base_url="https://payjent.example",
        checkout_provider="stripe",
    )
    with pytest.raises(RuntimeError) as excinfo:
        settings.validate_runtime_guardrails()
    message = str(excinfo.value)
    assert "PAYJENT_STRIPE_SECRET_KEY" in message
    assert "PAYJENT_STRIPE_WEBHOOK_SECRET" in message


def test_production_guardrails_accept_https_and_required_stripe_placeholders():
    settings = Settings(
        env="production",
        signing_secret="<replace-with-strong-secret>",
        public_base_url="https://payjent.example",
        checkout_provider="stripe",
        stripe_secret_key="<stripe-secret-key>",
        stripe_webhook_secret="<stripe-webhook-secret>",
    )
    settings.validate_runtime_guardrails()


def test_lifespan_fails_closed_for_invalid_production_env(monkeypatch):
    monkeypatch.setenv("PAYJENT_ENV", "production")
    monkeypatch.setenv("PAYJENT_PUBLIC_BASE_URL", "http://payjent.example")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="production guardrails failed"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_mock_pay_disabled_in_production_even_if_dev_flags_enabled(engine):
    with Session(engine) as session:
        quote = Quote(
            id="quote-prod",
            bot_id="bot-1",
            external_user_id="user-1",
            request_summary="prod guardrail",
            request_hash="hash-prod",
            quote_hash="quote-hash-prod",
            amount_minor=100,
            currency="USD",
            cost_breakdown=[{"label": "work", "amount_minor": 100}],
            execution_envelope={},
        )
        payment_session = PaymentSession(id="ps-prod", quote_id=quote.id, provider="mock", checkout_url="/pay/ps-prod")
        session.add(quote)
        session.add(payment_session)
        session.commit()

        with pytest.raises(HTTPException) as excinfo:
            mock_pay(
                "ps-prod",
                session=session,
                settings=Settings(
                    env="production",
                    dev_mode=True,
                    mock_provider_enabled=True,
                    signing_secret="<replace-with-strong-secret>",
                    public_base_url="https://payjent.example",
                ),
                _credential=object(),
            )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "mock provider disabled"


def test_reset_dev_database_drops_and_recreates_tables(engine, monkeypatch):
    monkeypatch.setattr(demo, "engine", engine)
    get_settings.cache_clear()
    with Session(engine) as session:
        create_bot_credential(session, "bot-1", "test-key", Settings().signing_secret)
        assert len(session.exec(select(BotCredential)).all()) == 1

    demo.reset_dev_database()

    with Session(engine) as session:
        assert session.exec(select(BotCredential)).all() == []


def test_reset_dev_database_refuses_production_without_unsafe_override(engine, monkeypatch):
    monkeypatch.setattr(demo, "engine", engine)
    monkeypatch.setenv("PAYJENT_ENV", "production")
    monkeypatch.delenv("PAYJENT_ALLOW_UNSAFE_DB_RESET", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="Refusing to reset the database in production"):
            demo.reset_dev_database()
    finally:
        get_settings.cache_clear()


def test_reset_dev_database_allows_explicit_unsafe_production_override(engine, monkeypatch):
    monkeypatch.setattr(demo, "engine", engine)
    monkeypatch.setenv("PAYJENT_ENV", "production")
    monkeypatch.setenv("PAYJENT_ALLOW_UNSAFE_DB_RESET", "true")
    get_settings.cache_clear()
    try:
        demo.reset_dev_database()
    finally:
        get_settings.cache_clear()


def test_checkout_provider_stripe_in_production_fails_before_serving_without_webhook_secret(monkeypatch):
    monkeypatch.setenv("PAYJENT_ENV", "production")
    monkeypatch.setenv("PAYJENT_SIGNING_SECRET", "<replace-with-strong-secret>")
    monkeypatch.setenv("PAYJENT_PUBLIC_BASE_URL", "https://payjent.example")
    monkeypatch.setenv("PAYJENT_CHECKOUT_PROVIDER", "stripe")
    monkeypatch.setenv("PAYJENT_STRIPE_SECRET_KEY", "<stripe-secret-key>")
    monkeypatch.delenv("PAYJENT_STRIPE_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="PAYJENT_STRIPE_WEBHOOK_SECRET"):
            get_settings().validate_runtime_guardrails()
    finally:
        get_settings.cache_clear()
