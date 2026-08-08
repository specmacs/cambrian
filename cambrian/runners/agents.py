"""Specialist agents + a confluence engine for the runner terminal.

Each agent is a pro at ONE thing and emits a 0..1 score with a short reason. The
confluence engine combines them — you act only when enough independent agents
agree, not on any single signal. Pure and fail-closed (unknown -> low/neutral,
never a false green). The terminal renders these; the LLM 'PM' gets them all and
makes the final call.

Adding an agent = add a function that returns (score, reason) and a weight in
CONFLUENCE_WEIGHTS. That's the whole extension point.
"""

from __future__ import annotations

from .flap_tax import MAX_TAX_BPS


def agent_flow(net_usd: float | None, gross_usd: float | None,
               flow_target: float = 5_000.0) -> tuple[float, str]:
    """Is real money accumulating? Signed WETH in, immune to trade-count games."""
    if not gross_usd or gross_usd <= 0:
        return 0.0, "no volume"
    if net_usd is None or net_usd <= 0:
        return 0.0, "net outflow/flat"
    return round(min(net_usd / flow_target, 1.0), 3), f"+${net_usd:,.0f} in"


def agent_sniper(share: float | None) -> tuple[float, str]:
    """Was the early buying just launch-block bots? Lower share scores higher."""
    if share is None:
        return 0.4, "sniper unknown"
    return round(max(0.0, 1.0 - min(max(share, 0.0), 1.0)), 3), f"{share:.0%} sniped"


def agent_farm(fanout: int | None, cap: int = 25) -> tuple[float, str]:
    """Deployer fanning tokens to fresh wallets (faked holders)? Lower is better."""
    if fanout is None:
        return 0.4, "fanout unknown"
    return round(max(0.0, 1.0 - fanout / (2.0 * cap)), 3), f"{fanout} fan-out"


def agent_momentum(buys: int | None, sells: int | None) -> tuple[float, str]:
    """Directional buy pressure (context only — counts are gameable, weighted low)."""
    total = (buys or 0) + (sells or 0)
    if total == 0:
        return 0.0, "no trades"
    skew = buys / total
    return round(max((skew - 0.5) * 2, 0.0), 3), f"{skew:.0%} buys"


def agent_safety(pad_verified: bool, sellable: bool | None,
                 tax_bps: int | None = None) -> tuple[float, str]:
    """The rug/honeypot/tax gate. A token from a VERIFIED pad (hook/deployer proof)
    runs the pad's standard template — safe. Otherwise we trust the sell-sim:
    sellable -> ok, not sellable -> honeypot (hard zero).

    Tax outranks everything, including pad verification. Owner's rule: never touch
    a token taxed above 3%. A flap token can be perfectly "verified" and perfectly
    sellable and still hand back 10% on the way out — the tax is a certain loss,
    not a risk, so it is checked FIRST and returns a hard zero.
    """
    if tax_bps is not None and tax_bps > MAX_TAX_BPS:
        return 0.0, f"TAX {tax_bps / 100:.2g}% > {MAX_TAX_BPS / 100:.0f}% limit"
    if pad_verified:
        return 1.0, "verified pad (standard contract)"
    if sellable is True:
        return 0.8, "sim: sellable"
    if sellable is False:
        return 0.0, "HONEYPOT / can't exit"
    return 0.3, "unverified, unchecked"


def agent_smart_money(watched_buyers: int | None) -> tuple[float, str]:
    """Are wallets you track buying? The strongest single signal when present."""
    if not watched_buyers:
        return 0.0, "no tracked wallets in"
    return round(min(watched_buyers / 3.0, 1.0), 3), f"{watched_buyers} smart wallet(s)"


# Confluence weights — how much each pro's vote counts. Safety is special: it's a
# GATE, not a vote (a honeypot can't be outvoted). The rest form the score.
CONFLUENCE_WEIGHTS = {
    "flow": 0.34,
    "smart_money": 0.24,
    "sniper": 0.16,
    "momentum": 0.10,
    "farm": 0.16,
}


def confluence(signals: dict[str, float], *, weights: dict[str, float] | None = None,
               safety: float | None = None, min_agree: int = 3,
               agree_at: float = 0.5) -> dict:
    """Combine specialist scores into one confluence verdict.

    signals: {name: score} for the weighted voters (exclude safety).
    safety : the safety agent's score — a GATE: 0 hard-blocks regardless of votes.
    Returns {score, agree, gated, tier}. tier: strong / watch / weak / blocked.
    A buy needs a high score AND >= min_agree agents above agree_at AND safety>0.
    """
    weights = weights or CONFLUENCE_WEIGHTS
    wsum = sum(weights.get(k, 0.0) for k in signals) or 1.0
    score = round(sum(signals.get(k, 0.0) * weights.get(k, 0.0) for k in signals) / wsum, 3)
    agree = sum(1 for k, v in signals.items() if v >= agree_at)
    gated = safety is not None and safety <= 0.0
    if gated:
        tier = "blocked"
    elif score >= 0.60 and agree >= min_agree:
        tier = "strong"
    elif score >= 0.40 and agree >= max(min_agree - 1, 1):
        tier = "watch"
    else:
        tier = "weak"
    return {"score": score, "agree": agree, "gated": gated, "tier": tier}
