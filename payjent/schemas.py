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
    callback_url: str | None = None


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


class AgentRegisterRequest(BaseModel):
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    bot_id: str = Field(min_length=1)
    callback_url: str | None = None
    default_currency: str = Field(default="USD", min_length=3, max_length=3)


class RailConnectionRead(BaseModel):
    rail: str
    status: str
    mode: str
    config: dict[str, Any]


class AgentRead(BaseModel):
    id: str
    owner_id: str
    bot_id: str
    name: str
    platform: str
    callback_url: str | None
    default_currency: str
    status: str
    rails: dict[str, RailConnectionRead] = Field(default_factory=dict)


class AgentRegisterResponse(BaseModel):
    agent: AgentRead
    bot_api_key: str | None = None
    key_warning: str = "Store bot_api_key now; Payjent only stores a hash and will not show it again."


class HostedSmokeBootstrapRequest(BaseModel):
    bot_id: str = Field(min_length=1)
    operator_id: str = Field(default="operator-smoke", min_length=1)
    agent_name: str = Field(default="Payjent hosted smoke agent", min_length=1)
    platform: str = Field(default="generic-agent", min_length=1)
    callback_url: str | None = None
    default_currency: str = Field(default="USD", min_length=3, max_length=3)


class HostedSmokeBootstrapResponse(BaseModel):
    bot_id: str
    operator_id: str
    agent: AgentRead
    bot_api_key: str
    operator_api_key: str
    behavior: str = "Existing agent profiles are reused, but new bot/operator credentials are minted every call because Payjent stores only hashes and cannot recover prior plaintext keys."
    key_warning: str = "Plaintext keys are returned once from this authenticated bootstrap action; store them securely. Payjent stores only hashes."


class HostedSmokeStatusRequest(BaseModel):
    bot_id: str | None = None
    operator_id: str | None = None
    callback_url: str | None = None


class HostedSmokeStatusResponse(BaseModel):
    ok: bool
    base_url: str
    public_base_url: str
    provider: Literal["pay_sh"] = "pay_sh"
    settlement: Literal["external_pay_sh_runtime"] = "external_pay_sh_runtime"
    operator_mock_pay: Literal["test_rail_only"] = "test_rail_only"
    action_id: str
    payment_session_id: str
    payment_link_exists: bool
    callback_mode: str
    callback_contains_payment_token: bool
    callback_contains_grant: bool
    unpaid_poll: dict[str, Any]
    paid_poll: dict[str, Any]
    resumed_status: str
    fulfilled_status: str
    security_note: str = "Redacted status artifact: no Payjent API keys, bootstrap token, raw payment_token, or grant token are returned."
    dev_note: str = "This protected smoke uses Payjent's internal mock/test settlement rail only to verify gate/resume/fulfillment metadata; it never executes or settles pay.sh."


class StripeConnectStartResponse(BaseModel):
    mode: str
    account_id: str
    onboarding_url: str
    status: str


class X402ConfigureRequest(BaseModel):
    network: str = Field(min_length=1)
    pay_to: str | None = None
    facilitator_url: str | None = None
    max_per_request_minor: int = Field(gt=0)
    max_per_call_minor: int = Field(gt=0)
    enabled: bool = True


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


class AgentActionCreate(QuoteCreate):
    """Create a paid agent action request.

    MVP alias: the returned action_id is the created quote id, preserving the
    existing request_hash binding to bot/user/payload without a migration.
    """


class PayShPremiumActionCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    service_url: str | None = None
    service_fqn: str | None = None
    resource: str | None = None
    method: str = "POST"
    body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    callback_url: str | None = None


class PaymentPrompt(BaseModel):
    action_id: str
    payment_url: str | None = None
    amount_minor: int
    currency: str
    message: str


class AgentActionCreateResponse(BaseModel):
    action_id: str
    quote_id: str
    payment_session_id: str
    payment_url: str | None = None
    amount_minor: int
    currency: str
    status: str
    request_hash: str
    payment_prompt: PaymentPrompt
    message: str


class AgentActionStatusResponse(BaseModel):
    action_id: str
    quote_id: str
    payment_session_id: str | None = None
    payment_status: str | None = None
    quote_status: str
    status: str
    request_hash: str
    external_user_id: str
    amount_minor: int
    currency: str
    payment_token: str | None = None
    payment_token_status: Literal["unissued", "available", "consumed"] = "unissued"


class PayShPremiumActionCreateResponse(AgentActionCreateResponse):
    provider: Literal["pay_sh"] = "pay_sh"
    premium_provider: Literal["pay_sh"] = "pay_sh"
    command_preview: str


class AgentActionConsumeRequest(BaseModel):
    payment_token: str
    presentation: GrantPresentation


class AgentActionExecutionEnvelope(BaseModel):
    action_id: str
    quote_id: str
    grant_id: str
    payment_token: str
    request_hash: str
    external_user_id: str
    bot_id: str
    execution_envelope: dict[str, Any]
    status: str


class AgentActionCompleteResponse(BaseModel):
    action_id: str
    quote_id: str
    fulfillment_id: str
    status: str
    metadata: dict[str, Any]


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
