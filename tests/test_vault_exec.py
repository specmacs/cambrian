"""Executing from the Definitive vault.

This is the only module in the repo that spends money, so the tests here are
about what must NOT happen: selling without confirmation, selling something the
chain says you do not hold, firing the same stop thirty times, or marking a
position from anything other than a real exit price.
"""

import pytest

from cambrian.runners import definitive as D
from cambrian.runners import positions as P
from cambrian.runners import vault_exec as VX


class FakeChain:
    def __init__(self, balances=None, decimals=18):
        self.balances = balances or {}
        self._dec = decimals

    def erc20_balance_of(self, token, holder):
        return self.balances.get(token.lower(), 0)

    def erc20_decimals(self, token):
        return self._dec


def test_quantity_is_truncated_to_the_assets_decimals():
    assert VX.fmt_qty(8.0, 6) == "8"
    assert VX.fmt_qty(1.23456789, 6) == "1.234567"
    assert VX.fmt_qty(1.9999999, 6) == "1.999999"     # never rounds up


def test_a_sell_is_refused_when_the_chain_says_you_hold_nothing(monkeypatch):
    # The book is not the authority here; the chain is. Selling against a stale
    # book is a rejected order at the exact moment the exit had to work.
    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: pytest.fail("must not quote"))
    plan = VX.plan_sell(FakeChain(), token="0xabc", vault="0xv")
    assert plan["ok"] is False
    assert "none" in plan["why"]


def test_an_unquotable_sell_is_data_not_an_exception(monkeypatch):
    # It is the unsellable signal the rug rule keys off, so it has to be
    # inspectable rather than something that unwinds the loop.
    def boom(**kw):
        raise D.DefinitiveError("missing notional rates", status=400)

    monkeypatch.setattr(D, "quicktrade_quote", boom)
    plan = VX.plan_sell(FakeChain({"0xabc": 10 ** 18}), token="0xabc", vault="0xv")
    assert plan["ok"] is False
    assert "not quotable" in plan["why"]


def test_executing_an_unquotable_plan_raises_rather_than_guessing():
    with pytest.raises(D.DefinitiveError):
        VX.execute_sell({"ok": False, "why": "nope"}, confirm=True)


def test_execute_sell_still_requires_confirmation(monkeypatch):
    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: {"metadata": {"toNotional": "5"}})
    plan = VX.plan_sell(FakeChain({"0xabc": 10 ** 18}), token="0xabc", vault="0xv")
    assert plan["ok"]
    with pytest.raises(D.DefinitiveError):
        VX.execute_sell(plan)            # confirm defaults to False


def test_partial_exits_are_expressible(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"metadata": {"toNotional": "5"}}

    monkeypatch.setattr(D, "quicktrade_quote", fake)
    VX.plan_sell(FakeChain({"0xabc": 4 * 10 ** 18}), token="0xabc", vault="0xv",
                 fraction=0.25)
    assert seen["qty"] == "1"            # a quarter of four tokens
    assert seen["side"] == "sell"


def test_the_guard_stops_one_stop_becoming_thirty():
    # The mark loop runs every couple of seconds and an intent stays true until
    # the position changes, so without this a single stop is a stream of sells.
    g = VX.ExitGuard(cooldown_s=60)
    assert g.allow("0xABC", now=1000) is True
    g.note("0xABC", now=1000)
    assert g.allow("0xabc", now=1030) is False      # case-insensitive
    assert g.allow("0xabc", now=1061) is True


def test_a_refused_exit_still_starts_the_cooldown():
    # Otherwise a venue that cannot quote gets hammered every tick forever.
    g = VX.ExitGuard(cooldown_s=30)
    g.note("0xdead", now=500)
    assert g.allow("0xdead", now=510) is False


# --- marking from the exit quote ---------------------------------------------

def _pos(cost=10.0):
    return P.Position(token="0xabc", venue_kind="vault", pool="", tokens=10 ** 18,
                      cost_usd=cost, opened_at=0.0, peak_usd=cost)


def test_a_quoted_mark_is_exact_by_construction():
    # The number IS the exit price — route, tax and this size's slippage are all
    # already inside it — so this is not the optimistic spot fallback.
    pos = P.mark_from_exit_quote(_pos(), 12.5)
    assert pos.mark_usd == 12.5
    assert pos.mark_is_exact is True
    assert pos.peak_usd == 12.5
    assert pos.fails == 0


def test_an_unquotable_sell_marks_to_zero_and_counts_a_failure():
    # Preserving the last good mark is how a honeypot renders as a winner.
    pos = _pos()
    P.mark_from_exit_quote(pos, 12.0)
    P.mark_from_exit_quote(pos, None)
    assert pos.mark_usd == 0.0
    assert pos.fails == 1


def test_repeated_unsellability_becomes_a_rug_exit():
    pos = _pos()
    for _ in range(P.RUG_FAILS):
        P.mark_from_exit_quote(pos, None)
    action, fraction, why = P.exit_decision(pos)
    assert action == "close" and fraction == 1.0
    assert "rug" in why


def test_the_peak_survives_a_drawdown_so_a_trailing_stop_can_fire():
    pos = _pos(cost=10.0)
    P.mark_from_exit_quote(pos, 30.0)
    P.mark_from_exit_quote(pos, 20.0)
    assert pos.peak_usd == 30.0
    assert pos.mark_usd == 20.0
