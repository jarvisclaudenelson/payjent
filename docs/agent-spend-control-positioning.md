# Payjent vs. Zero-like agent activation tools

This is a public, agent-readable positioning note for agents that discover or call paid external tools.

## Short version

Payjent is not a generic tool search engine. Payjent is the spend-control and payment authorization layer for agents using external tools, including Zero-like discovery/activation tools, x402 services, MPP endpoints, pay.sh-style providers, and direct provider APIs.

Publicly, Zero is positioned as an agent helper for finding and activating paid APIs and services. Payjent is complementary: it gives the agent a request-bound authorization checkpoint before money is spent, records the exact quote the human approved, and preserves a tenant-scoped spend ledger with fulfillment, failure, receipt, and refund evidence.

## When to use each layer

Use a Zero-like discovery or activation layer when the agent needs to:

- find a capability or paid API that can satisfy a user request;
- learn how to call a provider, x402 service, MPP endpoint, or payment-activated tool;
- activate the provider call in the agent runtime after authorization is complete.

Use Payjent when the agent needs to:

- enforce human/user-funded budgets before external spend;
- bind approval to a specific request, user, provider, quote, and execution envelope;
- require an exact provider quote and line-item cost breakdown before checkout;
- pause at a payment checkpoint until the human funds or approves the action;
- issue a bounded grant that authorizes only the approved spend;
- keep a tenant-scoped audit trail and spend ledger;
- attach fulfillment, failure, receipt, and refund evidence after provider execution.

## Agent workflow

1. Discover the needed tool externally with a catalog, search, Zero-like activation helper, x402 directory, MPP catalog, or direct provider docs.
2. Obtain the exact provider quote for the concrete request. Do not invent placeholder, demo, default, minimum, or test amounts.
3. Create a Payjent action or task budget with the quote, request hash, user/tenant context, provider metadata, and cost breakdown.
4. Send the human to Payjent checkout or approval. The agent waits until Payjent returns a paid/approved bounded grant.
5. Execute the external provider in the agent-side runtime using the provider's normal mechanism.
6. Complete, fail, or refund the Payjent action with evidence: provider receipt, fulfillment artifact, error, or refund reference.

## Secret safety

Public docs and chats must not contain provider secrets, API keys, payment tokens, private grants, bearer tokens, or one-time credentials. Store those only in the agent's private secret store or deployment secret manager. Public user-facing messages should contain safe checkout/approval links and non-secret status references, not raw credentials.

## Positioning summary

Discovery and activation tools help an agent find or call something useful. Payjent decides whether the agent is allowed to spend money on that call, under what exact budget, for which request, and with what audit evidence after execution.
