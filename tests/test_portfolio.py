from cambrian.base.pools import PoolYield
from cambrian.base.score import PoolScore
from cambrian.base.portfolio import build_portfolio
from cambrian.base_config import BaseLPPolicy

POLICY = BaseLPPolicy()


def ps(addr, score, sleeve, toks):
    pool = PoolYield("dex", addr, addr, 5_000_000, 0.10, 0.05, None, toks)
    return PoolScore(pool, "stable", "low", score, 0.005, score, sleeve, ())


def test_respects_per_pool_cap():
    scored = [
        ps("0x1", 0.10, "core", ("0xa", "0xb")),
        ps("0x2", 0.08, "core", ("0xc", "0xd")),
        ps("0x3", 0.06, "core", ("0xe", "0xf")),
    ]
    pf = build_portfolio(10_000, scored, POLICY)
    cap = POLICY.max_per_pool * 10_000  # 2000
    assert all(p.usd <= cap + 1e-6 for p in pf.positions)
    assert abs(pf.deployed_usd + pf.reserve_usd - 10_000) < 0.01


def test_per_token_cap_limits_shared_exposure():
    scored = [
        ps("0x1", 0.10, "core", ("0xshared", "0xa")),
        ps("0x2", 0.10, "core", ("0xshared", "0xb")),
    ]
    pf = build_portfolio(10_000, scored, POLICY)
    shared = sum(p.usd for p in pf.positions)  # both hold 0xshared
    assert shared <= POLICY.max_per_token * 10_000 + 1e-6  # 3500


def test_barbell_splits_core_and_satellite():
    scored = [
        ps("0x1", 0.08, "core", ("0xa", "0xb")),
        ps("0x1b", 0.07, "core", ("0xg", "0xh")),
        ps("0x2", 0.30, "satellite", ("0xc", "0xd")),
    ]
    pf = build_portfolio(10_000, scored, POLICY)
    core = sum(p.usd for p in pf.positions if p.sleeve == "core")
    sat = sum(p.usd for p in pf.positions if p.sleeve == "satellite")
    # satellite is capped below core despite a much higher score (barbell)
    assert core > sat
    assert sat <= POLICY.satellite_target * 10_000 + 1e-6


def test_reserve_holds_when_nothing_qualifies():
    pf = build_portfolio(10_000, [], POLICY)
    assert pf.positions == ()
    assert pf.reserve_usd == 10_000


def test_weights_and_blended_apr():
    scored = [ps("0x1", 0.10, "core", ("0xa", "0xb"))]
    pf = build_portfolio(10_000, scored, POLICY)
    p = pf.positions[0]
    assert abs(p.weight - p.usd / 10_000) < 1e-9
    # blended = usd-weighted APR over full capital (reserve at 0)
    assert abs(pf.blended_apr - p.usd * 0.10 / 10_000) < 1e-9
