from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    settlement: Literal["external_x402_runtime"] = "external_x402_runtime"
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


class X402PaidActionCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    service_url: str | None = None
    target_url: str | None = None
    service_fqn: str | None = None
    resource: str | None = None
    method: str = "POST"
    body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    provider: str | None = "pay_sh"
    rail: str | None = "x402"
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    readiness_mode: Literal["enforced", "advisory"] = "enforced"
    execution_readiness: dict[str, Any] = Field(default_factory=dict)
    payjent_fulfillment_callback: bool = Field(
        default=False,
        description="Must remain false for generic x402/pay.sh actions. Payjent does not execute target_url/service_url; agents consume the grant, obtain spend authorization, execute externally, then mark complete.",
    )
    payjent_managed_execution: bool = Field(
        default=False,
        description="Must remain false for generic x402/pay.sh actions; Payjent authorizes spend but does not execute downstream tasks.",
    )
    callback_url: str | None = None


class PayShPremiumActionCreate(X402PaidActionCreate):
    """Backward-compatible alias for the generic x402/pay.sh paid action primitive."""


class TaskBudgetCreate(BaseModel):
    bot_id: str
    external_user_id: str
    task_id: str = Field(min_length=1)
    max_amount_minor: int = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class TaskBudgetRead(BaseModel):
    id: str
    bot_id: str
    external_user_id: str
    task_id: str
    max_amount_minor: int
    currency: str
    available_minor: int
    reserved_minor: int
    captured_minor: int
    refunded_minor: int
    released_minor: int
    status: str
    provider: str | None = None
    checkout_url: str | None = None


class TaskBudgetFundResponse(BaseModel):
    budget: TaskBudgetRead
    checkout_url: str | None = None
    message: str


class PremiumActionCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    provider: str = Field(default="generic", min_length=1, max_length=64)
    rail: str | None = None
    target_url: str | None = None
    service_url: str | None = None
    action_type: str = "premium_action"
    kind: str | None = None
    method: str = "POST"
    body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    description: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    readiness_mode: Literal["enforced", "advisory"] = "enforced"
    execution_readiness: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = None
    payjent_fulfillment_callback: bool = Field(default=False)
    payjent_managed_execution: bool = Field(default=False)
    task_budget_id: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider_slug(cls, value: str) -> str:
        import re

        slug = (value or "generic").strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", slug):
            raise ValueError("provider must be a safe slug containing only lowercase letters, numbers, and underscores")
        return slug


class BigQueryPaidQueryCreate(BaseModel):
    bot_id: str
    external_user_id: str
    project_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    use_legacy_sql: bool = False
    request_summary: str | None = None
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    callback_url: str | None = None


class MerchantInfo(BaseModel):
    name: str = Field(min_length=1)
    domain: str = Field(min_length=1)


class PurchaseItemSummary(BaseModel):
    summary: str = Field(min_length=1)
    url: str | None = None


class PurchaseFulfillmentCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    merchant: MerchantInfo
    item: PurchaseItemSummary
    order_summary: str = Field(min_length=1)
    service_url: str = Field(min_length=1)
    method: Literal["POST"] = "POST"
    body: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    payjent_fulfillment_callback: Literal[True] = Field(
        default=True,
        description="Required: Payjent verifies payment, then POSTs a signed fulfillment handoff to an allowlisted procurement executor.",
    )
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
    fulfillment_events: list[dict[str, Any]] = Field(default_factory=list)


class ResumeEventRead(BaseModel):
    id: str
    event_type: str
    action_id: str
    quote_id: str
    payment_session_id: str
    bot_id: str
    status: str
    payload: dict[str, Any]
    callback_status: str | None = None
    created_at: str


class ResumeEventListResponse(BaseModel):
    events: list[ResumeEventRead]


class ResumeEventAckResponse(BaseModel):
    id: str
    status: str
    acked: bool


class PayShPremiumActionCreateResponse(AgentActionCreateResponse):
    provider: Literal["pay_sh"] = "pay_sh"
    premium_provider: Literal["pay_sh"] = "pay_sh"
    command_preview: str
    request_fingerprint: str | None = None
    execution_boundary: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class X402PaidActionCreateResponse(PayShPremiumActionCreateResponse):
    """Response for the public generic x402/pay.sh paid action primitive."""


class PremiumActionPresetActionCreate(BaseModel):
    bot_id: str
    external_user_id: str
    request_summary: str | None = None
    request_hash: str | None = None
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    cost_breakdown: list[CostItem]
    input: dict[str, Any] = Field(default_factory=dict)
    readiness_mode: Literal["enforced", "advisory"] = "enforced"
    execution_readiness: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = None


class ExecutionReadinessRequest(BaseModel):
    provider: str = Field(default="generic", min_length=1, max_length=64)
    rail: str | None = None
    status: Literal["ready", "connected", "active", "not_ready", "disabled"] = "ready"
    runtime_ready: bool = False
    can_execute_without_device_auth: bool = False
    provider_connected: bool = False
    labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionReadinessCheckRequest(BaseModel):
    bot_id: str = Field(min_length=1)
    provider: str = Field(default="generic", min_length=1, max_length=64)
    readiness_mode: Literal["enforced", "advisory"] = "enforced"
    execution_readiness: dict[str, Any] = Field(default_factory=dict)


class ExecutionReadinessResponse(BaseModel):
    bot_id: str
    provider: str
    ready_for_payment: bool
    charge_allowed: bool
    readiness_mode: str
    evidence: dict[str, Any]
    setup_guidance: str


class PremiumActionCreateResponse(AgentActionCreateResponse):
    provider: str
    premium_provider: str
    command_preview: str | None = None
    request_fingerprint: str | None = None
    execution_boundary: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class AgentActionFailRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    refund: bool = True
    reason: str = Field(default="provider_execution_failed", min_length=1, max_length=300)


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


class AgentActionFailResponse(AgentActionCompleteResponse):
    refund_status: str = "not_requested"
    refund_id: str | None = None
    payment_status: str | None = None
    quote_status: str | None = None
    message: str | None = None


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


class PaymentSessionRefundCreate(BaseModel):
    reason: str = Field(default="operator_requested", min_length=1, max_length=300)
    force: bool = False


class PaymentSessionRefundResponse(BaseModel):
    payment_session_id: str
    quote_id: str
    payment_status: str
    quote_status: str
    refund_id: str
    refund_status: str
    amount_minor: int
    currency: str
    fulfillment_id: str
    message: str


class FulfillmentRead(BaseModel):
    id: str
    quote_id: str
    status: str
    metadata: dict[str, Any]
