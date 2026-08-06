"""The facts about a fresh token/pool the scorer reasons over.

Like the desks' snapshots: plain frozen data, `None` means "couldn't determine"
(and the scorer treats unknown rug-risk facts as a rejection — fail closed).
Fetching these from RPC/Blockscout is the feed's job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerCandidate:
    token: str
    pool: str
    launchpad: str | None            # factory that minted it
    symbol: str | None = None
    age_minutes: float | None = None
    liquidity_usd: float | None = None
    top_holder_pct: float | None = None   # fraction (0.20 == 20%)
    holders: int | None = None
    holders_5m_ago: int | None = None
    volume_5m_usd: float | None = None
    volume_prior_5m_usd: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    smart_money_buyers: int = 0       # count of WATCHED_WALLETS that bought
    lp_locked: bool | None = None
    hook: str | None = None           # v4 hook (None = no hook)
