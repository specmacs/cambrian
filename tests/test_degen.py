"""The degen desk's gates, one rejection at a time.

Strategy: start from a candidate that passes everything, then break exactly one
thing per test and assert it (and only it) is the reason for rejection. That way
a loosened limit can't hide behind another gate.
"""

import dataclasses

from cambrian.config import DegenLimits
from cambrian.desks import degen
from cambrian.snapshots import DegenCandidate, DegenState

FACTORY = "0xFacToRy00000000000000000000000000000001"
HOOK = "0xH00K000000000000000000000000000000000002"
TOKEN = "0xToKeN00000000000000000000000000000000003"

TRUSTED = {FACTORY: "test launchpad"}
REVIEWED = {HOOK}
LIMITS = DegenLimits()


def make_candidate(**over) -> DegenCandidate:
    base = dict(
        token=TOKEN,
        factory=FACTORY,
        hook=None,
        pool_liquidity_usd=80_000.0,
        top_holder_pct=0.12,
        expected_slippage_bps=150.0,
        notional_usd=20.0,
        symbol="TESTTOK",
    )
    base.update(over)
    return DegenCandidate(**base)


def make_state(**over) -> DegenState:
    base = dict(open_positions=1, trades_last_hour=0, daily_notional_usd=0.0)
    base.update(over)
    return DegenState(**base)


def evaluate(candidate=None, state=None, limits=LIMITS):
    return degen.evaluate(
        candidate or make_candidate(),
        state or make_state(),
        limits=limits,
        trusted_factories=TRUSTED,
        reviewed_hooks=REVIEWED,
    )


def test_clean_candidate_approved():
    d = evaluate()
    assert d.approved, d.reasons


def test_halted_desk_rejects():
    d = evaluate(limits=dataclasses.replace(LIMITS, halted=True))
    assert d.rejected
    assert any("halted" in r for r in d.reasons)


def test_notional_over_cap_rejects():
    d = evaluate(make_candidate(notional_usd=26.0))
    assert d.rejected
    assert any("per-trade cap" in r for r in d.reasons)


def test_daily_notional_cap_rejects():
    d = evaluate(make_candidate(notional_usd=25.0), make_state(daily_notional_usd=80.0))
    assert d.rejected
    assert any("daily notional" in r for r in d.reasons)


def test_max_open_positions_rejects():
    d = evaluate(state=make_state(open_positions=4))
    assert d.rejected
    assert any("open positions" in r for r in d.reasons)


def test_trades_per_hour_rejects():
    d = evaluate(state=make_state(trades_last_hour=3))
    assert d.rejected
    assert any("trades this hour" in r for r in d.reasons)


def test_low_liquidity_rejects():
    d = evaluate(make_candidate(pool_liquidity_usd=49_999.0))
    assert d.rejected
    assert any("liquidity" in r for r in d.reasons)


def test_high_slippage_rejects():
    d = evaluate(make_candidate(expected_slippage_bps=301.0))
    assert d.rejected
    assert any("slippage" in r for r in d.reasons)


def test_top_holder_concentration_rejects():
    d = evaluate(make_candidate(top_holder_pct=0.25))
    assert d.rejected
    assert any("top holder" in r for r in d.reasons)


def test_untrusted_factory_rejects():
    d = evaluate(make_candidate(factory="0xdeadbeef00000000000000000000000000000000"))
    assert d.rejected
    assert any("trusted allowlist" in r for r in d.reasons)


def test_missing_factory_rejects():
    d = evaluate(make_candidate(factory=None))
    assert d.rejected
    assert any("trusted allowlist" in r for r in d.reasons)


def test_no_hook_is_fine():
    d = evaluate(make_candidate(hook=None))
    assert d.approved, d.reasons


def test_unreviewed_hook_rejects():
    d = evaluate(make_candidate(hook="0xUnReViEwEd0000000000000000000000000000ff"))
    assert d.rejected
    assert any("source-reviewed" in r for r in d.reasons)


def test_reviewed_hook_approved():
    d = evaluate(make_candidate(hook=HOOK))
    assert d.approved, d.reasons


def test_unknown_facts_fail_closed():
    d = evaluate(make_candidate(pool_liquidity_usd=None,
                                top_holder_pct=None,
                                expected_slippage_bps=None))
    assert d.rejected
    assert any("liquidity unknown" in r for r in d.reasons)
    assert any("slippage unknown" in r for r in d.reasons)
    assert any("concentration unknown" in r for r in d.reasons)


def test_case_insensitive_allowlist_match():
    d = evaluate(make_candidate(factory=FACTORY.lower()))
    assert d.approved, d.reasons
