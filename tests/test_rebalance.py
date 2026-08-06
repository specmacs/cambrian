from cambrian.base.portfolio import Portfolio, Position
from cambrian.base.rebalance import plan_rebalance


def pos(pool, usd, label="P"):
    return Position("dex", pool, label, usd, usd / 10_000, "core", 0.1, "")


def target(*positions):
    return Portfolio(10_000, tuple(positions), 0.0, 0.1)


def test_enters_from_empty():
    t = target(pos("0x1", 2000), pos("0x2", 1000))
    actions = plan_rebalance({}, t)
    assert {a.kind for a in actions} == {"ENTER"}
    assert {a.pool for a in actions} == {"0x1", "0x2"}


def test_exits_dropped_pool():
    t = target(pos("0x1", 2000))
    actions = plan_rebalance({"0x1": 2000, "0x9": 1500}, t)
    exits = [a for a in actions if a.kind == "EXIT"]
    assert [a.pool for a in exits] == ["0x9"]
    assert exits[0].to_usd == 0.0


def test_resize_only_beyond_threshold():
    t = target(pos("0x1", 2000), pos("0x2", 1000))
    # 0x1 drifts by 10 (below min_move) -> no action; 0x2 drifts by 500 -> resize
    actions = plan_rebalance({"0x1": 2010, "0x2": 500}, t)
    kinds = {a.pool: a.kind for a in actions}
    assert "0x1" not in kinds
    assert kinds["0x2"] == "RESIZE"


def test_no_actions_when_on_target():
    t = target(pos("0x1", 2000))
    assert plan_rebalance({"0x1": 2000}, t) == []
