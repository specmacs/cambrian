from cambrian.base.portfolio import Portfolio, Position
from cambrian.base.monitor import MonitorReport, Trigger
from cambrian.base.manager import plan_cycle


def pos(pool, usd, label="P"):
    return Position("dex", pool, label, usd, usd / 10_000, "core", 0.1, "",
                    entry_apr=0.2, entry_tvl=5e6)


def target(*positions):
    dep = sum(p.usd for p in positions)
    return Portfolio(10_000, tuple(positions), 10_000 - dep, 0.1)


def calm():
    return MonitorReport((), False, ())


def rotate(pool):
    return MonitorReport((Trigger(pool, "P", "ROTATE", ("decay",)),), False, ())


def breaker():
    return MonitorReport((), True, ("market dump",))


def test_normal_cycle_is_plain_rebalance():
    t = target(pos("0x1", 2000), pos("0x2", 1000))
    final, actions = plan_cycle(t, {}, calm())
    assert final is t
    assert {a.kind for a in actions} == {"ENTER"}


def test_rotation_drops_pool_from_target():
    t = target(pos("0x1", 2000), pos("0x2", 1000))
    # currently hold both; monitor says rotate 0x1 -> it must be exited, not kept
    final, actions = plan_cycle(t, {"0x1": 2000, "0x2": 1000}, rotate("0x1"))
    assert "0x1" not in {p.pool for p in final.positions}
    kinds = {a.pool: a.kind for a in actions}
    assert kinds["0x1"] == "EXIT"


def test_breaker_exits_everything():
    t = target(pos("0x1", 2000), pos("0x2", 1000))
    final, actions = plan_cycle(t, {"0x1": 2000, "0x2": 1000}, breaker())
    assert final.positions == ()
    assert final.reserve_usd == 10_000
    assert {a.kind for a in actions} == {"EXIT"}
    assert len(actions) == 2
