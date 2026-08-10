"""Execution: turning a sized plan into a signed, submitted order.

Flow, from Definitive Flash's OpenAPI spec (`/v1/openapi.json`, v2.0.0):

    POST /quote  -> quoteId + evm.{wrap, approveTx, orderTypedData}
    sign evm.orderTypedData (EIP-712) with the funder wallet
    POST /order  -> quoteId + userSignature + evmOrderTypedData echo

**Keys never enter this process.** Nothing here signs, and nothing here accepts a
private key. `prepare()` returns the typed data to be signed elsewhere — the
Flash MCP (`@definitive-fi/flash-mcp`) keeps keys in the OS keychain, out of any
transcript. `submit()` takes a signature that was produced somewhere else. A
Flash API key alone cannot move funds; every order also needs a wallet signature
plus an on-chain approval.

Three things learned the hard way against the live API:

**A browser User-Agent is mandatory.** Without one, Cloudflare answers 403 with
`error code: 1010` — a browser-signature block that looks exactly like an auth
failure and will send you hunting for a bad API key.

**Not everything is routable.** Flash aggregates 200+ DEXes but only prices
assets it has notional rates for. A fresh Pons v2 bonding curve returns
`FailedPrecondition ... missing notional rates for assets`, because a curve is
not an AMM it indexes. Those need the direct curve path instead, so `route_for`
decides per venue rather than assuming Flash covers everything.

**Native ETH gets wrapped.** Spending the `0xEeee...EEeE` sentinel returns a
`wrap.evmTx` that must be sent first; the order itself then spends WETH.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import venues as V

BASE_URL = os.getenv("RH_FLASH_URL", "https://flash.definitive.fi/v1")
CHAIN = "robinhood"
SETTLEMENT = "0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78"
NATIVE_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Public dev key: QUOTING ONLY, cannot move funds. Real keys come from the env.
DEV_KEY = "dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b"

# Cloudflare blocks non-browser agents with 403 / code 1010.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

ROUTE_FLASH = "flash"
ROUTE_CURVE = "curve-direct"


class FlashError(RuntimeError):
    """A Flash API error, carrying the server's own code where there is one."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


def api_key() -> str:
    return os.getenv("RH_FLASH_KEY") or DEV_KEY


def _post(path: str, body: dict, *, key: str | None = None, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        BASE_URL + path, json.dumps(body).encode(),
        {"content-type": "application/json",
         "x-definitive-api-key": key or api_key(),
         "user-agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf8", "replace")
        try:
            err = (json.loads(raw) or {}).get("error") or {}
        except Exception:
            err = {}
        raise FlashError(err.get("message") or raw[:300],
                         code=err.get("code"), status=e.code) from None


def route_for(venue: V.Venue) -> str:
    """Which execution path this venue needs.

    Pons v2 pre-graduation lives on a bonding curve that Flash does not index, so
    it must be traded against the curve directly. Everything else — v3/v4 pools,
    real V2 pairs, flap's pair, stock tokens — routes through Flash, which is
    strictly better there because it shops 200+ venues.
    """
    if venue.kind == V.PONS_V2 and not venue.graduated:
        return ROUTE_CURVE
    return ROUTE_FLASH


def quote(*, target: str, contra: str, qty: str, side: str = "buy",
          quick_trade: bool = True, max_slippage: float = 0.05,
          max_price_impact: float = 0.05, funder: str | None = None,
          key: str | None = None) -> dict:
    """A Flash quote. `qty` is always the amount of the asset being SPENT.

    Raises FlashError on an unroutable asset rather than returning an empty
    quote, so a caller can fall back to the curve path instead of silently
    treating "cannot price" as "no liquidity".
    """
    body = {"targetChain": CHAIN, "contraChain": CHAIN,
            "targetAsset": target, "contraAsset": contra,
            "side": side, "qty": str(qty), "orderType": "market",
            "quickTrade": quick_trade,
            "maxSlippage": str(max_slippage),
            "maxPriceImpact": str(max_price_impact)}
    if funder:
        body["funderAddress"] = funder
    return _post("/quote", body, key=key)


def eth_usd_from_quote(q: dict) -> float | None:
    """Live ETH/USD implied by a quote's own notionals.

    Worth using instead of a hardcoded price: the repo's `RH_WETH_USD` default of
    3000 was measured against Flash at ~1917, a 56% overstatement that would
    inflate every market cap and therefore every position size computed from one.
    """
    leg = q.get("from") or {}
    try:
        amt, notional = float(leg.get("amount")), float(leg.get("notional"))
    except (TypeError, ValueError):
        return None
    return notional / amt if amt else None


@dataclass(frozen=True)
class PreparedOrder:
    """A quote plus everything the signer needs — and nothing it should not have."""
    quote_id: str
    target: str
    contra: str
    side: str
    qty: str
    spend_notional_usd: float | None
    receive_notional_usd: float | None
    price_impact: float | None
    order_typed_data: str            # EIP-712 JSON: sign THIS, elsewhere
    permit_typed_data: str | None
    approve_tx: dict | None          # send once per token before the first order
    wrap_tx: dict | None             # send when spending native ETH
    warnings: tuple[str, ...] = ()

    @property
    def slippage_vs_quote_pct(self) -> float | None:
        if self.spend_notional_usd and self.receive_notional_usd:
            return 100.0 * (1 - self.receive_notional_usd / self.spend_notional_usd)
        return None


def prepare(*, target: str, contra: str, qty: str, side: str = "buy",
            funder: str, max_slippage: float = 0.05,
            max_price_impact: float = 0.05, quick_trade: bool = True,
            max_loss_pct: float = 10.0, key: str | None = None) -> PreparedOrder:
    """Quote and validate, returning typed data for an EXTERNAL signer.

    `max_loss_pct` is a last-line sanity check on the quote itself: if Flash says
    the round leg loses more than this, something is wrong with the token (tax,
    a broken pool) and the order should not be signed. It is deliberately checked
    here rather than trusted from the earlier gate, because the quote is the
    first time we see what the venue will REALLY pay.
    """
    q = quote(target=target, contra=contra, qty=qty, side=side,
              quick_trade=quick_trade, max_slippage=max_slippage,
              max_price_impact=max_price_impact, funder=funder, key=key)
    evm = q.get("evm") or {}
    typed = evm.get("orderTypedData")
    if not typed:
        raise FlashError("quote returned no orderTypedData; nothing to sign")
    frm, to = q.get("from") or {}, q.get("to") or {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    spend, recv = _f(frm.get("notional")), _f(to.get("notional"))
    warnings: list[str] = []
    if spend and recv:
        loss = 100.0 * (1 - recv / spend)
        if loss > max_loss_pct:
            raise FlashError(
                "quote loses %.1f%% (> %.1f%% limit) — refusing to prepare an order"
                % (loss, max_loss_pct))
        if loss > max_loss_pct / 2:
            warnings.append("quote loses %.1f%% on this leg" % loss)
    impact = _f(q.get("estimatedPriceImpact"))
    if impact is not None and impact > max_price_impact:
        warnings.append("price impact %.2f%% exceeds cap" % (impact * 100))
    return PreparedOrder(
        quote_id=q.get("quoteId"), target=target, contra=contra, side=side,
        qty=str(qty), spend_notional_usd=spend, receive_notional_usd=recv,
        price_impact=impact, order_typed_data=typed,
        permit_typed_data=evm.get("permitTypedData") or None,
        approve_tx=evm.get("approveTx"), wrap_tx=(q.get("wrap") or {}).get("evmTx"),
        warnings=tuple(warnings))


def submit(prepared: PreparedOrder, *, funder: str, signature: str,
           permit_signature: str | None = None, confirm: bool = False,
           key: str | None = None) -> dict:
    """Send a SIGNED order. Refuses unless `confirm=True`.

    The explicit confirm exists because everything upstream of here is reversible
    and this call is not. A signature alone should never be sufficient to fire —
    it may have been produced for review.
    """
    if not confirm:
        raise FlashError("refusing to submit without confirm=True")
    if not signature or not signature.startswith("0x"):
        raise FlashError("a 0x-prefixed funder signature is required")
    body = {"targetChain": CHAIN, "contraChain": CHAIN,
            "targetAsset": prepared.target, "contraAsset": prepared.contra,
            "side": prepared.side, "qty": prepared.qty, "orderType": "market",
            "funderAddress": funder, "quoteId": prepared.quote_id,
            "userSignature": signature,
            "evmOrderTypedData": prepared.order_typed_data}
    if prepared.permit_typed_data:
        body["evmPermitTypedData"] = prepared.permit_typed_data
    if permit_signature:
        body["evmPermitSignature"] = permit_signature
    return _post("/order", body, key=key)


def get_order(order_id: str, *, key: str | None = None) -> dict:
    req = urllib.request.Request(
        "%s/orders/%s" % (BASE_URL, order_id),
        headers={"x-definitive-api-key": key or api_key(), "user-agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise FlashError(e.read().decode("utf8", "replace")[:300], status=e.code) from None


def cancel_order(order_id: str, *, key: str | None = None) -> dict:
    return _post("/orders/%s/cancel" % order_id, {}, key=key)


# --- live quote-asset pricing ------------------------------------------------
# The repo shipped `RH_WETH_USD` defaulting to 3000. Measured against Flash it is
# ~1917 — a 56% overstatement. Since market cap is priced in the quote asset and
# position size is a fraction of market cap, a stale ETH price inflates every
# ticket by the same 56%. Deriving it from a real quote removes the whole class
# of error, and a quote is a thing we already need anyway.

_eth_usd: tuple[float, float] | None = None      # (price, fetched_at)
ETH_PRICE_TTL = float(os.getenv("RH_ETH_PRICE_TTL", "60"))


def live_eth_usd(*, fallback: float | None = None, now: float | None = None) -> float | None:
    """ETH/USD from a live Flash quote, cached briefly.

    Returns `fallback` only if the API cannot be reached — never a stale
    hardcoded constant when a real number is available.
    """
    global _eth_usd
    import time as _time
    t = _time.time() if now is None else now
    if _eth_usd and (t - _eth_usd[1]) < ETH_PRICE_TTL:
        return _eth_usd[0]
    # Quote a liquid, always-priced asset. NOT WETH — spending native ETH for
    # WETH is just a wrap, which Flash declines to quote.
    for target in (PRICE_ANCHOR, USDG_ON_RH):
        try:
            q = quote(target=target, contra=NATIVE_SENTINEL, qty="0.01")
        except Exception:
            continue
        price = eth_usd_from_quote(q)
        if price:
            _eth_usd = (price, t)
            return price
    return _eth_usd[0] if _eth_usd else fallback


WETH_ON_RH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG_ON_RH = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
# AAPL: a canonical Robinhood Stock Token, continuously priced by Flash. Used
# only as a pricing anchor for the ETH leg — the target asset is irrelevant, the
# `from` leg's notional/amount is what carries ETH/USD.
PRICE_ANCHOR = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"


def sell_quote_usd(*, token: str, contra: str, qty_tokens: float,
                   key: str | None = None) -> tuple[float | None, str]:
    """(usd_out, status) for selling `qty_tokens` — the authoritative exit mark.

    This exists because a spot price is not proof of an exit. For venues with no
    local sell quote (Uniswap V3, v4), marking at spot always returns a number,
    so `fails` never increments and the rug check can never fire — a honeypot
    would mark healthy forever. Flash is the path those trades would really take,
    so its refusal to quote a sell IS the unsellable signal.

    status: "ok" | "unsellable" | "error". Only "unsellable" should count toward
    the rug counter; a network blip is not a rug, and treating it as one would
    close good positions on a bad connection.
    """
    try:
        q = quote(target=token, contra=contra, qty=str(qty_tokens), side="sell",
                  quick_trade=False, key=key)
    except FlashError as e:
        msg = str(e).lower()
        if "notional" in msg or "no route" in msg or "liquidity" in msg \
                or "failedprecondition" in msg or "invalid" in msg:
            return None, "unsellable"
        return None, "error"
    except Exception:
        return None, "error"
    to = q.get("to") or {}
    try:
        return float(to.get("notional")), "ok"
    except (TypeError, ValueError):
        return None, "unsellable"
