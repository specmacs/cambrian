"""One interface over every place a Robinhood Chain token can trade.

The desk has to answer four questions about ANY token, regardless of which pad
launched it: what is it worth, what will I actually pay to get in and out, can I
get out at all, and how much should I buy. Today those answers live in five
different shapes:

    pons-v1     v3 pool vs WETH, real reserves
    pons-v2     per-token bonding curve, PHANTOM quote reserve, quote may be
                native ETH or any approved ERC-20, 6- or 18-decimal
    flap        bonding curve inside the Portal; the "V2 pair" is a beacon-proxy
                shell reporting VIRTUAL reserves it does not hold
    pools.trade hookless v4 pool via the Liquidity Launcher
    graduated   a real Uniswap V2 pair once a curve completes

The unifying insight: pons-v2 and flap are both constant-product curves whose
*pricing* reserves are virtual. That is fine for pricing — a virtual reserve is
exactly what the contract prices against, so it gives the true marginal price —
but it is NOT depth. Depth is what the venue really holds and can pay out.

So every venue here carries two separate numbers, and conflating them is the
single most expensive mistake available:

    pricing_reserves   what quotes are computed from (may be virtual)
    real_backing_quote what could actually be paid out on exit (never virtual)

A flap pair reports 1.9190 WETH of reserve with `balanceOf(pair) == 0` on both
legs. Priced off the reserve it looks like an $11.5k pool; measured by backing it
holds nothing, because the assets are pooled in the Portal. Both statements are
true and they answer different questions.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as rcfg

BPS = 10_000
NATIVE_ETH = "0x0000000000000000000000000000000000000000"

# Venue kinds.
PONS_V1 = "pons-v1-v3"
PONS_V2 = "pons-v2-curve"
FLAP = "flap-curve"
POOLS_TRADE = "pools-trade-v4"
UNI_V2 = "uni-v2"

# Kinds that price against a virtual/phantom reserve. Their quotes are correct;
# their reserves are not proof of assets.
CURVE_KINDS = frozenset({PONS_V2, FLAP})


@dataclass(frozen=True)
class Venue:
    """Everything needed to price, cost, gate and size one token."""
    kind: str
    token: str
    pool: str                          # pool / pair / curve address
    quote_token: str = NATIVE_ETH
    quote_decimals: int = 18
    token_decimals: int = 18
    pricing_reserves: tuple[int, int] | None = None   # (quote, token) — may be virtual
    real_backing_quote: int | None = None             # exitable quote asset
    fee_bps: int | None = None
    tax_bps: int | None = None                        # creator/token tax, per leg
    total_supply: int | None = None
    graduated: bool | None = None
    sellable_tokens: int | None = None

    @property
    def prices_off_virtual_reserves(self) -> bool:
        return self.kind in CURVE_KINDS


def _u(client, to: str, data: str) -> int | None:
    try:
        raw = client.eth_call(to, data)
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def _decimals(client, token: str) -> int:
    if not token or token.lower() == NATIVE_ETH:
        return 18
    return _u(client, token, "0x313ce567") or 18


def _balance_of(client, token: str, holder: str) -> int | None:
    if not token or token.lower() == NATIVE_ETH:
        return None
    return _u(client, token, "0x70a08231" + holder[2:].rjust(64, "0"))


# --- resolution -------------------------------------------------------------

def from_pons_v2(client, *, token: str, curve: str, quote_token: str) -> Venue:
    from .pons_v2 import read_curve_state
    st = read_curve_state(client, curve)
    return Venue(
        kind=PONS_V2, token=token, pool=curve, quote_token=quote_token,
        quote_decimals=_decimals(client, quote_token),
        token_decimals=_decimals(client, token),
        pricing_reserves=(st["quote_reserve"], st["token_reserve"]),
        real_backing_quote=st["real_quote_reserve"],   # NOT getReserves
        fee_bps=st["fee_bps"], tax_bps=st["creator_tax_bps"],
        total_supply=_u(client, token, "0x18160ddd"),
        graduated=st["graduated"], sellable_tokens=st["sellable_tokens"],
    )


def from_v2_pair(client, *, token: str, pair: str, quote_token: str,
                 kind: str = UNI_V2) -> Venue:
    """A V2-shaped venue: a real Uniswap V2 pair, or a flap curve shell.

    Which one it is falls out of `real_backing_quote`: a genuine pair holds its
    reserves, a shell holds nothing. Callers do not have to know in advance.
    """
    from .uniswap_v2 import read_reserves
    reserves = read_reserves(client, pair)
    qt = (quote_token or "").lower()
    t0 = (_u(client, pair, "0x0dfe1681") or 0)
    quote_is_token0 = f"{t0:040x}".endswith(qt[2:]) if qt.startswith("0x") else True
    pricing = None
    if reserves:
        pricing = (reserves[0], reserves[1]) if quote_is_token0 \
            else (reserves[1], reserves[0])
    return Venue(
        kind=kind, token=token, pool=pair, quote_token=quote_token,
        quote_decimals=_decimals(client, quote_token),
        token_decimals=_decimals(client, token),
        pricing_reserves=pricing,
        real_backing_quote=_balance_of(client, quote_token, pair),
        fee_bps=30, tax_bps=None,
        total_supply=_u(client, token, "0x18160ddd"),
    )


def from_flap(client, *, token: str, pair: str,
              quote_token: str | None = None) -> Venue:
    """flap: priced off its pair shell's virtual reserves, taxed on the token.

    Backing is deliberately left None rather than read off the pair — the pair
    holds nothing, and reporting 0 would read as "rugged" when the real position
    is "pooled in the Portal". None means UNKNOWN, which the gate treats as
    unproven rather than as empty.
    """
    from .flap_tax import worst_tax_bps, read_tax_bps
    v = from_v2_pair(client, token=token, pair=pair,
                     quote_token=quote_token or rcfg.CONTRACTS["weth"], kind=FLAP)
    return Venue(**{**v.__dict__, "tax_bps": worst_tax_bps(read_tax_bps(client, token)),
                    "fee_bps": 30, "real_backing_quote": None})


# --- pricing ----------------------------------------------------------------

def price_quote_per_token(v: Venue) -> float | None:
    """Marginal price of one whole token, in whole units of the quote asset."""
    if not v.pricing_reserves or None in v.pricing_reserves:
        return None
    q, t = v.pricing_reserves
    if not t:
        return None
    return (q / 10 ** v.quote_decimals) / (t / 10 ** v.token_decimals)


def market_cap_quote(v: Venue) -> float | None:
    """Fully diluted value in quote units.

    FDV, not float-adjusted: these tokens mint their whole supply at launch (1e27
    observed on every pad), so FDV is the number the market actually quotes.
    """
    p = price_quote_per_token(v)
    if p is None or not v.total_supply:
        return None
    return p * (v.total_supply / 10 ** v.token_decimals)


def market_cap_usd(v: Venue, quote_price_usd: float) -> float | None:
    mc = market_cap_quote(v)
    return None if mc is None else mc * quote_price_usd


def _cp_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    return (amount_in * reserve_out) // (reserve_in + amount_in)


def quote_buy(v: Venue, quote_in: int) -> int | None:
    """Tokens received for an exact quote-asset input, fees taken off the input."""
    if not v.pricing_reserves or None in v.pricing_reserves or quote_in <= 0:
        return None
    q, t = v.pricing_reserves
    cut = quote_in * ((v.fee_bps or 0) + (v.tax_bps or 0)) // BPS
    out = _cp_out(quote_in - cut, q, t)
    if v.sellable_tokens is not None:
        out = min(out, v.sellable_tokens)
    return out


def quote_sell(v: Venue, tokens_in: int) -> int | None:
    """Quote asset received for an exact token input, fees taken off the output.

    Deliberately the mirror of `quote_buy`: on a curve the fee side differs by
    direction, and applying it to the input on a sell overstates proceeds.
    """
    if not v.pricing_reserves or None in v.pricing_reserves or tokens_in <= 0:
        return None
    q, t = v.pricing_reserves
    gross = _cp_out(tokens_in, t, q)
    return gross - (gross * ((v.fee_bps or 0) + (v.tax_bps or 0)) // BPS)


def round_trip(v: Venue, quote_in: int) -> dict | None:
    """What a buy-then-immediate-sell of `quote_in` actually costs, split out.

    Tax and slippage are different animals and want different responses: tax is
    fixed per token and either acceptable or not, while slippage is a function of
    size against depth and is fixed by trading smaller. Reporting only the total
    hides which one is hurting.
    """
    tokens = quote_buy(v, quote_in)
    if not tokens:
        return None
    back = quote_sell(v, tokens)
    if back is None:
        return None
    free = Venue(**{**v.__dict__, "fee_bps": 0, "tax_bps": 0})
    tokens_f = quote_buy(free, quote_in)
    back_f = quote_sell(free, tokens_f) if tokens_f else 0
    return {
        "tokens_out": tokens,
        "quote_back": back,
        "total_loss_bps": round((quote_in - back) * BPS / quote_in),
        "fee_tax_bps": 2 * ((v.fee_bps or 0) + (v.tax_bps or 0)),
        "slippage_bps": round((quote_in - back_f) * BPS / quote_in),
        "returned_pct": 100.0 * back / quote_in,
    }


# --- the gate ---------------------------------------------------------------

def tradeable(v: Venue, *, max_tax_bps: int | None = None,
              require_backing: bool = False) -> dict:
    """Should the desk touch this at all? Returns {ok, reasons}.

    Fails closed: anything unknown blocks rather than passes. Tax is checked
    first because it is a certain loss rather than a risk — a token can be
    perfectly liquid, perfectly sellable, and still hand back 10% on exit.
    """
    from .flap_tax import MAX_TAX_BPS
    limit = MAX_TAX_BPS if max_tax_bps is None else max_tax_bps
    reasons: list[str] = []
    if v.tax_bps is not None and v.tax_bps > limit:
        reasons.append(f"tax {v.tax_bps / 100:.2g}% > {limit / 100:.0f}%")
    if v.graduated is False and v.sellable_tokens == 0:
        reasons.append("curve closed, not yet graduated")
    if not v.pricing_reserves or None in (v.pricing_reserves or (None,)):
        reasons.append("no price")
    if require_backing:
        if v.real_backing_quote is None:
            reasons.append("backing unknown")
        elif v.real_backing_quote <= 0:
            reasons.append("no real backing")
    return {"ok": not reasons, "reasons": reasons}
