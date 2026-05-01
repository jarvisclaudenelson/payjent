from datetime import datetime, timezone
from typing import Any
from sqlmodel import Field, SQLModel, Column, JSON, UniqueConstraint


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Quote(SQLModel, table=True):
    id: str = Field(primary_key=True)
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str
    amount_minor: int
    currency: str
    cost_breakdown: list[dict[str, Any]] = Field(sa_column=Column(JSON))
    execution_envelope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    quote_hash: str
    status: str = "quoted"
    created_at: datetime = Field(default_factory=now_utc)


class BotCredential(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    bot_id: str = Field(index=True)
    key_hash: str = Field(index=True, unique=True)
    role: str = "bot"
    created_at: datetime = Field(default_factory=now_utc)


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=now_utc)


class AgentProfile(SQLModel, table=True):
    id: str = Field(primary_key=True)
    owner_id: str = Field(default="local-owner", index=True)
    bot_id: str = Field(index=True, unique=True)
    name: str
    platform: str
    callback_url: str | None = None
    default_currency: str = "USD"
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=now_utc)


class RailConnection(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("agent_id", "rail", name="uq_rail_agent_rail"),)

    id: str = Field(primary_key=True)
    agent_id: str = Field(index=True)
    bot_id: str = Field(index=True)
    rail: str = Field(index=True)
    status: str = Field(index=True)
    mode: str = Field(default="local", index=True)
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime | None = None


class PaymentSession(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    provider: str = "mock"
    status: str = "checkout_created"
    checkout_url: str | None = None
    provider_session_id: str | None = Field(default=None, index=True)
    idempotency_key: str | None = Field(default=None, index=True)
    receipt_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    paid_at: datetime | None = None


class Receipt(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("payment_session_id", name="uq_receipt_payment_session_id"),)
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    payment_session_id: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
    signature: str
    created_at: datetime = Field(default_factory=now_utc)


class Grant(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("payment_session_id", name="uq_grant_payment_session_id"),)

    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    payment_session_id: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
    signature: str
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class SpendLedgerEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("grant_id", "operation_id", name="uq_spend_grant_operation_id"),)

    id: str = Field(primary_key=True)
    grant_id: str = Field(index=True)
    quote_id: str = Field(index=True)
    operation_id: str = Field(index=True)
    tool: str
    vendor: str
    rail: str
    amount_minor: int
    currency: str
    reason: str
    status: str = Field(index=True)
    provider_reference: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)


class FulfillmentEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    status: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
