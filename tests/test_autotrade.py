from cambrian.runners.autotrade import (Decision, TradePolicy, Wallet,
                                        record_buy, record_close, should_buy)

TOK = "0xa3b6aee90017b72c0812dc1e013de70eb2917ba3"


def base(**over):
    d = dict(token=TOK, score=0.72, pad="bankr", liquidity_usd=30_000,
             sniper_share=0.1, fanout=5, wallet=Wallet(), policy=TradePolicy(),
             now=1_000.0)
    d.update(over)
    return d


def test_clean_hot_runner_is_bought():
    d = should_buy(**base())
    assert d.ok and d.spend == 0.02


def test_weak_score_rejected():
    assert not should_buy(**base(score=0.4)).ok


def test_wrong_pad_rejected_when_required():
    p = TradePolicy(require_pad="bankr")
    assert not should_buy(**base(pad="noxa", policy=p)).ok
    assert should_buy(**base(pad="bankr", policy=p)).ok


def test_thin_or_unknown_liquidity_fails_closed():
    assert not should_buy(**base(liquidity_usd=5_000)).ok
    assert not should_buy(**base(liquidity_usd=None)).ok


def test_sniped_and_farmed_rejected():
    assert not should_buy(**base(sniper_share=0.8)).ok
    assert not should_buy(**base(fanout=40)).ok


def test_dedupe_already_held_or_seen():
    w = Wallet()
    record_buy(w, TOK, 0.02, now=1_000.0)
    assert not should_buy(**base(wallet=w, now=2_000.0)).ok  # already a position
    w2 = Wallet(seen={TOK})
    assert not should_buy(**base(wallet=w2)).ok              # already seen


def test_cooldown_blocks_rapid_buys():
    w = Wallet(last_buy_ts=1_000.0)
    assert not should_buy(**base(wallet=w, now=1_030.0)).ok  # 30s < 60s cooldown
    assert should_buy(**base(wallet=w, now=1_100.0)).ok      # 100s ok


def test_max_positions_and_total_caps():
    w = Wallet(positions={f"0x{i:040x}": {"spend": 0.02} for i in range(5)}, spent=0.10)
    assert not should_buy(**base(wallet=w)).ok               # at 5 positions AND cap
    w2 = Wallet(spent=0.09)                                  # only 0.01 left
    d = should_buy(**base(wallet=w2))
    assert d.ok and abs(d.spend - 0.01) < 1e-9              # sized down to budget


def test_daily_loss_kill_switch():
    w = Wallet(realized_pnl=-0.05)                           # down 0.05 >= 0.04 limit
    assert not should_buy(**base(wallet=w)).ok


def test_record_close_frees_budget_and_books_pnl():
    w = Wallet()
    record_buy(w, TOK, 0.02, now=1.0)
    assert w.spent == 0.02
    record_close(w, TOK, pnl=-0.007)
    assert w.spent == 0.0 and abs(w.realized_pnl + 0.007) < 1e-9
    assert TOK not in w.positions and TOK in w.seen          # stays deduped
