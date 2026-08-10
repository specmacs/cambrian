"""Definitive Client API — trading from a Definitive vault, no wallet needed.

There are TWO Definitive APIs and they have very different operating models:

    Flash API      non-custodial. YOU hold the wallet and sign every order
                   (EIP-712). Built for third parties. `runners/execution.py`.
    Client API     trades from a **Definitive-managed vault**. The API key and
                   secret authorize the trade; there is no wallet to hold and
                   nothing to sign per order. This module.

The Client API is the simpler path when the funds live in a Definitive vault: no
key custody, no approve transaction, no EIP-712, no wrap step. One call quotes,
one call executes.

    base    https://ddp.definitive.fi
    keys    x-definitive-api-key  `dpka_...`   (the public one)
            api secret            `dpks_...`   (the private one — signs, never sent)
    chain   "robinhood" is supported

**Auth is HMAC-SHA256 over a prehash string**, and the format is unforgiving —
any mismatch is a 401 that tells you nothing:

    {method}:{path}?{queryString}:{timestamp}:{sortedHeaders}{body}

with `sortedHeaders` being only the `x-definitive-*` headers, sorted by name and
rendered as `name:"value"` (JSON-quoted, comma-joined), and `body` being the exact
JSON string that gets sent. Two traps worth naming, because both produce a valid
signature over the wrong bytes:

- JavaScript's `JSON.stringify` emits **compact** JSON. Python's default adds a
  space after `:` and `,`, so the signed bytes would differ from the sent bytes.
  Every body here is serialised once, compactly, and that same string is both
  signed and sent.
- The header values are JSON-**quoted** in the prehash (`key:"value"`), not bare.

Requests are valid for 2 minutes, so the timestamp must be live rather than
cached.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.getenv("RH_DEFINITIVE_URL", "https://ddp.definitive.fi")
CHAIN = "robinhood"

# Same Cloudflare block as the Flash host: a non-browser agent gets 403/1010.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class DefinitiveError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.status, self.code, self.raw = status, code, ""


def api_key() -> str | None:
    return os.getenv("DEFINITIVE_API_KEY") or os.getenv("API_KEY")


def api_secret() -> str | None:
    return os.getenv("DEFINITIVE_API_SECRET") or os.getenv("API_SECRET")


def prehash(*, method: str, path: str, timestamp: str, headers: dict,
            query: dict | None = None, body_str: str = "") -> str:
    """Exactly the string Definitive's AuthHelpers signs.

    Header values are JSON-quoted and only `x-definitive-*` headers participate.
    Kept as its own function so it can be tested against the documented format
    without a network call — a signature mismatch is a bare 401 otherwise.
    """
    filtered = sorted((k, v) for k, v in headers.items()
                      if k.lower().startswith("x-definitive-"))
    sorted_headers = ",".join("%s:%s" % (k, json.dumps(v)) for k, v in filtered)
    qs = urllib.parse.urlencode(query or {})
    return "%s:%s?%s:%s:%s%s" % (method, path, qs, timestamp, sorted_headers, body_str)


def sign(secret: str, message: str) -> str:
    """HMAC-SHA256 hex. The `dpks_` prefix is stripped before use as the key."""
    return hmac.new(secret.replace("dpks_", "", 1).encode(),
                    message.encode(), hashlib.sha256).hexdigest()


def request(path: str, *, method: str = "GET", body: dict | None = None,
            query: dict | None = None, key: str | None = None,
            secret: str | None = None, timeout: float = 30.0,
            now_ms: int | None = None, debug: bool = False) -> dict:
    """Signed call against the Client API.

    `debug` prints the exact string being signed with the key redacted. A wrong
    signature and a wrong key return the same bare 401, so this is the only way
    to tell them apart — and it can be read out over a screenshot without
    exposing anything, which the alternative (handing over real credentials)
    cannot.
    """
    k, s = key or api_key(), secret or api_secret()
    if not k or not s:
        raise DefinitiveError(
            "set DEFINITIVE_API_KEY and DEFINITIVE_API_SECRET (the dpka_/dpks_ pair)")
    import time as _t
    ts = str(now_ms if now_ms is not None else int(_t.time() * 1000))
    # Serialise ONCE: the bytes that are signed must be the bytes that are sent.
    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    headers = {"x-definitive-api-key": k, "x-definitive-timestamp": ts}
    pre = prehash(method=method, path=path, timestamp=ts,
                  headers=headers, query=query, body_str=body_str)
    signature = sign(s, pre)
    if debug:
        print("  [debug] signing: %s" % pre.replace(k, "<KEY:%d chars>" % len(k)))
        print("  [debug] signature %s...  secret %d chars, prefix %s"
              % (signature[:12], len(s), s[:5]))
    qs = urllib.parse.urlencode(query or {})
    url = "%s%s%s" % (BASE_URL, path, ("?" + qs) if qs else "")
    req = urllib.request.Request(
        url, body_str.encode() if body is not None else None,
        {**headers, "x-definitive-signature": signature,
         "content-type": "application/json", "user-agent": _UA},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        msg = payload.get("message") or payload.get("error") or raw[:300]
        err = DefinitiveError(msg, status=e.code, code=str(payload.get("code") or ""))
        # Keep the untouched body: "Internal server error" as a 400 message tells
        # you nothing, and whatever detail exists is in the parts we dropped.
        err.raw = raw
        raise err from None


# --- portfolio ---------------------------------------------------------------

def deposit_address(chain: str = CHAIN, *, wallet_address: str,
                    **kw) -> tuple[str | None, str | None]:
    """The vault address to fund. Vaults are created on demand per chain.

    `wallet_address` is **required** and is YOUR wallet — the one that will send
    the deposit — not the vault's. Omitting it returns a 400 ZodError naming
    `walletAddress`, which is how this was found: the signature was already
    correct and the request reached body validation.

    Returns (address, vaultId).
    """
    out = request("/v2/portfolio/address/%s" % chain,
                  query={"walletAddress": wallet_address}, **kw)
    data = out if "address" in out else (out.get("data") or {})
    return data.get("address"), data.get("vaultId")


def positions(*, limit: int | None = None, cursor: str | None = None,
              include_dust: bool = False, **kw) -> dict:
    query: dict = {}
    if limit is not None:
        query["limit"] = str(limit)
    if cursor:
        query["cursor"] = cursor
    if include_dust:
        query["includeDustBalances"] = "true"
    return request("/v2/portfolio/positions", query=query or None, **kw)


def portfolio(**kw) -> dict:
    return request("/v2/portfolio", **kw)


# --- trading -----------------------------------------------------------------

def quicktrade_quote(*, target: str, contra: str, qty: str,
                     side: str = "buy", chain: str = CHAIN, **kw) -> dict:
    """Preview a trade. `qty` is the amount of the asset being SPENT."""
    return request("/v2/portfolio/quicktrade/quote", method="POST", body={
        "chain": chain, "targetAsset": target, "contraAsset": contra,
        "qty": str(qty), "orderSide": side}, **kw)


def quicktrade_submit(*, target: str, contra: str, qty: str, side: str = "buy",
                      chain: str = CHAIN, slippage_tolerance: str | None = None,
                      display_asset_price: str | None = None,
                      seconds_to_expire: int | None = None,
                      confirm: bool = False, **kw) -> dict:
    """EXECUTE a trade from the vault. Requires `confirm=True`.

    The path is `/v2/portfolio/quicktrade` — there is no `/submit` sibling, and
    no quote ID is threaded through: QuickTrade re-quotes and executes
    atomically, which is why it is the right primitive here. A quote taken first
    is a preview for the operator, not an input to this call.

    `slippage_tolerance` ("0.01" = 1%) is price protection and is passed on every
    call the desk makes — the default is 1%, which is tighter than a memecoin
    launch tolerates, so leaving it implicit would silently fail trades.

    This is the call that spends real money, and unlike the Flash path there is
    no wallet signature standing between the key and the fill — the key IS the
    authorization. So the explicit confirm matters more here, not less.
    """
    if not confirm:
        raise DefinitiveError("refusing to submit without confirm=True")
    body: dict = {"chain": chain, "targetAsset": target, "contraAsset": contra,
                  "qty": str(qty), "orderSide": side}
    if slippage_tolerance is not None:
        body["slippageTolerance"] = str(slippage_tolerance)
    if display_asset_price is not None:
        body["displayAssetPrice"] = str(display_asset_price)
    if seconds_to_expire is not None:
        body["secondsToExpire"] = int(seconds_to_expire)
    return request("/v2/portfolio/quicktrade", method="POST", body=body, **kw)


def order(order_id: str, **kw) -> dict:
    """State of a submitted order. QuickTrade returns only an orderId."""
    return request("/v2/portfolio/orders/%s" % order_id, **kw)


def quote_cost(q: dict) -> dict:
    """Flatten a quote into the numbers a decision needs."""
    meta = q.get("metadata") or {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    spend, recv = _f(meta.get("fromNotional")), _f(meta.get("toNotional"))
    return {
        "spend_usd": spend, "receive_usd": recv,
        "loss_pct": (100.0 * (1 - recv / spend)) if (spend and recv) else None,
        "price_impact": _f(meta.get("estimatedPriceImpact")),
        "fee_usd": _f(meta.get("estimatedFeeNotional")),
        "min_out": meta.get("minAmountOut"),
        "min_out_usd": _f(meta.get("minAmountOutNotional")),
        "buy_amount": meta.get("buyAmount"),
        "sell_amount": meta.get("sellAmount"),
        "price": _f(meta.get("price")),
        "marketable": meta.get("isMarketable"),
        "warnings": tuple(meta.get("warnings") or ()),
        "quote_id": ((q.get("quote") or {}).get("quote") or {}).get("id"),
    }
