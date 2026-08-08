"""Routing from the settlement asset to a position, and back out again.

The desk holds **USDG or ETH** on Robinhood Chain (USDC or ETH on Base). Nothing
launches denominated in USDG, so almost every entry crosses at least one asset
boundary, and the boundary is where the mistakes live.

Three shapes, in increasing order of how much can go wrong:

1. **Flash-routable, settlement is the contra.** One order. Flash shops 200+
   venues and takes USDG directly — $25 USDG into FRONG quoted 0.18% impact live.
2. **Curve-direct, curve quote IS the settlement asset.** One curve call. Only
   happens for native-ETH launches when settling in ETH.
3. **Curve-direct, curve quote is NOT the settlement asset.** TWO legs: swap
   settlement into the curve's quote asset via Flash, then call the curve. This
   is the common case — ~60% of Pons v2 launches are stock-paired, so buying one
   from a USDG balance means USDG -> AAPL -> curve.

Leg 3 has a cost the single-leg cases do not: you pay slippage twice and you are
exposed to the intermediate asset between legs. A stock token can move between
the swap and the curve call, so the size that arrives is not exactly the size
quoted. `plan_route` reports the extra hop explicitly rather than folding it into
one number, because a two-leg entry into a thin curve can cost more in transit
than the edge being chased.

**USDG is 6 decimals, not 18.** $25 is `25_000_000`. Every amount here is scaled
from the asset's own decimals for exactly this reason — an 18-decimal assumption
overstates a USDG amount by 10^12 and would size a position a trillion times too
large, or revert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config as rcfg
from . import execution as X
from . import venues as V

# Settlement assets the desk actually holds on Robinhood Chain.
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDG_DECIMALS = 6                       # NOT 18 — verified on-chain
NATIVE_ETH = V.NATIVE_ETH

SETTLEMENT_DECIMALS = {USDG.lower(): USDG_DECIMALS, NATIVE_ETH: 18}

LEG_FLASH = "flash"
LEG_CURVE = "curve"


@dataclass(frozen=True)
class Leg:
    """One hop. `kind` says who executes it, not what it costs."""
    kind: str
    describe: str
    spend_asset: str
    receive_asset: str
    spend_amount: int | None = None


@dataclass(frozen=True)
class Route:
    legs: tuple[Leg, ...] = ()
    notes: tuple[str, ...] = field(default=())
    ok: bool = True
    reasons: tuple[str, ...] = ()

    @property
    def hops(self) -> int:
        return len(self.legs)

    @property
    def crosses_intermediate(self) -> bool:
        """True when the route is exposed to an asset it does not intend to hold."""
        return len(self.legs) > 1


def settlement_decimals(asset: str) -> int:
    return SETTLEMENT_DECIMALS.get((asset or "").lower(), 18)


def to_units(amount_usd: float, asset: str, *, price_usd: float) -> int:
    """USD -> the asset's own base units.

    Routed through the asset's real decimals because USDG's 6 is the single most
    likely place to be off by 10^12.
    """
    if price_usd <= 0:
        raise ValueError("price must be positive")
    return int(amount_usd / price_usd * (10 ** settlement_decimals(asset)))


def plan_route(venue: V.Venue, *, settlement_asset: str) -> Route:
    """How to get from the settlement asset into this position.

    Returns the legs rather than executing them. A blocked venue still returns a
    Route so the caller can show WHY nothing will happen — silence is
    indistinguishable from a broken feed.
    """
    settle = (settlement_asset or "").lower()
    quote = (venue.quote_token or "").lower()
    route_kind = X.route_for(venue)

    if route_kind == X.ROUTE_FLASH:
        # Flash takes the settlement asset directly as contra; no intermediate.
        return Route(legs=(Leg(kind=LEG_FLASH,
                               describe="Flash market buy, %s as contra" % _label(settle),
                               spend_asset=settlement_asset,
                               receive_asset=venue.token),),
                     notes=("single hop — Flash shops 200+ venues",))

    if quote == settle:
        return Route(legs=(Leg(kind=LEG_CURVE,
                               describe="curve buy (settlement IS the curve quote)",
                               spend_asset=settlement_asset,
                               receive_asset=venue.token),),
                     notes=("single hop — no asset boundary crossed",))

    # Curve-direct whose quote asset we do not hold: swap in first.
    return Route(
        legs=(Leg(kind=LEG_FLASH,
                  describe="swap %s -> %s (the curve's quote asset)"
                           % (_label(settle), _label(quote)),
                  spend_asset=settlement_asset, receive_asset=venue.quote_token),
              Leg(kind=LEG_CURVE, describe="curve buy with %s" % _label(quote),
                  spend_asset=venue.quote_token, receive_asset=venue.token)),
        notes=("TWO hops: slippage is paid twice",
               "exposed to %s between legs — the amount that arrives is not the "
               "amount quoted" % _label(quote)))


def exit_route(venue: V.Venue, *, settlement_asset: str) -> Route:
    """Getting back OUT to the settlement asset.

    Deliberately its own function rather than the entry reversed: a curve sale
    returns the curve's quote asset, so a stock-paired position exits into a
    STOCK, which then has to be sold for USDG. An exit planned as "the entry
    backwards" silently leaves the desk holding equity it never chose.
    """
    settle = (settlement_asset or "").lower()
    quote = (venue.quote_token or "").lower()
    if X.route_for(venue) == X.ROUTE_FLASH:
        return Route(legs=(Leg(kind=LEG_FLASH,
                               describe="Flash market sell to %s" % _label(settle),
                               spend_asset=venue.token,
                               receive_asset=settlement_asset),))
    legs = [Leg(kind=LEG_CURVE, describe="curve sell -> %s" % _label(quote),
                spend_asset=venue.token, receive_asset=venue.quote_token)]
    notes: tuple[str, ...] = ()
    if quote != settle:
        legs.append(Leg(kind=LEG_FLASH,
                        describe="swap %s -> %s" % (_label(quote), _label(settle)),
                        spend_asset=venue.quote_token,
                        receive_asset=settlement_asset))
        notes = ("exit lands in %s first — the desk holds it until leg 2 clears"
                 % _label(quote),)
    return Route(legs=tuple(legs), notes=notes)


def _label(asset: str) -> str:
    a = (asset or "").lower()
    if not a or a == NATIVE_ETH:
        return "ETH"
    if a == USDG.lower():
        return "USDG"
    if a == (rcfg.CONTRACTS.get("weth") or "").lower():
        return "WETH"
    from .stock_tokens import symbol_for
    return symbol_for(a) or a[:8]


def describe(route: Route) -> str:
    """One-line human summary, hops and warnings included."""
    if not route.legs:
        return "no route (%s)" % ("; ".join(route.reasons) or "unknown")
    path = " -> ".join([_label(route.legs[0].spend_asset)]
                       + [_label(l.receive_asset) for l in route.legs])
    tail = ("  [%s]" % "; ".join(route.notes)) if route.notes else ""
    return "%d hop%s: %s%s" % (route.hops, "" if route.hops == 1 else "s", path, tail)
