import dataclasses

from cambrian.base.pools import PoolYield
from cambrian.base_config import BaseLPLimits
from cambrian.desks import base_lp
from cambrian.desks.base_lp import BaseLPState

TRUSTED = {"aerodrome-v2"}
LIMITS = BaseLPLimits()


def pool(**over) -> PoolYield:
    base = dict(dex="aerodrome-v2", address="0xpool", label="WETH/USDC",
                tvl_usd=2_000_000.0, fee_apr=0.20, volume_usd=1_000_000.0,
                token_addrs=("0xweth", "0xusdc"))
    base.update(over)
    return PoolYield(**base)


def ev(p=None, position=250.0, state=None, limits=LIMITS):
    return base_lp.evaluate(p or pool(), position,
                            state or BaseLPState(0.0, 0),
                            limits=limits, trusted_dexes=TRUSTED)


def test_clean_pool_approved():
    assert ev().approved


def test_untrusted_dex_rejected():
    d = ev(pool(dex="rando-dex"))
    assert d.rejected and any("trusted venues" in r for r in d.reasons)


def test_low_tvl_rejected():
    d = ev(pool(tvl_usd=100_000.0))
    assert d.rejected and any("below floor" in r for r in d.reasons)


def test_fee_below_floor_rejected():
    d = ev(pool(fee_apr=0.02))
    assert d.rejected and any("below floor" in r for r in d.reasons)


def test_unknown_fee_fails_closed():
    d = ev(pool(fee_apr=None))
    assert d.rejected and any("fee APR unknown" in r for r in d.reasons)


def test_unknown_tvl_fails_closed():
    d = ev(pool(tvl_usd=None))
    assert d.rejected and any("TVL unknown" in r for r in d.reasons)


def test_position_cap_rejected():
    d = ev(position=600.0)
    assert d.rejected and any("per-position cap" in r for r in d.reasons)


def test_total_deployed_cap_rejected():
    d = ev(position=500.0, state=BaseLPState(1_800.0, 1))
    assert d.rejected and any("deployed" in r for r in d.reasons)


def test_max_positions_rejected():
    d = ev(state=BaseLPState(0.0, 5))
    assert d.rejected and any("positions" in r for r in d.reasons)


def test_halted_rejected():
    d = ev(limits=dataclasses.replace(LIMITS, halted=True))
    assert d.rejected and any("halted" in r for r in d.reasons)
