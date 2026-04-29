from datetime import datetime, timezone
from typing import Any
from sqlmodel import Field, SQLModel, Column, JSON


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


class PaymentSession(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    provider: str = "mock"
    status: str = "checkout_created"
    checkout_url: str | None = None
    receipt_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    paid_at: datetime | None = None


class Receipt(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    payment_session_id: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
    signature: str
    created_at: datetime = Field(default_factory=now_utc)


class Grant(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    payload: dict[str, Any] = Field(sa_column=Column(JSON))
    signature: str
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=now_utc)


class FulfillmentEvent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    quote_id: str = Field(index=True)
    status: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now_utc)
