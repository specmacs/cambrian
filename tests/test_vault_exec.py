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


# --- exit_decision is stateful, and that has consequences ---------------------

def test_a_reported_rung_is_a_consumed_rung():
    # Pinning the property that made the next two tests necessary: asking the
    # question changes the answer, so the question must only be asked when the
    # answer can be acted on.
    pos = _pos(cost=10.0)
    P.mark_from_exit_quote(pos, 30.0)
    assert P.exit_decision(pos)[0] == "trim"
    assert pos.rungs_hit                      # recorded as taken
    P.mark_from_exit_quote(pos, 30.0)
    assert P.exit_decision(pos)[2] != "rung 2x"


def test_halting_must_not_burn_the_take_profits(monkeypatch):
    # Observed live in the terminal: while halted the engine still called
    # exit_decision to render a signal, so 2x and 3x were marked hit with
    # nothing sold, and on resume they could never fire again. Halting has to
    # skip the decision entirely, not skip only the submission.
    import time as _time

    from cambrian import terminal as T

    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: {"metadata": {"toNotional": "30.00"}})
    monkeypatch.setattr(D, "positions", lambda **kw: {"positions": []})
    monkeypatch.setattr(D, "quicktrade_submit",
                        lambda **kw: pytest.fail("halted must not submit"))

    tok = "0xhalt"
    pos = _pos(cost=10.0)
    T.STATE["vault"] = "0xv"
    T.STATE["halted"] = True
    T.STATE["book"] = {tok: {"position": pos, "decimals": 18, "held": 1.0,
                             "basis_known": True, "symbol": "H"}}
    thread = __import__("threading").Thread(
        target=T._engine, args=(FakeChain({tok: 10 ** 18}),),
        kwargs={"slippage": 0.05, "interval": 0.05}, daemon=True)
    thread.start()
    _time.sleep(0.6)
    assert pos.rungs_hit == []                 # nothing consumed while halted
    assert T.STATE["book"][tok]["signal"] == "halted"
    T.STATE["halted"] = False                  # leave global state clean


def test_a_cooldown_must_not_burn_a_rung_either(monkeypatch):
    # Same class of bug in `watch`: the guard was checked after the decision,
    # so a rung that arrived during a cooldown was consumed and never sold.
    from cambrian import cli

    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: {"metadata": {"toNotional": "30.00"}})
    monkeypatch.setattr(D, "quicktrade_submit",
                        lambda **kw: pytest.fail("cooling down must not submit"))
    tok = "0xcool"
    pos = _pos(cost=10.0)
    book = {tok: {"position": pos, "decimals": 18, "held": 1.0,
                  "basis_known": True, "symbol": "C"}}
    guard = VX.ExitGuard(cooldown_s=999)
    guard.note(tok)                            # already cooling
    cli._vault_tick(FakeChain({tok: 10 ** 18}), "0xv", book, guard,
                    slippage=0.05, VX=VX, quiet=True)
    assert pos.rungs_hit == []
    assert pos.mark_usd == 30.0                # still marked, just not decided


# --- reading a response whose shape was never documented ---------------------

def test_a_token_address_is_found_wherever_it_is_nested():
    # The first cut guessed at `address` / `assetAddress` / `asset.address`. When
    # the real response spells it differently the book comes back empty, the desk
    # shows nothing, and it looks like you hold nothing — the worst way for a
    # screen you trust with your money to be wrong.
    row = {"qty": "5", "asset": {"contractAddress":
                                 "0xd0f8a87e4402013f95bdc1a12433bfd487ccf31c"}}
    assert VX.find_address(row) == "0xd0f8a87e4402013f95bdc1a12433bfd487ccf31c"


def test_an_asset_keyed_address_wins_over_an_unrelated_one():
    row = {"vaultAddress": "0x" + "1" * 40,
           "asset": {"address": "0x" + "2" * 40}}
    assert VX.find_address(row) == "0x" + "2" * 40


def test_no_address_is_empty_rather_than_a_wrong_guess():
    assert VX.find_address({"qty": "5", "notional": "8"}) == ""


def test_a_symbol_is_found_but_prose_is_not():
    assert VX.find_symbol({"asset": {"symbol": "MOON"}}) == "MOON"
    assert VX.find_symbol({"description": "a very long marketing sentence"}) == ""


def test_the_terminal_lock_is_reentrant():
    # The engine holds it while adopting and calls _event inside that block. With
    # a plain Lock the first successful adoption deadlocked every request, and
    # the bug stayed invisible while adoption kept returning nothing.
    import threading

    from cambrian import terminal as T
    assert isinstance(T._lock, type(threading.RLock()))
    with T._lock:
        with T._lock:
            pass


def test_a_counterparty_address_is_never_mistaken_for_the_asset():
    # Taking the vault's own address would make every balance lookup ask what
    # the vault holds of itself — zero, silently, for every position.
    for key in ("vaultAddress", "walletAddress", "ownerAddress", "portfolioId"):
        assert VX.find_address({key: "0x" + "1" * 40}) == ""


def test_a_position_sold_outside_the_desk_is_closed_not_called_a_rug(monkeypatch):
    # Happened for real: the token was sold by hand through Definitive's UI.
    # A zero balance means GONE, not unsellable — treating it as unsellable
    # counts failures until the rug rule fires an exit against nothing.
    from cambrian import cli

    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: pytest.fail("nothing to quote"))
    monkeypatch.setattr(D, "quicktrade_submit",
                        lambda **kw: pytest.fail("nothing to sell"))
    tok = "0xgone"
    pos = _pos(cost=8.0)
    book = {tok: {"position": pos, "decimals": 18, "held": 1.0,
                  "basis_known": True, "symbol": "G"}}
    cli._vault_tick(FakeChain({}), "0xv", book, VX.ExitGuard(),
                    slippage=0.05, VX=VX, quiet=True)
    assert book[tok]["position"].closed is True
    assert pos.fails == 0                      # not a rug, just gone


# --- entry side: the caps are the only thing between a bad scan and $0 -------

def _fresh_terminal():
    from cambrian import terminal as T
    T.STATE.update(book={}, halted=False, auto_buy=True, buys_this_hour=[],
                   max_positions=3, buys_per_hour=6, max_usd=8.0,
                   bought_ever=set(), events=[], fills=[])
    return T


def test_entries_stop_at_the_position_cap():
    T = _fresh_terminal()
    T.STATE["max_positions"] = 2
    for i in range(2):
        T.STATE["book"]["0x%d" % i] = {"position": _pos(), "decimals": 18}
    ok, why = T._room_to_buy()
    assert ok is False and "open positions" in why


def test_entries_stop_at_the_hourly_cap():
    import time as _t
    T = _fresh_terminal()
    T.STATE["buys_per_hour"] = 3
    T.STATE["buys_this_hour"] = [_t.time()] * 3
    ok, why = T._room_to_buy()
    assert ok is False and "hourly" in why


def test_an_hour_old_buy_no_longer_counts_against_the_cap():
    import time as _t
    T = _fresh_terminal()
    T.STATE["buys_per_hour"] = 1
    T.STATE["buys_this_hour"] = [_t.time() - 3700]
    assert T._room_to_buy()[0] is True


def test_halting_stops_entries_as_well_as_exits():
    T = _fresh_terminal()
    T.STATE["halted"] = True
    assert T._room_to_buy()[0] is False


def test_a_buy_is_capped_by_the_settlement_balance(monkeypatch):
    # Sizing off a bankroll setting that exceeds the balance is a rejected order,
    # and it gets rejected exactly when a launch was worth catching.
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"metadata": {"fromNotional": "5"}}

    monkeypatch.setattr(D, "quicktrade_quote", fake)
    usdg = VX.rcfg.CONTRACTS["usdg"].lower()
    plan = VX.plan_buy(FakeChain({usdg: 5_000_000}, decimals=6),
                       token="0xt", vault="0xv", usd=8.0)
    assert plan["ok"] and seen["qty"] == "5"       # not 8 — that is all there is


def test_a_pending_fill_is_not_mistaken_for_a_sold_position(monkeypatch):
    # A just-submitted buy has a zero on-chain balance until it settles, and a
    # zero balance otherwise means "sold elsewhere". Closing it there would
    # abandon the position the instant it was opened.
    import time as _t

    T = _fresh_terminal()
    monkeypatch.setattr(D, "quicktrade_quote",
                        lambda **kw: pytest.fail("nothing to quote yet"))
    monkeypatch.setattr(D, "positions", lambda **kw: {"positions": []})
    tok = "0xpending"
    pos = _pos(cost=8.0)
    T.STATE["vault"] = "0xv"
    T.STATE["book"] = {tok: {"position": pos, "decimals": 18, "held": 0.0,
                             "basis_known": True, "symbol": "P",
                             "seen_balance": False}}
    thread = __import__("threading").Thread(
        target=T._engine, args=(FakeChain({}),),
        kwargs={"slippage": 0.05, "interval": 0.05}, daemon=True)
    thread.start()
    _t.sleep(0.4)
    assert T.STATE["book"][tok]["position"].closed is False
