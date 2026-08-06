from cambrian.base.monitor import Holding, assess, check_position, circuit_breaker
from cambrian.base_config import MonitorPolicy

POLICY = MonitorPolicy()


def hold(**over):
    base = dict(pool="0x1", label="WETH/AERO", usd=1000, is_stable=False,
                entry_apr=0.40, current_apr=0.40, entry_tvl=5_000_000,
                current_tvl=5_000_000, entry_price=100.0, current_price=100.0)
    base.update(over)
    return Holding(**base)


def test_healthy_position_holds():
    assert check_position(hold(), POLICY).action == "HOLD"


def test_apr_decay_rotates():
    t = check_position(hold(current_apr=0.15), POLICY)  # 15% < 50% of 40%
    assert t.action == "ROTATE" and any("APR" in r for r in t.reasons)


def test_tvl_drain_rotates():
    t = check_position(hold(current_tvl=1_000_000), POLICY)  # < 50% of 5M
    assert t.action == "ROTATE" and any("TVL" in r for r in t.reasons)


def test_price_stop_rotates():
    t = check_position(hold(current_price=70.0), POLICY)  # down 30% > 25% stop
    assert t.action == "ROTATE" and any("down" in r for r in t.reasons)


def test_stable_position_ignores_price():
    t = check_position(hold(is_stable=True, current_price=70.0), POLICY)
    assert t.action == "HOLD"


def test_market_dump_flees_to_stables():
    holdings = [hold(pool="0x1", label="WETH/AERO", is_stable=False),
                hold(pool="0x2", label="USDC/USDT", is_stable=True)]
    r = assess(holdings, market_drawdown=0.20)  # >= 15%
    assert r.go_stable
    actions = {t.label: t.action for t in r.triggers}
    assert actions["WETH/AERO"] == "ROTATE"   # volatile flees
    assert actions["USDC/USDT"] == "HOLD"     # already stable, stays


def test_book_drawdown_trips_breaker():
    go, reasons = circuit_breaker(None, 0.25, POLICY)  # >= 20%
    assert go and any("book down" in r for r in reasons)


def test_no_breaker_when_calm():
    r = assess([hold()], market_drawdown=0.05, portfolio_drawdown=0.05)
    assert not r.go_stable and r.rotations == ()
