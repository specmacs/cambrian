"""The LP desk's gates, one rejection at a time."""

import dataclasses
from datetime import datetime, timezone

from cambrian.config import LPLimits
from cambrian.desks import lp
from cambrian.snapshots import LPContext, LPPool, LPState

STOCK = "0xSt0ck00000000000000000000000000000000a1"
QUOTE = "0xQu0te00000000000000000000000000000000b2"
POOL = "0xP00l000000000000000000000000000000000c3"

STOCK_TOKENS = {STOCK: "NVDA"}
QUOTE_TOKENS = {QUOTE: "USDG"}
LIMITS = LPLimits()


def make_pool(**over) -> LPPool:
    base = dict(
        pool=POOL,
        stock_token=STOCK,
        quote_token=QUOTE,
        tvl_usd=2_000_000.0,
        fee_apr=0.25,
        position_usd=300.0,
    )
    base.update(over)
    return LPPool(**base)


def make_ctx(**over) -> LPContext:
    base = dict(
        now_utc=datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc),
        ticker="NVDA",
        market_open=True,
        minutes_since_open=90.0,
        minutes_to_close=300.0,
        days_to_earnings=10.0,
    )
    base.update(over)
    return LPContext(**base)


def make_state(**over) -> LPState:
    base = dict(total_deployed_usd=500.0, open_positions=1)
    base.update(over)
    return LPState(**base)


def evaluate(pool=None, ctx=None, state=None, limits=LIMITS):
    return lp.evaluate(
        pool or make_pool(),
        ctx or make_ctx(),
        state or make_state(),
        limits=limits,
        stock_tokens=STOCK_TOKENS,
        quote_tokens=QUOTE_TOKENS,
    )


def test_clean_pool_approved():
    d = evaluate()
    assert d.approved, d.reasons


def test_halted_desk_rejects():
    d = evaluate(limits=dataclasses.replace(LIMITS, halted=True))
    assert d.rejected
    assert any("halted" in r for r in d.reasons)


def test_ineligible_stock_token_rejects():
    d = evaluate(make_pool(stock_token="0xnope00000000000000000000000000000000dead"))
    assert d.rejected
    assert any("STOCK_TOKENS" in r for r in d.reasons)


def test_ineligible_quote_token_rejects():
    d = evaluate(make_pool(quote_token="0xnope00000000000000000000000000000000beef"))
    assert d.rejected
    assert any("QUOTE_TOKENS" in r for r in d.reasons)


def test_position_over_cap_rejects():
    d = evaluate(make_pool(position_usd=501.0))
    assert d.rejected
    assert any("per-position cap" in r for r in d.reasons)


def test_total_deployed_cap_rejects():
    d = evaluate(make_pool(position_usd=500.0), state=make_state(total_deployed_usd=1600.0))
    assert d.rejected
    assert any("deployed capital" in r for r in d.reasons)


def test_max_positions_rejects():
    d = evaluate(state=make_state(open_positions=3))
    assert d.rejected
    assert any("positions" in r for r in d.reasons)


def test_low_tvl_rejects():
    d = evaluate(make_pool(tvl_usd=999_999.0))
    assert d.rejected
    assert any("TVL" in r for r in d.reasons)


def test_market_closed_rejects():
    d = evaluate(ctx=make_ctx(market_open=False, minutes_since_open=None,
                              minutes_to_close=None))
    assert d.rejected
    assert any("market is closed" in r for r in d.reasons)


def test_too_soon_after_open_rejects():
    d = evaluate(ctx=make_ctx(minutes_since_open=5.0))
    assert d.rejected
    assert any("since open" in r for r in d.reasons)


def test_too_close_to_close_rejects():
    d = evaluate(ctx=make_ctx(minutes_to_close=10.0))
    assert d.rejected
    assert any("exit window" in r for r in d.reasons)


def test_earnings_blackout_rejects():
    d = evaluate(ctx=make_ctx(days_to_earnings=0.5))
    assert d.rejected
    assert any("blackout" in r for r in d.reasons)


def test_unknown_earnings_fails_closed():
    d = evaluate(ctx=make_ctx(days_to_earnings=None))
    assert d.rejected
    assert any("earnings" in r and "unknown" in r for r in d.reasons)


def test_fee_apr_below_stress_il_rejects():
    d = evaluate(make_pool(fee_apr=0.001))
    assert d.rejected
    assert any("stress IL" in r for r in d.reasons)


def test_unknown_fee_apr_fails_closed():
    d = evaluate(make_pool(fee_apr=None))
    assert d.rejected
    assert any("fee APR unknown" in r for r in d.reasons)


def test_unknown_tvl_fails_closed():
    d = evaluate(make_pool(tvl_usd=None))
    assert d.rejected
    assert any("TVL unknown" in r for r in d.reasons)
