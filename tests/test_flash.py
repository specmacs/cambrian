from cambrian.runners.flash import (FLASH_CHAIN, TradeIntent, intent_from_score,
                                    mcp_call, quote_body, stop_from_entry,
                                    stop_loss_body)
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
