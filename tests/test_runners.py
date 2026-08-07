from cambrian.runners.config import RunnerPolicy
from cambrian.runners.score import rank_runners, score_runner
from cambrian.runners.snapshots import RunnerCandidate

POLICY = RunnerPolicy()


def cand(**over):
    base = dict(token="0xtok", pool="0xpool", launchpad="0xpad", symbol="TEST",
                age_minutes=20, liquidity_usd=40_000, top_holder_pct=0.10,
                holders=150, holders_5m_ago=100, volume_5m_usd=90_000,
                volume_prior_5m_usd=20_000, buys_5m=200, sells_5m=50,
                smart_money_buyers=3, lp_locked=True, hook=None)
    base.update(over)
    return RunnerCandidate(**base)


def test_strong_runner_is_hot():
    s = score_runner(cand(), policy=POLICY)
    assert s.tier == "hot" and s.score >= POLICY.min_score_hot
    assert any("smart wallet" in sig for sig in s.signals)


def test_high_top_holder_rejected():
    s = score_runner(cand(top_holder_pct=0.44), policy=POLICY)
    assert s.tier == "reject" and any("top-holder" in r for r in s.reasons)


def test_lp_not_locked_rejected():
    s = score_runner(cand(lp_locked=False), policy=POLICY)
    assert s.tier == "reject" and any("LP not locked" in r for r in s.reasons)


def test_thin_liquidity_rejected():
    s = score_runner(cand(liquidity_usd=1_000), policy=POLICY)
    assert s.tier == "reject" and any("liquidity" in r for r in s.reasons)


def test_stale_rejected():
    s = score_runner(cand(age_minutes=999), policy=POLICY)
    assert s.tier == "reject" and any("fresh" in r for r in s.reasons)


def test_unknown_rug_facts_fail_closed():
    s = score_runner(cand(top_holder_pct=None, liquidity_usd=None), policy=POLICY)
    assert s.tier == "reject"


def test_quiet_token_is_cold_not_flagged():
    # clean but no momentum: no smart money, flat volume/holders/buys
    s = score_runner(cand(smart_money_buyers=0, volume_5m_usd=7_000,
                          volume_prior_5m_usd=7_000, holders_5m_ago=148,
                          buys_5m=30, sells_5m=30), policy=POLICY)
    assert s.tier == "cold"


def test_net_outflow_is_cold_despite_buy_counts():
    # Counts look bullish (200b/50s) but net WETH is leaving = distribution.
    s = score_runner(cand(net_flow_usd=-1_500), policy=POLICY)
    assert s.tier == "cold" and any("outflow" in x for x in s.signals)


def test_net_inflow_adds_signal():
    s = score_runner(cand(net_flow_usd=8_000), policy=POLICY)
    assert any("net +$" in x for x in s.signals)


def test_snipers_discount_the_score():
    base = score_runner(cand(), policy=POLICY).score
    sniped = score_runner(cand(sniper_share=0.9), policy=POLICY)
    assert sniped.score < base and any("sniped" in x for x in sniped.signals)


def test_transfer_fanout_discounts_the_score():
    base = score_runner(cand(), policy=POLICY).score
    farmed = score_runner(cand(transfer_fanout=60), policy=POLICY)
    assert farmed.score < base and any("fan-out" in x for x in farmed.signals)


def test_rank_filters_and_orders():
    hot = cand(symbol="HOT", smart_money_buyers=3)
    rug = cand(symbol="RUG", top_holder_pct=0.5)
    quiet = cand(symbol="MEH", smart_money_buyers=0, volume_5m_usd=7_000,
                 volume_prior_5m_usd=7_000, holders_5m_ago=148, buys_5m=30, sells_5m=30)
    ranked = rank_runners([rug, quiet, hot])
    assert [s.candidate.symbol for s in ranked] == ["HOT"]  # rug + quiet dropped
