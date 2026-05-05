"""Deprecated C3PO compatibility shim.

Use :mod:`payjent.agent_bridge` and ``AgentPayjentBridge`` for new integrations.
This module re-exports the generic bridge under the former C3PO names so existing
C3PO/community-agent callers keep working without duplicated logic.
"""

from __future__ import annotations

from payjent.agent_bridge import (
    AgentPayjentBridge,
    JsonFilePendingPremiumRequestStore,
    MemoryPendingPremiumRequestStore,
    PendingPremiumRequest,
    PendingPremiumRequestStore,
    pay_sh_request_hash,
    premium_pay_sh_request_hash,
)

C3POPayjentBridge = AgentPayjentBridge

__all__ = [
    "AgentPayjentBridge",
    "C3POPayjentBridge",
    "PendingPremiumRequest",
    "PendingPremiumRequestStore",
    "MemoryPendingPremiumRequestStore",
    "JsonFilePendingPremiumRequestStore",
    "premium_pay_sh_request_hash",
    "pay_sh_request_hash",
]
