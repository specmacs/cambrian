from cambrian.base.lending import LendingMarket
from cambrian.base.pools import PoolYield
from cambrian.base.opportunities import build_opportunities


def pool(label, tvl, apr, swap):
    return PoolYield("aerodrome-v2", "0x" + label, label, tvl, apr, swap, None,
                     (None, None))


def market(sym, apr, tvl=5_000_000):
    return LendingMarket("morpho", sym, "0x" + sym, apr, tvl)


def test_ranks_lp_and_lending_together_by_apr():
    pools = [pool("WETH/AERO", 5_000_000, 0.30, 0.05),
             pool("USDC/USDT", 8_000_000, 0.06, 0.06)]
    markets = [market("USDC", 0.12), market("WETH", 0.03)]
    opps = build_opportunities(pools, markets, min_tvl_usd=250_000)
    assert [o.apr for o in opps] == [0.30, 0.12, 0.06, 0.03]  # sorted desc
    # both kinds represented, and a lending market outranks a low LP
    kinds = [o.kind for o in opps]
    assert kinds == ["LP", "LEND", "LP", "LEND"]


def test_thin_pools_and_markets_filtered():
    pools = [pool("SCAM/WETH", 10_000, 5.0, 0.01)]        # thin
    markets = [market("X", 0.20, tvl=1_000)]              # thin
    assert build_opportunities(pools, markets, min_tvl_usd=250_000) == []


def test_unknown_apr_skipped():
    pools = [pool("A/B", 5_000_000, None, None)]
    markets = [LendingMarket("aave", "Y", "0xy", None, 5_000_000)]
    assert build_opportunities(pools, markets, min_tvl_usd=250_000) == []


def test_lp_detail_shows_real_fee_share():
    pools = [pool("WETH/AERO", 5_000_000, 0.20, 0.05)]  # 25% real fees
    opps = build_opportunities(pools, [], min_tvl_usd=250_000)
    assert "25% real fees" in opps[0].detail
