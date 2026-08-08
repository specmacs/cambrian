"""Pons v2 bonding curves — pricing tokens that have no AMM pool yet.

Pons v2 does not open a pool at launch. Each token gets its own constant-product
bonding CURVE contract (address in topic2 of `TokenLaunched`), trades there, and
only graduates into a locked Uniswap v4 pool once the curve sells its whole
tradable allocation. So for a v2 token's entire pre-graduation life — which is
where a sniping desk operates — there is no pool to quote against, and every
price, depth and flow number has to come off the curve.

Formulas are from docs.ponsfamily.com/v2, and every getter below was verified
against live curves. Three things that will bite otherwise:

**Fees land on opposite sides.** A BUY takes fees off the INPUT before pricing; a
SELL prices first and takes fees off the OUTPUT. Applying them the same way both
directions silently overstates sell proceeds.

**`getReserves()` is mostly phantom.** It returns the PRICING reserves, which
include a virtual quote reserve seeded at launch — a fresh curve reads ~4.36 ETH
of quote against `realQuoteReserve() == 0`. Price with `getReserves()`, but never
report it as liquidity: real money in the curve is `realQuoteReserve()`. Treating
phantom reserve as depth would make every brand-new launch look deep.

**The quote asset is not always 18 decimals.** v2 pairs against native ETH (the
zero address), WETH, or any approved ERC-20. A live sample had `graduationThreshold`
of 8.09e9 — a 6-decimal asset. Hardcoding 1e18 misprices those by 10^12.

Note `getReserves()` shares Uniswap V2's selector but returns
`(uint256 quoteReserve, uint256 tokenReserve)` — two words, different meaning.
Do not route it through `uniswap_v2.read_reserves`.
"""

from __future__ import annotations

from .uniswap_v3 import _addr

# Selectors — keccak256(signature)[:4], pinned by tests.
SEL_GET_RESERVES = "0x0902f1ac"          # -> (quoteReserve, tokenReserve), phantom included
SEL_REAL_QUOTE_RESERVE = "0x4f1f58fd"    # -> real quote collected, net of fees
SEL_TOKEN_RESERVE = "0xcbcb3171"
SEL_SELLABLE_TOKENS = "0x808bcddc"       # tokens still buyable before closure
SEL_FEE_BPS = "0x24a9d853"
SEL_CREATOR_TAX_BPS = "0xc1bb8901"
SEL_READY_TO_GRADUATE = "0xc68360a5"
SEL_GRADUATED = "0xe7c2b772"
SEL_GRADUATION_THRESHOLD = "0x8b0bc501"

NATIVE_ETH = "0x0000000000000000000000000000000000000000"
BPS = 10_000


def decode_token_launched(log: dict) -> dict:
    """Decode a v2 factory `TokenLaunched`.

    TokenLaunched(address indexed token, address indexed curve,
                  address indexed deployer, address pairToken,
                  uint256 launchConfigId, uint256 graduationThreshold)

    topic2 is the CURVE. Pons **v1**'s topic2 is the deployer — the layouts differ
    between the two protocols, so never decode one with the other's reader.
    """
    t = log["topics"]
    data = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    w = [data[i * 64:(i + 1) * 64] for i in range(3)]
    return {
        "token": _addr(t[1]),
        "curve": _addr(t[2]),
        "deployer": _addr(t[3]),
        "pair_token": _addr(w[0]),
        "launch_config_id": int(w[1], 16),
        "graduation_threshold": int(w[2], 16),
    }


def _decode_curve_trade(log: dict) -> dict[str, int]:
    """CurveBuy/CurveSell share a shape: two indexed addresses + four uint256."""
    data = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    w = [int(data[i * 64:(i + 1) * 64], 16) for i in range(4)]
    return {"a": w[0], "b": w[1], "fee": w[2], "tax": w[3]}


def decode_curve_buy(log: dict) -> dict[str, int]:
    """CurveBuy(buyer, recipient, quoteIn, tokensOut, fee, tax)."""
    d = _decode_curve_trade(log)
    return {"quote_in": d["a"], "tokens_out": d["b"], "fee": d["fee"], "tax": d["tax"]}


def decode_curve_sell(log: dict) -> dict[str, int]:
    """CurveSell(seller, recipient, tokensIn, quoteOut, fee, tax)."""
    d = _decode_curve_trade(log)
    return {"tokens_in": d["a"], "quote_out": d["b"], "fee": d["fee"], "tax": d["tax"]}


def _u(client, curve: str, selector: str) -> int | None:
    try:
        raw = client.eth_call(curve, selector)
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def read_curve_state(client, curve: str) -> dict:
    """Everything needed to quote, gate and score one curve.

    Any field that cannot be read stays None so the scorer keeps failing closed —
    a curve we cannot price must not look like a cheap one.
    """
    reserves = (None, None)
    try:
        raw = client.eth_call(curve, SEL_GET_RESERVES)
        if raw and raw != "0x":
            body = raw[2:] if raw.startswith("0x") else raw
            if len(body) >= 128:
                reserves = (int(body[0:64], 16), int(body[64:128], 16))
    except Exception:
        pass
    return {
        "quote_reserve": reserves[0],        # phantom-inclusive: for PRICING only
        "token_reserve": reserves[1],
        "real_quote_reserve": _u(client, curve, SEL_REAL_QUOTE_RESERVE),
        "sellable_tokens": _u(client, curve, SEL_SELLABLE_TOKENS),
        "fee_bps": _u(client, curve, SEL_FEE_BPS),
        "creator_tax_bps": _u(client, curve, SEL_CREATOR_TAX_BPS),
        "ready_to_graduate": bool(_u(client, curve, SEL_READY_TO_GRADUATE)),
        "graduated": bool(_u(client, curve, SEL_GRADUATED)),
        "graduation_threshold": _u(client, curve, SEL_GRADUATION_THRESHOLD),
    }


def quote_buy(quote_in: int, state: dict, *, snipe_bps: int = 0) -> int | None:
    """Tokens out for an exact quote-asset input.

    Fees come off the INPUT before the curve prices the trade, and the result is
    clamped to `sellableTokens()` — past that the curve closes and graduates, so a
    quote that ignores the clamp promises tokens that cannot be bought.
    """
    qr, tr = state.get("quote_reserve"), state.get("token_reserve")
    if not qr or not tr or quote_in <= 0:
        return None
    fee_bps = state.get("fee_bps") or 0
    tax_bps = state.get("creator_tax_bps") or 0
    deduct = quote_in * (fee_bps + tax_bps + max(snipe_bps, 0)) // BPS
    net = quote_in - deduct
    if net <= 0:
        return 0
    out = (net * tr) // (qr + net)
    sellable = state.get("sellable_tokens")
    if sellable is not None:
        out = min(out, sellable)
    return out


def quote_sell(tokens_in: int, state: dict) -> int | None:
    """Quote-asset out for an exact token input.

    A sell is PRICED FIRST and fees come off the OUTPUT — the mirror of `quote_buy`.
    No snipe tax applies to sells.
    """
    qr, tr = state.get("quote_reserve"), state.get("token_reserve")
    if not qr or not tr or tokens_in <= 0:
        return None
    gross = (tokens_in * qr) // (tr + tokens_in)
    fee_bps = state.get("fee_bps") or 0
    tax_bps = state.get("creator_tax_bps") or 0
    return gross - (gross * (fee_bps + tax_bps) // BPS)


def real_liquidity_quote(state: dict) -> int | None:
    """Actual quote asset sitting in the curve — NOT `getReserves()`.

    `getReserves()` includes a phantom reserve seeded at launch (~4.36 ETH on a
    live sample whose real reserve was 0), so using it as depth would make every
    brand-new launch look well funded.
    """
    return state.get("real_quote_reserve")


def graduation_progress(state: dict) -> float | None:
    """0..1 toward graduation, by real quote raised against the threshold."""
    real, target = state.get("real_quote_reserve"), state.get("graduation_threshold")
    if real is None or not target:
        return None
    return min(real / target, 1.0)


def round_trip_cost_bps(state: dict) -> int | None:
    """What a buy-then-sell surrenders to fees and tax, in bps.

    Charged on both legs, so a 6% creator tax with a 1% fee costs ~14% round trip
    before any price move. This is the number that decides whether a curve is
    worth touching at all.
    """
    fee, tax = state.get("fee_bps"), state.get("creator_tax_bps")
    if fee is None and tax is None:
        return None
    return 2 * ((fee or 0) + (tax or 0))


def curve_flow_metrics(buys: list[dict], sells: list[dict], *,
                       quote_price_usd: float, quote_decimals: int = 18) -> dict:
    """Volume/net/buy/sell counts from decoded curve trades.

    Mirrors `aggregate_swaps` for pools, but reads the curve's own events. The
    quote leg is already explicit here, so there is no token0/token1 ordering and
    no sign convention to invert — the two event types ARE the direction.
    """
    scale = 10 ** quote_decimals
    vin = sum(b["quote_in"] for b in buys) / scale
    vout = sum(s["quote_out"] for s in sells) / scale
    return {
        "volume_usd": (vin + vout) * quote_price_usd,
        "net_usd": (vin - vout) * quote_price_usd,
        "buys": len(buys),
        "sells": len(sells),
    }
