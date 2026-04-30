from typing import Any, Literal
from pydantic import BaseModel, Field


class CostItem(BaseModel):
    label: str
    amount_minor: int = Field(ge=0)


class QuoteCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    execution_envelope: dict[str, Any] = Field(default_factory=dict)


class QuoteRead(BaseModel):
    id: str
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str
    amount_minor: int
    currency: str
    cost_breakdown: list[dict[str, Any]]
    execution_envelope: dict[str, Any]
    quote_hash: str
    status: str


class PaymentSessionRead(BaseModel):
    id: str
    quote_id: str
    provider: str
    status: str
    checkout_url: str | None = None
    provider_session_id: str | None = None
    idempotency_key: str | None = None
    receipt_id: str | None = None


class MockPayResponse(BaseModel):
    payment_session: PaymentSessionRead
    receipt: dict[str, Any]
    grant: dict[str, Any]


class LinkCredentialRequest(BaseModel):
    merchant_url: str = Field(min_length=1)
    credential_type: str = Field(min_length=1)
    purpose: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkCredentialApproval(BaseModel):
    payment_session: PaymentSessionRead
    approval_url: str
    provider_session_id: str
    polling_command: list[str] | None = None
    message: str


class LinkPollResponse(BaseModel):
    payment_session: PaymentSessionRead
    normalized_status: Literal["pending", "approved_not_settled", "credential_created_not_settled", "settled", "failed", "unknown"]
    provider_session_id: str | None = None
    raw_status: str | None = None
    is_settled: bool
    settlement_mapping_required: bool = True
    message: str


class GrantPresentation(BaseModel):
    bot_id: str | None = None
    external_user_id: str | None = None
    request_hash: str | None = None


class GrantVerifyResponse(BaseModel):
    valid: bool
    grant_id: str
    consumed: bool
    payload: dict[str, Any]


class SpendAuthorizationCreate(BaseModel):
    operation_id: str = Field(min_length=1)
    presentation: GrantPresentation
    tool: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    rail: str = Field(min_length=1)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(min_length=1)
    provider_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    capture: bool = False


class SpendAuthorizationRead(BaseModel):
    id: str
    grant_id: str
    quote_id: str
    operation_id: str
    tool: str
    vendor: str
    rail: str
    amount_minor: int
    currency: str
    reason: str
    status: str
    provider_reference: str | None = None
    metadata: dict[str, Any]
    total_authorized: int
    total_captured: int
    remaining_budget: int


class SpendCaptureRequest(BaseModel):
    presentation: GrantPresentation
    provider_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FulfillmentCreate(BaseModel):
    status: Literal["executing", "fulfilled", "failed", "refunded"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class FulfillmentRead(BaseModel):
    id: str
    quote_id: str
    status: str
    metadata: dict[str, Any]
