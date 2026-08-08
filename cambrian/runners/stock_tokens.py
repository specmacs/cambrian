"""Robinhood Stock Tokens: the canonical registry, and USD pricing.

A large share of Pons v2 launches are paired against a tokenized stock rather
than ETH — measured live, roughly 60% (GME, PLTR, SPY, COIN, AAPL, SPCX, TSLA,
NVDA, MU, CRCL ...). Those launches are worth trading, so the desk has to handle
them properly, and "properly" means two things it did not do before:

**Price the quote asset correctly.** A token quoted in SPY is denominated in a
~$773 asset; one quoted in GME in a ~$19 asset. Valuing either at the ETH price
misstates market cap by orders of magnitude and therefore misstates position
size, since sizing is a fraction of market cap.

**Use the canonical contract.** Robinhood's docs are explicit: "a token with a
matching name/ticker but a different contract address is not a Robinhood Stock
Token." Ticker matching is forgeable — anyone can deploy an ERC-20 called AAPL
and pair a launch against it, and the launch would look stock-backed while the
quote asset is worthless. Only membership in this registry counts.

Sources, both public and unauthenticated:
    GET https://api.robinhood.com/rhj/assets            (60 req/s)
    GET https://api.robinhood.com/rhj/prices/{symbol}   (60 req/s, 15s cache)

The token's USD value is the underlying equity price times `currentMultiplier`.
The multiplier carries corporate actions (a 4.0 was live on CRWD) without
rebasing balances: after a 4:1 split one token still represents the original
economic claim, so its value is four post-split shares. Skipping the multiplier
would price such a token at a quarter of its worth.

Everything is cached and every failure degrades to None rather than to a guess —
an unknown quote price must block a trade, not size one wrongly.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

ASSETS_URL = "https://api.robinhood.com/rhj/assets"
PRICES_URL = "https://api.robinhood.com/rhj/prices/%s"
CHAIN_ID = 4663

REGISTRY_TTL = float(os.getenv("RH_STOCK_REGISTRY_TTL", "3600"))
PRICE_TTL = float(os.getenv("RH_STOCK_PRICE_TTL", "15"))   # matches RH's own cache

_registry: dict[str, dict] = {}
_registry_at: float = 0.0
_prices: dict[str, tuple[float, float]] = {}               # symbol -> (usd, fetched_at)


def _get(url: str, timeout: float = 10.0):
    # urllib goes direct; requests is not guaranteed present in every runner.
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf8"))


def load_registry(*, force: bool = False, now: float | None = None) -> dict[str, dict]:
    """{lowercased address -> {symbol, name, multiplier, status}} for chain 4663.

    Cached for an hour: the roster changes on the order of listings, not blocks,
    and the hot path must never wait on an HTTP round trip.
    """
    global _registry, _registry_at
    t = time.time() if now is None else now
    if _registry and not force and (t - _registry_at) < REGISTRY_TTL:
        return _registry
    try:
        payload = _get(ASSETS_URL)
    except Exception:
        return _registry            # keep the last good roster rather than emptying it
    out: dict[str, dict] = {}
    for asset in payload.get("assets", []):
        for dep in asset.get("deployments", []):
            if dep.get("chainId") != CHAIN_ID or not dep.get("contractAddress"):
                continue
            try:
                mult = float(asset.get("currentMultiplier") or 1.0)
            except (TypeError, ValueError):
                mult = 1.0
            out[dep["contractAddress"].lower()] = {
                "symbol": asset.get("tokenSymbol"),
                "name": asset.get("tokenName"),
                "multiplier": mult,
                "status": asset.get("status"),
            }
    if out:
        _registry, _registry_at = out, t
    return _registry


def is_stock_token(address: str | None) -> bool:
    """Canonical-registry membership. Never name matching — names are forgeable."""
    if not address:
        return False
    return address.lower() in load_registry()


def info(address: str | None) -> dict | None:
    if not address:
        return None
    return load_registry().get(address.lower())


def symbol_for(address: str | None) -> str | None:
    rec = info(address)
    return rec.get("symbol") if rec else None


def usd_price(address: str | None, *, now: float | None = None) -> float | None:
    """USD value of ONE stock token, corporate-action multiplier applied.

    Mid of bid/ask: these are equity quotes and the spread can be wide on thin
    names (GME showed 19.08/19.99 live), so taking either side alone would bias
    every market cap computed against it.
    """
    rec = info(address)
    if not rec or not rec.get("symbol"):
        return None
    sym = rec["symbol"]
    t = time.time() if now is None else now
    hit = _prices.get(sym)
    if hit and (t - hit[1]) < PRICE_TTL:
        return hit[0] * rec["multiplier"]
    try:
        payload = _get(PRICES_URL % sym)
    except Exception:
        return hit[0] * rec["multiplier"] if hit else None
    quotes = payload.get("quotes") or []
    if not quotes:
        return None
    q = quotes[0]
    try:
        bid, ask = float(q.get("bid")), float(q.get("ask"))
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    _prices[sym] = (mid, t)
    return mid * rec["multiplier"]


def quote_price_usd(address: str | None, *, weth_usd: float,
                    usdg: str | None = None, weth: str | None = None) -> float | None:
    """USD price of ANY quote asset a launch might be denominated in.

    Returns None for an asset we cannot value — that is deliberate. Sizing is a
    fraction of market cap, so an unknown quote price must block the trade rather
    than silently fall back to the ETH price and size a SPY-quoted launch as if
    it were ETH-quoted.
    """
    if address is None:
        return weth_usd
    a = address.lower()
    if a == "0x" + "0" * 40:                       # native ETH
        return weth_usd
    if weth and a == weth.lower():
        return weth_usd
    if usdg and a == usdg.lower():
        return 1.0
    if is_stock_token(a):
        return usd_price(a)
    return None
