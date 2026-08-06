import math

from cambrian.base.pools import PoolYield
from cambrian.base.score import score_pool
from cambrian.base_config import BaseLPPolicy

POLICY = BaseLPPolicy()


def mk(label, tvl, fee_apr, swap, toks=("0xa", "0xb")):
    return PoolYield("dex", "0xpool", label, tvl, fee_apr, swap, None, toks)


def test_stable_pair_is_core_low_risk():
    s = score_pool(mk("USDC/USDT", 5_000_000, 0.06, 0.06), POLICY)
    assert s.pair_class == "stable" and s.risk == "low" and s.sleeve == "core"
    assert math.isclose(s.effective_apr, 0.06)
    assert math.isclose(s.score, 0.06 - POLICY.il_stable)


def test_emissions_are_discounted():
    # USDC/AERO: 4% fees + 20% emissions -> effective 4% + 40%*20% = 12%.
    s = score_pool(mk("USDC/AERO", 10_000_000, 0.24, 0.04), POLICY)
    assert math.isclose(s.effective_apr, 0.04 + POLICY.emission_credit * 0.20)
    assert s.risk == "med"  # AERO is a known bluechip -> volatile, not exotic


def test_exotic_token_goes_satellite():
    s = score_pool(mk("PEPE/WETH", 2_000_000, 0.80, 0.10), POLICY)
    assert s.pair_class == "exotic" and s.sleeve == "satellite"


def test_thin_tvl_rejected():
    assert score_pool(mk("USDC/USDT", 10_000, 0.06, 0.06), POLICY).sleeve == "reject"


def test_no_yield_rejected():
    assert score_pool(mk("USDC/USDT", 5_000_000, None, None), POLICY).sleeve == "reject"


def test_yield_below_floor_rejected():
    # Effective yield under min_effective_apr (3%).
    s = score_pool(mk("USDC/USDT", 5_000_000, 0.01, 0.01), POLICY)
    assert s.sleeve == "reject"


def test_il_can_sink_a_volatile_pool():
    # Exotic pool whose discounted yield doesn't cover its 20% IL cost.
    s = score_pool(mk("FOO/BAR", 1_000_000, 0.10, 0.02), POLICY)
    # effective = 0.02 + 0.4*0.08 = 0.052; minus il_exotic 0.20 -> negative
    assert s.score < 0 and s.sleeve == "reject"
