"""The execution seam: turn a scored runner into a Definitive Flash order.

Flash (flash.definitive.fi) is a non-custodial best-execution API that supports
Robinhood Chain (chainId 4663) — so the watch side of this repo can hand a HOT
runner straight to an execution venue. QuickTrade market orders are built for
sniping fresh launches; stop-loss / take-profit cover the "in a dump go stable"
exit.

This module is PURE and read-only: it builds request bodies and human-readable
intents. It never signs and never holds a key. Actual submission (which needs a
per-order wallet signature) belongs in the Flash MCP server (@definitive-fi/
flash-mcp), which keeps keys in the OS keychain and out of any transcript — see
docs/flash-execution.md. Nothing here moves funds.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as rcfg

FLASH_BASE_URL = "https://flash.definitive.fi/v1"
# Public integrator key from Flash's docs — safe to use: it enables quoting and
# CANNOT move funds (every trade also needs the funder's per-order signature).
# Override with your own via RH_FLASH_KEY once you register as an integrator.
FLASH_DEV_KEY = "dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b"
FLASH_CHAIN = "robinhood"                 # Flash's name for RH Chain (4663)
FLASH_SETTLEMENT = "0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78"  # same on every EVM chain
NATIVE_ETH_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # spend-native marker


def quote_body(token: str, *, contra: str, qty: str, side: str = "buy",
               quick_trade: bool = True, max_slippage: str = "0.05",
               funder: str | None = None, chain: str = FLASH_CHAIN) -> dict:
    """Build a POST /quote (and /order) body for a runner trade.

    qty is the SPEND amount in contra units on a buy (e.g. WETH in), target units
    on a sell. QuickTrade is on by default — this is a fresh-launch snipe path.
    """
    body = {
        "targetChain": chain, "contraChain": chain,
        "targetAsset": token, "contraAsset": contra,
        "side": side, "qty": str(qty),
        "orderType": "market", "maxSlippage": str(max_slippage),
    }
    if quick_trade:
        body["quickTrade"] = True
    if funder:
        body["funderAddress"] = funder
    return body


def stop_loss_body(token: str, *, contra: str, qty: str, stop_usd: str,
                   chain: str = FLASH_CHAIN) -> dict:
    """A resting stop-loss: market-sell the runner if it draws down to stop_usd.

    The "flight to stables" exit as a native order — sell the position back to the
    contra asset the moment price falls to the trigger. qty is in target units.
    """
    return {
        "targetChain": chain, "contraChain": chain,
        "targetAsset": token, "contraAsset": contra,
        "side": "sell", "qty": str(qty), "orderType": "stop-loss",
        "triggers": [{"notionalPrice": str(stop_usd), "triggerType": "lower"}],
    }


def take_profit_body(token: str, *, contra: str, qty: str, target_usd: str,
                     chain: str = FLASH_CHAIN) -> dict:
    """A resting take-profit: market-sell qty target-tokens when price rises to
    target_usd (an `upper` trigger)."""
    return {
        "targetChain": chain, "contraChain": chain,
        "targetAsset": token, "contraAsset": contra,
        "side": "sell", "qty": str(qty), "orderType": "take-profit",
        "triggers": [{"notionalPrice": str(target_usd), "triggerType": "upper"}],
    }


def stop_from_entry(token: str, *, contra: str, qty: str, entry_usd: float,
                    drawdown: float = 0.30, chain: str = FLASH_CHAIN) -> dict:
    """A stop-loss placed `drawdown` below your fill price. You only know the fill
    price after the market buy lands, so this is the follow-up leg: buy, read the
    fill, then rest this to auto-sell back to the contra asset if it dumps."""
    stop = round(entry_usd * (1.0 - drawdown), 12)
    return stop_loss_body(token, contra=contra, qty=qty, stop_usd=str(stop), chain=chain)


@dataclass(frozen=True)
class ExitPlan:
    """A LADDERED, conviction-aware exit for a sniped runner. Cut losers fast, pull
    initials at the first rung (risk-free), then scale OUT on the way up — but when
    the runner is genuinely strong, hold back the upper trims and let a bigger moon
    bag run. Defaults are an aggressive-but-sane degen profile."""
    stop_loss_pct: float = 0.30
    # (multiple, fraction-of-ORIGINAL-position to sell at that rung). The first rung
    # is always resized to pull initials exactly (sell 1/mult); later rungs trim.
    tp_rungs: tuple = ((2.0, 0.50), (3.0, 0.25), (5.0, 0.15))
    moon_stop_pct: float = 0.0      # breakeven stop on the free moon bag (can't lose)
    moon_tp_mult: float = 25.0      # sell whatever's left of the bag here
    conviction_hold: float = 0.40   # at conviction=1, hold back this much of the
    #                                 scheduled UPPER trims — i.e. let it run


def conviction_from(score: float, gross_usd: float | None,
                    strong_gross: float = 25_000.0) -> float:
    """0..1 'let it run' signal: a HOT score plus real traded volume. Strong volume
    on a high score = conviction to hold a bigger bag rather than TP it all."""
    g = min((gross_usd or 0.0) / strong_gross, 1.0)
    return round(0.6 * min(max(score, 0.0), 1.0) + 0.4 * g, 3)


def exit_ladder(entry_usd: float, qty_tokens: float, plan: ExitPlan | None = None,
                conviction: float = 0.0) -> dict:
    """Build the exit ladder from a fill. Rung 0 pulls initials (sell 1/mult so the
    stake is recovered); upper rungs trim, scaled DOWN by conviction so a strong
    runner keeps a bigger moon bag. Prices are USD/token, qtys are target-tokens."""
    plan = plan or ExitPlan()
    conviction = min(max(conviction, 0.0), 1.0)
    rungs, sold = [], 0.0
    for i, (mult, frac) in enumerate(plan.tp_rungs):
        sell_frac = (1.0 / mult) if i == 0 else frac * (1.0 - conviction * plan.conviction_hold)
        qty_i = min(qty_tokens * sell_frac, qty_tokens - sold)
        sold += qty_i
        rungs.append({"mult": mult, "price_usd": round(entry_usd * mult, 12),
                      "qty": round(qty_i, 6)})
    return {
        "stop_loss_usd": round(entry_usd * (1 - plan.stop_loss_pct), 12),
        "stop_loss_qty": qty_tokens,               # full position until initials are out
        "rungs": rungs,                            # scale-out ladder on the way up
        "moon_bag_qty": round(max(qty_tokens - sold, 0.0), 6),
        "moon_stop_usd": round(entry_usd * (1 - plan.moon_stop_pct), 12),
        "moon_take_profit_usd": round(entry_usd * plan.moon_tp_mult, 12),
        "conviction": conviction,
    }


@dataclass(frozen=True)
class TradeIntent:
    """A proposed action for a HOT runner — inspectable, not executed."""
    action: str                 # "buy"
    venue: str                  # "definitive-flash"
    chain: str
    token: str
    contra: str
    qty: str
    quick_trade: bool
    pad: str | None
    reason: str                 # why (tier + score)
    quote_body: dict            # ready for POST /quote or the flash MCP


def intent_from_score(score, *, contra: str, qty: str,
                      funder: str | None = None) -> TradeIntent:
    """Turn a RunnerScore into a QuickTrade buy intent on Flash. Read-only —
    the caller decides whether to route it to the Flash MCP for signing."""
    c = score.candidate
    return TradeIntent(
        action="buy", venue="definitive-flash", chain=FLASH_CHAIN,
        token=c.token, contra=contra, qty=str(qty), quick_trade=True,
        pad=c.launchpad, reason=f"{score.tier} {score.score}",
        quote_body=quote_body(c.token, contra=contra, qty=qty, funder=funder),
    )


def mcp_call(intent: TradeIntent) -> dict:
    """The flash_submit_order argument object for the Flash MCP (which signs)."""
    return {"tool": "flash_submit_order", "arguments": intent.quote_body}
