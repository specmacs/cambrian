"""Small, dependency-free numeric helpers shared across desks.

Kept deliberately boring: money and risk math is exactly where a cute
one-liner turns into a wrong trade.
"""

from __future__ import annotations

import math


def bps_to_fraction(bps: float) -> float:
    """300 bps -> 0.03."""
    return bps / 10_000.0


def fraction_to_bps(fraction: float) -> float:
    """0.03 -> 300 bps."""
    return fraction * 10_000.0


def pct(fraction: float) -> str:
    """0.0342 -> '3.42%'. Formatting only, never for comparisons."""
    return f"{fraction * 100:.2f}%"


def usd(amount: float) -> str:
    """12345.6 -> '$12,345.60'. Formatting only."""
    return f"${amount:,.2f}"


def scale_down(raw: int, decimals: int) -> float:
    """On-chain integer amount -> human float. Loses precision; display/threshold
    use only, never feed back into a signed transaction."""
    return raw / (10 ** decimals)


def scale_up(amount: float, decimals: int) -> int:
    """Human float -> on-chain integer amount (floored)."""
    return int(amount * (10 ** decimals))


def impermanent_loss(price_ratio: float) -> float:
    """Magnitude (a positive fraction) of impermanent loss for a full-range
    constant-product LP position after the volatile asset's price moves by
    `price_ratio` relative to entry.

        IL = 1 - 2*sqrt(r)/(1+r)

    The formula is symmetric: a 1.25x move and a 0.8x move lose the same. So a
    single ratio captures the stress in either direction.

    This is the classic v2 / full-range approximation. A concentrated v3/v4
    position inside its range loses *more* than this for the same move, so
    treat the number as a floor on the pain, not the whole of it.
    """
    if price_ratio <= 0:
        raise ValueError(f"price_ratio must be positive, got {price_ratio}")
    r = price_ratio
    return 1.0 - (2.0 * math.sqrt(r) / (1.0 + r))


def addr_eq(a: str | None, b: str | None) -> bool:
    """Case-insensitive address equality. Never compare addresses with ==."""
    if a is None or b is None:
        return False
    return a.lower() == b.lower()


def addr_in(addr: str | None, collection) -> bool:
    """Membership test that normalizes case. `collection` may be a dict keyed by
    address, a set, or any iterable of address strings."""
    if addr is None:
        return False
    needle = addr.lower()
    return any(needle == str(item).lower() for item in collection)
