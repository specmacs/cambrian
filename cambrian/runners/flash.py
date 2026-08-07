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
