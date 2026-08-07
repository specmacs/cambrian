from cambrian.runners.flash import (ExitPlan, FLASH_CHAIN, TradeIntent,
                                    conviction_from, exit_ladder,
                                    intent_from_score, mcp_call, quote_body,
                                    stop_from_entry, stop_loss_body,
                                    take_profit_body)
from cambrian.runners.score import RunnerScore
from cambrian.runners.snapshots import RunnerCandidate

TOKEN = "0xa3b6aee90017b72c0812dc1e013de70eb2917ba3"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"


def test_quote_body_is_a_robinhood_quicktrade_buy():
    b = quote_body(TOKEN, contra=WETH, qty="0.02")
    assert b["targetChain"] == "robinhood" and b["contraChain"] == "robinhood"
    assert b["targetAsset"] == TOKEN and b["contraAsset"] == WETH
    assert b["side"] == "buy" and b["orderType"] == "market"
    assert b["quickTrade"] is True and b["qty"] == "0.02"
    assert "funderAddress" not in b               # omitted unless provided


def test_quote_body_funder_and_no_quick():
    b = quote_body(TOKEN, contra=WETH, qty="1", quick_trade=False, funder="0xabc")
    assert "quickTrade" not in b and b["funderAddress"] == "0xabc"


def test_stop_loss_is_a_lower_trigger_sell():
    s = stop_loss_body(TOKEN, contra=WETH, qty="1000", stop_usd="0.0004")
    assert s["side"] == "sell" and s["orderType"] == "stop-loss"
    assert s["triggers"] == [{"notionalPrice": "0.0004", "triggerType": "lower"}]


def test_stop_from_entry_is_below_fill():
    s = stop_from_entry(TOKEN, contra=WETH, qty="1000", entry_usd=0.001, drawdown=0.35)
    assert s["orderType"] == "stop-loss" and s["side"] == "sell"
    trig = s["triggers"][0]
    assert trig["triggerType"] == "lower"
    assert abs(float(trig["notionalPrice"]) - 0.00065) < 1e-9   # 0.001 * (1-0.35)


def test_take_profit_is_an_upper_trigger_sell():
    t = take_profit_body(TOKEN, contra=WETH, qty="500", target_usd="0.002")
    assert t["side"] == "sell" and t["orderType"] == "take-profit"
    assert t["triggers"] == [{"notionalPrice": "0.002", "triggerType": "upper"}]


def test_exit_ladder_rung0_pulls_initials_and_scales_out():
    # bought 1000 tokens at $0.001 ($1 stake). Default rungs 2x/3x/5x, no conviction.
    L = exit_ladder(entry_usd=0.001, qty_tokens=1000.0)
    assert L["stop_loss_usd"] == 0.0007 and L["stop_loss_qty"] == 1000.0
    r = L["rungs"]
    assert r[0]["mult"] == 2.0 and r[0]["price_usd"] == 0.002 and r[0]["qty"] == 500.0
    assert r[1]["mult"] == 3.0 and r[1]["qty"] == 250.0    # 0.25 of original
    assert r[2]["mult"] == 5.0 and r[2]["qty"] == 150.0    # 0.15 of original
    assert L["moon_bag_qty"] == 100.0                      # the rest rides
    assert L["moon_stop_usd"] == 0.001 and L["moon_take_profit_usd"] == 0.025


def test_conviction_holds_a_bigger_moon_bag():
    low = exit_ladder(0.001, 1000.0, conviction=0.0)
    high = exit_ladder(0.001, 1000.0, conviction=1.0)
    # rung 0 (initials) is identical; upper trims shrink, so the bag grows.
    assert high["rungs"][0]["qty"] == low["rungs"][0]["qty"] == 500.0
    assert high["rungs"][1]["qty"] < low["rungs"][1]["qty"]
    assert high["moon_bag_qty"] > low["moon_bag_qty"]      # let it run


def test_conviction_from_rewards_score_and_volume():
    assert conviction_from(0.9, 30_000) > conviction_from(0.6, 1_000)
    assert 0.0 <= conviction_from(0.4, None) <= 1.0


def _score(tier="hot", pad="bankr"):
    c = RunnerCandidate(token=TOKEN, pool="0xpool", launchpad=pad)
    return RunnerScore(c, 0.72, tier, (), ("net +$4,000 in",))


def test_intent_from_score_carries_pad_and_body():
    it = intent_from_score(_score(), contra=WETH, qty="0.02")
    assert isinstance(it, TradeIntent)
    assert it.action == "buy" and it.chain == FLASH_CHAIN and it.quick_trade
    assert it.pad == "bankr" and it.token == TOKEN
    assert it.quote_body["quickTrade"] is True
    assert mcp_call(it) == {"tool": "flash_submit_order", "arguments": it.quote_body}
