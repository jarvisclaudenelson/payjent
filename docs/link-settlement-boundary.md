# Link settlement boundary

Payjent's Link rail is currently an approval and credential-request flow only. A Link approval URL, spend request, or one-time credential creation is **not** Payjent settlement by itself.

## Current behavior

- Checkout with `X-Payjent-Provider: link` creates a Payjent payment session in `checkout_created` state.
- `POST /api/v1/payment-sessions/{session_id}/link/spend-request` creates or requests a Link approval URL and polling hint.
- `POST /api/v1/payment-sessions/{session_id}/link/poll` retrieves Link status using MCP first or `link-cli spend-request retrieve {provider_session_id} --format json` after an auth-status preflight.
- Approval URL creation, user approval, and credential creation do not mark the Payjent session paid.
- Payjent does not issue a receipt or grant from the current Link approval/credential-request/polling flow.

## Status normalization

| Raw Link status/state examples | Payjent normalized status | Settlement? | Payjent action |
| --- | --- | --- | --- |
| missing, unrecognized | `unknown` | No | Leave session unpaid; no receipt/grant |
| `pending`, `created`, `requires_approval`, `processing` | `pending` | No | Leave session unpaid; no receipt/grant |
| `approved`, `approval_complete`, `authorized` | `approved_not_settled` | No | Leave session unpaid; no receipt/grant |
| `credential_created`, `credential_issued`, `payment_method_created` | `credential_created_not_settled` | No | Leave session unpaid; no receipt/grant |
| `failed`, `declined`, `canceled`, `expired`, `rejected` | `failed` | No | Leave session unpaid; no receipt/grant |
| very explicit terminal-ish values: `paid`, `settled`, `payment_succeeded`, `merchant_charge_succeeded` | `settled` | Parser-only evidence | Endpoint still fails closed and returns `settlement_mapping_required`; no receipt/grant |

The parser intentionally does not over-map vague status values. Current fake/demo statuses remain non-settled.

## Required settlement signal

Payjent should issue a receipt/grant only after verified terminal payment evidence exists, such as a successful merchant charge or a future Link MPP/payment confirmation. The exact terminal signal and verification mapping are intentionally still to be defined.

## Fail-closed rule

The next planned Link endpoint may poll Link status, but it must fail closed until Payjent maps a verified terminal settlement signal. Non-terminal Link states, approval-only states, credential-created states, missing status, or unknown status must leave the Payjent session unpaid and must not issue grants.
