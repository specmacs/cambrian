from cambrian.base.allocate import recommend
from cambrian.units import impermanent_loss


def test_lp_wins_when_net_beats_lending():
    a = recommend("0xtok", lp_apr=0.20, lend_apr=0.05, stress_price_ratio=1.5)
    assert a.choice == "LP"
    assert a.lp_net_apr == 0.20 - impermanent_loss(1.5)


def test_lending_wins_after_il_haircut():
    # LP 3% minus ~2% IL = ~1% net, loses to 5% lending.
    a = recommend("0xtok", lp_apr=0.03, lend_apr=0.05, stress_price_ratio=1.5)
    assert a.choice == "LEND"


def test_only_lending_available():
    a = recommend("0xtok", lp_apr=None, lend_apr=0.04)
    assert a.choice == "LEND"


def test_only_lp_available():
    a = recommend("0xtok", lp_apr=0.20, lend_apr=None)
    assert a.choice == "LP"


def test_nothing_available():
    a = recommend("0xtok", lp_apr=None, lend_apr=None)
    assert a.choice == "NONE"
