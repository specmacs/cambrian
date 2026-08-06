import math

from cambrian.base.pools import (PoolYield, normalize_pool, pools_holding, scan)


def test_normalize_live_aerodrome_columns():
    # The exact columns from the live /evm/aero/v2/pools payload.
    row = {"poolId": "0x6cd", "token0": "0x833", "token0Symbol": "USDC",
           "token1": "0x940", "token1Symbol": "AERO", "poolTvlUsd": 25_757_527.24,
           "volume24hUsd": 1_151_580.11, "swapFeeApr7d": 0.0424,
           "aeroRewardApr7d": 0.1998, "bribeFeeApr7d": None, "totalApr7d": None}
    p = normalize_pool(row, "aerodrome-v2")
    assert p.address == "0x6cd"
    assert p.label == "USDC/AERO"
    assert p.tvl_usd == 25_757_527.24
    assert p.swap_fee_apr == 0.0424
    # totalApr7d was null -> summed from components (swap + reward).
    assert math.isclose(p.fee_apr, 0.0424 + 0.1998, rel_tol=1e-9)
    assert p.token_addrs == ("0x833", "0x940")


def test_normalize_generic_fee_apr_field():
    row = {"pool_address": "0xAbC", "tvl_usd": 1_000_000, "fee_apr": 0.15,
           "token0_symbol": "WETH", "token1_symbol": "USDC"}
    p = normalize_pool(row, "uniswap-v3")
    assert p.swap_fee_apr == 0.15
    assert p.fee_apr == 0.15   # no emissions -> all-in equals swap fees


def test_apr_derived_from_volume_and_fee_tier():
    # No fee_apr column; derive from 30bps fee, $100k volume, $1M TVL.
    row = {"address": "0x1", "tvl_usd": 1_000_000, "volume_usd_24h": 100_000,
           "fee_tier": 30}  # 30 bps -> 0.003
    p = normalize_pool(row, "aerodrome-v2")
    expected = 100_000 * 0.003 * 365 / 1_000_000  # 0.1095
    assert p.fee_apr is not None
    assert math.isclose(p.fee_apr, expected, rel_tol=1e-9)


def test_apr_unknown_when_no_inputs():
    p = normalize_pool({"address": "0x1", "tvl_usd": 1_000_000}, "sushi-v3")
    assert p.fee_apr is None


def test_scan_filters_and_ranks():
    pools = [
        PoolYield("a", "0x1", "A/B", 500_000, 0.10, None, None, (None, None)),
        PoolYield("b", "0x2", "C/D", 100_000, 0.50, None, None, (None, None)),  # below TVL floor
        PoolYield("c", "0x3", "E/F", 2_000_000, 0.30, None, None, (None, None)),
        PoolYield("d", "0x4", "G/H", 900_000, None, None, None, (None, None)),   # unknown APR
    ]
    ranked = scan(pools, min_tvl_usd=250_000)
    assert [p.address for p in ranked] == ["0x3", "0x1"]  # by APR desc, floor+known only


def test_pools_holding_matches_by_address_case_insensitive():
    pools = [
        PoolYield("a", "0x1", "A/B", 1, 1, None, None, ("0xweth", "0xusdc")),
        PoolYield("b", "0x2", "C/D", 1, 1, None, None, ("0xdai", "0xusdc")),
    ]
    got = pools_holding(pools, "0xWETH")
    assert [p.address for p in got] == ["0x1"]
