"""Read a flap token's tax rate, and gate on it.

Owner's rule, non-negotiable: **never touch a token taxed above 3%.**

This is not a theoretical filter. Sampling 120 consecutive live flap launches:

    10.0%  x74      7.3% x11     6.3% x6      5.3% x4      4.3% x5
     9.3%  x4       8.3% x4      3.3% x4      3.0% x1      2.3% x1
     1.3%  x5       1.0% x1

62% of flap launches carry the maximum 10% tax, and only **8 of 120 (~7%)** sit at
or below 3%. So the gate removes about 93% of flap flow. That is the intended
outcome — a 10% tax presents as slippage in the fill and quietly destroys P&L —
but it means flap's headline launch rate is NOT the tradable rate. Size the
opportunity off the post-gate number.

Note the rates are arbitrary (730, 630, 430 bps ...), not the 1/3/5/10% menu the
public docs describe, so gate on the number rather than on a set of known tiers.

Rates are read straight off the token — `buyTaxRate()` / `sellTaxRate()`, both
uint16 basis points. That is preferred over the Tax Token Helper's
`getTaxTokenInfoV2`, which returns a 20-word struct whose field order is not
published; guessing offsets for a safety gate is exactly the wrong trade.
"""

from __future__ import annotations

# keccak256("buyTaxRate()")[:4] / keccak256("sellTaxRate()")[:4]
BUY_TAX_SELECTOR = "0x691f224f"
SELL_TAX_SELECTOR = "0x24024efd"

# Owner's hard ceiling, in basis points. 300 = 3%.
MAX_TAX_BPS = 300


def _read_u16(client, token: str, selector: str) -> int | None:
    try:
        raw = client.eth_call(token, selector)
    except Exception:
        return None
    if not raw or raw in ("0x", "0x0"):
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def read_tax_bps(client, token: str) -> dict[str, int | None]:
    """{'buy': bps, 'sell': bps} for a token; either may be None if unreadable.

    A plain (non-taxed) ERC-20 has no such function, so the call reverts and both
    come back None. None means UNKNOWN, never 0 — see `tax_ok`.
    """
    return {"buy": _read_u16(client, token, BUY_TAX_SELECTOR),
            "sell": _read_u16(client, token, SELL_TAX_SELECTOR)}


def tax_ok(tax: dict[str, int | None], *, max_bps: int = MAX_TAX_BPS,
           allow_unknown: bool = True) -> bool:
    """True if both legs are known-and-within-limit, or plainly untaxed.

    `allow_unknown=True` treats "no tax function" as untaxed, which is correct for
    ordinary ERC-20s from pons and pools.trade — otherwise this gate would reject
    every non-flap token on the chain. Pass allow_unknown=False on flap tokens
    specifically, where a missing read means the call failed rather than that the
    token is clean, and failing closed is the safer read.
    """
    buy, sell = tax["buy"], tax["sell"]
    if buy is None and sell is None:
        return allow_unknown
    for leg in (buy, sell):
        if leg is None:
            if not allow_unknown:
                return False
            continue
        if leg > max_bps:
            return False
    return True


def worst_tax_bps(tax: dict[str, int | None]) -> int | None:
    """The higher of the two legs, for display and for sizing the sell haircut."""
    legs = [v for v in (tax.get("buy"), tax.get("sell")) if v is not None]
    return max(legs) if legs else None
