"""The hands — turning decided actions into positions.

Two executors, same shape as the RH-side execution boundary:

  * `PaperExecutor` — journals every intended move and returns the new position
    state (which, after executing the whole action list, is exactly the target).
    Works today, moves nothing.

  * `LiveBaseExecutor` — the real signer/broadcaster. Intentionally unbuilt: it
    refuses, and its message is the build checklist. Placing an Aerodrome/Uniswap
    LP position for real means a key-segregated signer, the position-manager
    contract, token approvals (Permit2), slippage + deadline, and MEV-aware
    submission. A stubbed signer that *looks* like it works is worse than one
    that refuses, so this stays a deliberate seam until it's built and tested
    dry-run → tiny size → scale.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..base.portfolio import Portfolio
from ..base.rebalance import Action
from ..journal import Journal


class BaseExecutor(Protocol):
    def execute(self, actions: list[Action], final_target: Portfolio,
                journal: Journal) -> dict[str, dict[str, Any]]: ...


class PaperExecutor:
    """Journals intent, moves no funds. The new state is the reconciled target."""

    def execute(self, actions: list[Action], final_target: Portfolio,
                journal: Journal) -> dict[str, dict[str, Any]]:
        for a in actions:
            journal.record("order", {
                "dry_run": True, "desk": "base-lp", "order_kind": a.kind.lower(),
                "subject": a.pool, "label": a.label, "notional_usd": a.to_usd,
            })
        return {
            p.pool: {"usd": p.usd, "label": p.label, "sleeve": p.sleeve,
                     "entry_apr": p.entry_apr, "entry_tvl": p.entry_tvl}
            for p in final_target.positions
        }


class LiveExecutionBlocked(RuntimeError):
    pass


class LiveBaseExecutor:
    def execute(self, actions: list[Action], final_target: Portfolio,
                journal: Journal) -> dict[str, dict[str, Any]]:  # pragma: no cover
        raise LiveExecutionBlocked(
            "live execution is not built. To wire it: a key-segregated signer for "
            "the LP wallet, the DEX position-manager contract, Permit2 approvals, "
            "slippage + deadline, and MEV-aware submission — then gate on DRY_RUN "
            "off + config complete, and roll out dry-run -> tiny size -> scale."
        )
