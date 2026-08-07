"""The auto-buy decision: should this fresh runner be bought, right now, with real
money? Pure and fail-closed — every unknown or breached limit is a NO.

This is the guardrail brain for examples/rh_autotrade.py. It never signs, never
holds a key, never touches the network. It only answers "buy or not, and how
much" given a scored candidate and the wallet's current state. Keeping it pure
means every safety rule is unit-tested with no chain access.

Design stance: an auto-sniper on brand-new memecoins loses money by default. The
defaults here are deliberately strict — only strong signals, only exitable pools,
hard per-trade and total caps, one position per token, a cooldown, and a daily
loss kill-switch. Loosen them knowingly, not by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradePolicy:
    # Sizing (in the contra asset — WETH by default).
    spend_per_trade: float = 0.02          # buy this much per runner
    max_total_deployed: float = 0.10        # never have more than this at risk at once
    max_positions: int = 5                  # cap concurrent open positions
    # Quality gates (defense in depth — the scorer already discounts these).
    min_score: float = 0.60                 # HOT only
    require_pad: str | None = None          # e.g. "bankr" to only buy one pad
    min_liquidity_usd: float = 20_000.0     # must be deep enough to EXIT
    max_sniper_share: float = 0.5           # skip bot-sniped launches
    max_fanout: int = 25                    # skip farmed books
    # Pacing + risk.
    cooldown_seconds: float = 60.0          # min gap between buys
    max_slippage: str = "0.05"
    stop_pct: float = 0.35                  # rest a stop-loss this far below fill
    daily_loss_limit: float = 0.04          # halt buying if down this much on the day


@dataclass
class Wallet:
    """Live trading state — what's open, what's been spent, what's been seen."""
    positions: dict = field(default_factory=dict)   # token -> {spend, entry_ts, ...}
    spent: float = 0.0                              # currently deployed (contra units)
    realized_pnl: float = 0.0                       # today, contra units (neg = loss)
    last_buy_ts: float = 0.0
    seen: set = field(default_factory=set)          # tokens already acted on

    def remaining_budget(self, policy: TradePolicy) -> float:
        return max(policy.max_total_deployed - self.spent, 0.0)


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str
    spend: float = 0.0          # contra units to spend if ok


def should_buy(*, token: str, score: float, pad: str | None,
               liquidity_usd: float | None, sniper_share: float | None,
               fanout: int | None, wallet: Wallet, policy: TradePolicy,
               now: float) -> Decision:
    """Fail-closed buy gate. Returns Decision(ok, reason, spend)."""
    # Kill-switch: stop for the day if losses breached the limit.
    if -wallet.realized_pnl >= policy.daily_loss_limit:
        return Decision(False, "daily loss limit hit — halted")

    # One shot per token, ever (dedupe re-discovery and re-alerts).
    if token in wallet.seen or token in wallet.positions:
        return Decision(False, "already acted on this token")

    # Signal quality.
    if score < policy.min_score:
        return Decision(False, f"score {score:.2f} < {policy.min_score:.2f}")
    if policy.require_pad and pad != policy.require_pad:
        return Decision(False, f"pad {pad or '?'} != {policy.require_pad}")

    # Exitability + rug defense (fail closed on unknowns).
    if liquidity_usd is None or liquidity_usd < policy.min_liquidity_usd:
        return Decision(False, f"liquidity below ${policy.min_liquidity_usd:,.0f} (can't exit)")
    if sniper_share is not None and sniper_share > policy.max_sniper_share:
        return Decision(False, f"{sniper_share:.0%} sniped")
    if fanout is not None and fanout >= policy.max_fanout:
        return Decision(False, f"{fanout} fan-out wallets")

    # Pacing.
    if now - wallet.last_buy_ts < policy.cooldown_seconds:
        return Decision(False, "cooldown")

    # Capacity + budget.
    if len(wallet.positions) >= policy.max_positions:
        return Decision(False, f"at max {policy.max_positions} positions")
    budget = wallet.remaining_budget(policy)
    if budget <= 0:
        return Decision(False, "total-deployed cap reached")

    spend = min(policy.spend_per_trade, budget)
    if spend <= 0:
        return Decision(False, "no budget for a full-size buy")
    return Decision(True, "ok", spend)


def record_buy(wallet: Wallet, token: str, spend: float, now: float,
               entry_usd: float | None = None) -> None:
    """Apply an executed buy to the wallet state."""
    wallet.positions[token] = {"spend": spend, "entry_ts": now, "entry_usd": entry_usd}
    wallet.spent += spend
    wallet.last_buy_ts = now
    wallet.seen.add(token)


def record_close(wallet: Wallet, token: str, pnl: float) -> None:
    """Apply a closed position (stop-loss or take-profit) to the wallet state."""
    pos = wallet.positions.pop(token, None)
    if pos:
        wallet.spent = max(wallet.spent - pos["spend"], 0.0)
        wallet.realized_pnl += pnl
