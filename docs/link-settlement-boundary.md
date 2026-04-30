# Link settlement boundary

Payjent's Link rail is currently an approval and credential-request flow only. A Link approval URL, spend request, or one-time credential creation is **not** Payjent settlement by itself.

## Current behavior

- Checkout with `X-Payjent-Provider: link` creates a Payjent payment session in `checkout_created` state.
- `POST /api/v1/payment-sessions/{session_id}/link/spend-request` creates or requests a Link approval URL and polling hint.
- Approval URL creation, user approval, and credential creation do not mark the Payjent session paid.
- Payjent does not issue a receipt or grant from the current Link approval/credential-request flow.

## Required settlement signal

Payjent should issue a receipt/grant only after verified terminal payment evidence exists, such as a successful merchant charge or a future Link MPP/payment confirmation. The exact terminal signal and verification mapping are intentionally still to be defined.

## Fail-closed rule

The next planned Link endpoint may poll Link status, but it must fail closed until Payjent maps a verified terminal settlement signal. Non-terminal Link states, approval-only states, credential-created states, missing status, or unknown status must leave the Payjent session unpaid and must not issue grants.
