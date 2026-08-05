"""The execution boundary.

A `Decision` only says "this trade is allowed". Turning that into an actual
order is a separate, deliberately guarded step. Two executors exist:

  * `DryRunExecutor` — the default and the only one that works today. It
    journals the order it *would* have placed and returns a synthetic receipt.
    Everything upstream can run end to end against it, safely, forever.

  * `LiveExecutor` — the seam where real transaction signing and broadcasting
    plug in. It is intentionally not implemented. Constructing one performs the
    fail-closed preflight (config complete, DRY_RUN off, desk live) and then
    raises, so nobody flips to live by accident or by editing a single flag.

Signing is not stubbed with a fake because a fake signer is worse than none:
it invites running the live path while believing it's safe. Going live is a
project of its own — wire a real signer here, keep the desks' keys segregated,
and only then remove the guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config
from .journal import Journal


@dataclass(frozen=True)
class Order:
    desk: str
    #: "buy" | "sell" | "add_liquidity" | "remove_liquidity"
    kind: str
    subject: str
    notional_usd: float
    #: Wallet the order draws on. Must be the desk's own wallet — see the
    #: segregation check in `LiveExecutor`.
    wallet: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Receipt:
    accepted: bool
    dry_run: bool
    order: Order
    detail: str
    tx_hash: str | None = None


class Executor(Protocol):
    def submit(self, order: Order) -> Receipt: ...


class DryRunExecutor:
    """Records intent, moves no funds. Safe to run against anything."""

    def __init__(self, journal: Journal):
        self.journal = journal

    def submit(self, order: Order) -> Receipt:
        self.journal.record(
            "order",
            {
                "dry_run": True,
                "desk": order.desk,
                "order_kind": order.kind,
                "subject": order.subject,
                "notional_usd": order.notional_usd,
                "wallet": order.wallet,
                "params": order.params,
            },
        )
        return Receipt(
            accepted=True,
            dry_run=True,
            order=order,
            detail="dry run: journaled, not broadcast",
            tx_hash=None,
        )


class LiveExecutionBlocked(RuntimeError):
    """Raised when something tries to trade for real before it's safe to."""


class LiveExecutor:
    """Placeholder for the real signing/broadcast path. Refuses to exist until
    the world is safe, and even then refuses to *act* — signing is unbuilt."""

    def __init__(self, wallet: str):
        blockers = preflight_live_blockers()
        if blockers:
            raise LiveExecutionBlocked(
                "cannot go live:\n  - " + "\n  - ".join(blockers)
            )
        self.wallet = wallet

    def submit(self, order: Order) -> Receipt:  # pragma: no cover - unbuilt
        raise NotImplementedError(
            "live execution is not implemented. Wire a real, key-segregated "
            "transaction signer here before removing this guard."
        )


def preflight_live_blockers() -> list[str]:
    """Reasons live trading must not proceed right now. Empty list == clear."""
    blockers: list[str] = []
    if config.DRY_RUN:
        blockers.append("DRY_RUN is on (set DRY_RUN=false only when you mean it)")
    gaps = config.missing_config()
    if gaps:
        blockers.append("config incomplete: " + ", ".join(gaps))
    return blockers


def executor_for(journal: Journal, wallet: str) -> Executor:
    """Return the executor appropriate to the current mode. In DRY_RUN (the
    default), you always get the dry-run executor. Only a fully-configured,
    DRY_RUN=false setup even attempts the live path — which then raises, by
    design, until signing is built."""
    if config.DRY_RUN:
        return DryRunExecutor(journal)
    return LiveExecutor(wallet)
