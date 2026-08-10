"""Executing from the Definitive vault — the one place this repo spends money.

`definitive.py` speaks the API. This decides *whether and how much*, and it is
the only module both the CLI and the live loop call to trade, so a rule added
here cannot be bypassed by using the other entry point.

Three decisions worth stating, because each one is a trade that does not happen:

**Balances come from the chain, never from the book.** The chain is what a sell
settles against. A position record that disagrees with it is how you try to sell
tokens you no longer hold, and the failure mode is a rejected order at exactly
the moment you needed the exit to work.

**Quantities are truncated to the asset's own decimals.** Eight decimal places
against six-decimal USDG returns `400 "Internal server error"` — a message that
cost hours before it was pinned. Truncated rather than rounded, because rounding
up asks to spend more than the caller authorised.

**Automated exits carry a cooldown, and the book is updated on submit.** The mark
loop runs every two seconds; an exit intent stays true until the position
changes, so without both of those the same stop fires thirty times before the
first fill lands. Updating on submit breaks `live.py`'s "the book changes only on
a fill" rule on purpose: for an *exit*, the duplicate-sell risk is real and
immediate while the stale-book risk lasts seconds. The cooldown is the backstop
if a submit silently fails.
"""

from __future__ import annotations

import os
import time

from . import config as rcfg
from . import definitive as D

# How long after a SUCCESSFUL exit before the same token may be exited again.
EXIT_COOLDOWN_S = 60.0

# How long after a FAILED exit before retrying. This must be short. A stop that
# fails and then waits a full minute is not a stop — on a collapsing token that
# minute is the difference between -20% and -92%. Failure means the exit still
# has not happened, so the urgency is higher than before it was attempted, not
# lower.
EXIT_RETRY_S = float(os.getenv("RH_EXIT_RETRY_S", "3"))

# Slippage tolerance for EXITS, and it is deliberately nothing like the entry's.
# An entry that fails costs you a launch you did not need; an exit that fails
# costs you the position. A stop-loss submitted with 5% tolerance simply reverts
# while the price is falling — which is how a position rides from -20% to -92%
# with a stop rule that "fired" every tick.
EXIT_SLIPPAGE = float(os.getenv("RH_EXIT_SLIPPAGE", "0.25"))

# For a stop, a trail, or a rug: get out at any price the book will give. This
# is not a trade any more, it is an evacuation.
URGENT_SLIPPAGE = float(os.getenv("RH_URGENT_SLIPPAGE", "0.60"))

URGENT_REASONS = ("stop", "rug", "trail", "unsellable", "liquidity", "manual")


def exit_slippage_for(why: str) -> float:
    """How much slippage this exit should accept, from WHY it is exiting.

    A profit rung can be picky — if it does not fill, the position is still
    winning and the rung fires again next tick. A stop cannot: there is no next
    tick worth waiting for.
    """
    w = (why or "").lower()
    return URGENT_SLIPPAGE if any(k in w for k in URGENT_REASONS) else EXIT_SLIPPAGE

# The last positions payload seen, kept for diagnosis only. Never contains keys.
LAST_RAW: dict = {}


def cash(client, vault: str) -> dict:
    """Settlement balances at the vault, read on chain.

    The desk's P&L is meaningless without it: a book worth $8 next to $13 of
    USDG is a very different picture from a book worth $8 and nothing else.
    """
    out = {}
    for name in ("usdg", "weth"):
        addr = rcfg.CONTRACTS.get(name)
        if not addr:
            continue
        try:
            raw, dec, human = held(client, addr, vault)
        except Exception:
            continue
        out[name] = {"address": addr, "amount": human,
                     "usd": human if name == "usdg" else None}
    return out


def vault_address(wallet: str, chain: str = D.CHAIN) -> str:
    """The vault that holds the funds. `wallet` is YOUR address, not the vault's."""
    address, _ = D.deposit_address(chain, wallet_address=wallet)
    if not address:
        raise D.DefinitiveError("no vault address returned for %s" % chain)
    return address


def held(client, token: str, vault: str) -> tuple[int, int, float]:
    """(raw, decimals, human) balance of `token` at `vault`, read on chain."""
    raw = client.erc20_balance_of(token, vault)
    dec = client.erc20_decimals(token)
    return raw, dec, raw / (10 ** dec)


def fmt_qty(value: float, decimals: int) -> str:
    """Render a quantity the API will accept: truncated to `decimals`, no zeros."""
    from decimal import ROUND_DOWN, Decimal
    q = Decimal(str(value)).quantize(Decimal(1).scaleb(-max(decimals, 0)),
                                     rounding=ROUND_DOWN)
    return format(q.normalize(), "f")


def plan_sell(client, *, token: str, vault: str, fraction: float = 1.0,
              contra: str | None = None) -> dict:
    """Quote selling `fraction` of what the vault holds. Submits nothing.

    Returns a dict with `ok`; when False, `why` says which check stopped it so a
    caller can log a refusal without re-deriving the reason.
    """
    contra = contra or rcfg.CONTRACTS["usdg"]
    raw, dec, human = held(client, token, vault)
    if raw <= 0:
        return {"ok": False, "why": "vault holds none of this token",
                "token": token, "held": 0.0}
    qty_str = fmt_qty(human * max(min(fraction, 1.0), 0.0), dec)
    if float(qty_str) <= 0:
        return {"ok": False, "why": "size rounds to zero at %d decimals" % dec,
                "token": token, "held": human}
    try:
        q = D.quicktrade_quote(target=token, contra=contra, qty=qty_str, side="sell")
    except D.DefinitiveError as e:
        # An unquotable SELL is the honest unsellable signal — the same one the
        # live loop's rug check leans on. Surface it as data, not an exception,
        # so an automated exit can fall through to the curve/router path.
        return {"ok": False, "why": "not quotable: %s" % e, "raw": e.raw,
                "token": token, "held": human, "qty": qty_str, "contra": contra}
    return {"ok": True, "token": token, "contra": contra, "qty": qty_str,
            "held": human, "quote": q, "cost": D.quote_cost(q)}


def execute_sell(plan: dict, *, slippage: float = 0.05, confirm: bool = False) -> dict:
    """Submit a plan produced by `plan_sell`. Requires `confirm=True`."""
    if not plan.get("ok"):
        raise D.DefinitiveError("refusing to submit an unquotable sell: %s"
                                % plan.get("why"))
    return D.quicktrade_submit(target=plan["token"], contra=plan["contra"],
                               qty=plan["qty"], side="sell",
                               slippage_tolerance="%.4f" % slippage,
                               confirm=confirm)


_ADDR_RE = None


def find_address(node) -> str:
    """Pull a token address out of a response of unknown shape.

    The positions payload is undocumented and the first cut guessed at
    `address` / `assetAddress` / `asset.address`. When the real response nests
    them somewhere else the book comes back empty, the desk shows nothing, and
    it looks like you hold nothing — the worst possible failure mode for a
    screen you trust to show your money.

    A 40-hex address is unmistakable, so this walks the whole structure and
    takes the first one rather than requiring the key to be spelled the way I
    expected. Prefers keys that look like an asset address; falls back to any
    address-shaped string.
    """
    global _ADDR_RE
    if _ADDR_RE is None:
        import re
        _ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
    # Ranked, because "address-ish" is not good enough: a row can carry both
    # `vaultAddress` and `asset.address`, and taking the first one that merely
    # contains "address" picks the vault — then every balance lookup asks what
    # the vault holds *of itself*. Asset-ish keys win outright; anything naming
    # a counterparty is excluded from the middle tier rather than merely
    # outranked.
    NOT_THE_ASSET = ("vault", "wallet", "owner", "user", "account", "recipient",
                     "sender", "spender", "portfolio", "deposit")
    tiers: dict[int, list] = {0: [], 1: [], 2: []}

    def walk(n, key=""):
        if isinstance(n, dict):
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, (list, tuple)):
            for v in n:
                walk(v, key)
        elif isinstance(n, str) and _ADDR_RE.match(n):
            k = key.lower()
            if any(w in k for w in ("asset", "token", "contract", "mint")):
                tiers[0].append(n)
            elif "address" in k and not any(w in k for w in NOT_THE_ASSET):
                tiers[1].append(n)
            elif not any(w in k for w in NOT_THE_ASSET):
                tiers[2].append(n)

    walk(node)
    return (tiers[0] or tiers[1] or tiers[2] or [""])[0]


def find_symbol(node) -> str:
    """Ticker from a response of unknown shape — same reasoning as above."""
    found = []

    def walk(n, key=""):
        if isinstance(n, dict):
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, (list, tuple)):
            for v in n:
                walk(v, key)
        elif isinstance(n, str) and n and len(n) <= 16:
            k = key.lower()
            if k in ("symbol", "ticker", "assetsymbol"):
                found.append(n)

    walk(node)
    return found[0] if found else ""


def symbol_of(client, token: str) -> str:
    """Ticker from the chain, since the venue's own response often omits it.

    The desk shows an instrument, not an address — a row reading `0xd0f8a8` is
    not something you can recognise at a glance while deciding whether to close
    it.
    """
    try:
        raw = client.eth_call(token, "0x95d89b41")          # symbol()
        if raw and len(raw) > 130:
            n = int(raw[2:][64:128], 16)
            return bytes.fromhex(raw[2:][128:128 + n * 2]).decode("utf8", "replace")[:12]
    except Exception:
        pass
    return ""


def plan_buy(client, *, token: str, vault: str, usd: float,
             contra: str | None = None, quote_price_usd: float = 1.0) -> dict:
    """Quote spending `usd` of the contra asset on `token`. Submits nothing.

    The spend is capped by what the vault actually holds — a buy sized off a
    bankroll setting that exceeds the balance is a rejected order, and it gets
    rejected at the moment a launch is worth catching.
    """
    contra = contra or rcfg.CONTRACTS["usdg"]
    try:
        _, dec, have = held(client, contra, vault)
    except Exception:
        return {"ok": False, "why": "could not read the settlement balance",
                "token": token}
    want = usd / (quote_price_usd or 1.0)
    if want > have:
        want = have
    qty_str = fmt_qty(want, dec)
    if float(qty_str) <= 0:
        return {"ok": False, "why": "no settlement balance to spend",
                "token": token, "have": have}
    try:
        q = D.quicktrade_quote(target=token, contra=contra, qty=qty_str, side="buy")
    except D.DefinitiveError as e:
        # Definitive cannot price every fresh launch. That is a skip, not an
        # error: the scan should move to the next candidate, not stop.
        return {"ok": False, "why": "not quotable: %s" % e, "raw": e.raw,
                "token": token, "qty": qty_str, "contra": contra}
    return {"ok": True, "token": token, "contra": contra, "qty": qty_str,
            "quote": q, "cost": D.quote_cost(q)}


def round_trip_ok(plan: dict, *, min_recovery: float = 0.75) -> tuple[bool, float | None, str]:
    """Simulate the exit BEFORE the entry. (ok, recovery, why).

    This is `honeypot_ok` from `examples/cambrian_desk.py`, which the packaged
    gate never had — and its absence bought a honeypot within seconds of the
    scanner going live. The gate checks tax, liquidity and price; none of those
    answer the only question that matters, which is whether the position can be
    got out of at all.

    Quote the buy, then quote selling back **exactly the amount that buy would
    return**. A honeypot answers the first and refuses the second, or answers
    both with a sell worth a fraction of the buy. A declared tax of 0% means
    nothing here: the trap is usually in transfer logic, not in a tax field.

    Cheap — one extra quote per candidate — and it measures the real thing
    rather than a proxy for it.
    """
    cost = plan.get("cost") or {}
    tokens_out = cost.get("buy_amount")
    spend = cost.get("spend_usd")
    if not tokens_out or not spend:
        return False, None, "buy quote did not say what it returns"
    try:
        back = D.quicktrade_quote(target=plan["token"], contra=plan["contra"],
                                  qty=str(tokens_out), side="sell")
    except D.DefinitiveError as e:
        # Buyable but not sellable is the textbook honeypot.
        return False, 0.0, "cannot be sold back (%s)" % str(e)[:60]
    out = D.quote_cost(back).get("receive_usd")
    if not out:
        return False, 0.0, "sell quote returned nothing"
    recovery = out / spend
    if recovery < min_recovery:
        return False, recovery, "round trip returns %.0f%% (< %.0f%%)" % (
            recovery * 100, min_recovery * 100)
    return True, recovery, ""


def execute_buy(plan: dict, *, slippage: float = 0.05, confirm: bool = False) -> dict:
    """Submit a plan produced by `plan_buy`. Requires `confirm=True`."""
    if not plan.get("ok"):
        raise D.DefinitiveError("refusing to submit an unquotable buy: %s"
                                % plan.get("why"))
    return D.quicktrade_submit(target=plan["token"], contra=plan["contra"],
                               qty=plan["qty"], side="buy",
                               slippage_tolerance="%.4f" % slippage,
                               confirm=confirm)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def adopt(client, vault: str, *, exclude: tuple = (), now: float | None = None) -> dict:
    """Build a book from what the vault ACTUALLY holds.

    The desk has to manage positions it did not open — the one bought by hand a
    minute ago, or anything left behind when the process restarted. A loop that
    only knows about fills it personally saw will watch a real position go to
    zero without ever firing a stop, which is the worst failure this system has.

    Cost basis is taken from the venue's own accounting when it reports one. When
    it does not, the current exit value stands in and `basis_known` is False for
    that token — every percentage rule downstream is then measured from adoption
    rather than from entry, which is a materially different thing and must be
    said out loud rather than assumed.
    """
    from . import positions as P

    skip = {(a or "").lower() for a in exclude}
    skip |= {(rcfg.CONTRACTS.get(k) or "").lower() for k in ("usdg", "weth")}
    skip.discard("")

    try:
        payload = D.positions(include_dust=False)
    except D.DefinitiveError:
        payload = {}
    rows = payload.get("positions") or payload.get("data") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    # Keep the raw payload when nothing adopts: "you hold nothing" and "I could
    # not read the response" look identical on screen, and only one of them is
    # a reason to relax.
    LAST_RAW.clear()
    LAST_RAW.update(payload if isinstance(payload, dict) else {"payload": payload})

    book: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        asset = r.get("asset") or {}
        token = find_address(r)
        if not token or token.lower() in skip:
            continue
        try:
            raw, dec, human = held(client, token, vault)
        except Exception:
            continue
        if raw <= 0:
            continue
        value = (_f(r.get("notional")) or _f(r.get("notionalValue"))
                 or _f(r.get("usdValue")) or _f(r.get("notionalUsd"))
                 or _f((r.get("value") or {}).get("usd")
                       if isinstance(r.get("value"), dict) else r.get("value")))
        # Prefer a real basis if the venue reports one, directly or via PnL.
        basis = _f(r.get("costBasis")) or _f(r.get("cost")) or _f(r.get("totalCost"))
        pnl = _f(r.get("pnl")) or _f(r.get("unrealizedPnl"))
        if basis is None and value is not None and pnl is not None:
            basis = value - pnl
        basis_known = basis is not None and basis > 0
        if not basis_known:
            plan = plan_sell(client, token=token, vault=vault)
            basis = (plan.get("cost") or {}).get("receive_usd") if plan.get("ok") \
                else value
        if not basis or basis <= 0:
            continue
        pos = P.Position(token=token, venue_kind="vault", pool="", tokens=raw,
                         cost_usd=float(basis),
                         opened_at=(now if now is not None else time.time()),
                         peak_usd=float(basis),
                         # Nothing is known about an adopted position's setup,
                         # so it gets the default rather than a guess.
                         profile="standard")
        book[token] = {"position": pos, "decimals": dec, "held": human,
                       "basis_known": basis_known,
                       # basis0 is the ORIGINAL cost, kept because `P.apply`
                       # reduces `cost_usd` as rungs are trimmed. Realized P&L
                       # has to measure against what was actually paid, not
                       # against the remainder.
                       "basis0": float(basis), "banked": 0.0, "pad": "vault",
                       # Best available: basis over units held. For a position
                       # adopted rather than opened here it is the honest one.
                       "entry_px": (float(basis) / human) if human else None,
                       "symbol": (r.get("symbol") or asset.get("symbol")
                                  or find_symbol(r) or symbol_of(client, token))}
    return book


class ExitGuard:
    """Stops one exit intent from being submitted over and over.

    The mark loop re-evaluates every couple of seconds and an intent stays true
    until the position changes, so this is not a nicety — without it a single
    stop becomes a stream of duplicate sells.
    """

    def __init__(self, cooldown_s: float = EXIT_COOLDOWN_S,
                 retry_s: float = EXIT_RETRY_S):
        self.cooldown_s = cooldown_s
        self.retry_s = retry_s
        self.last: dict[str, float] = {}
        self.wait: dict[str, float] = {}

    def allow(self, token: str, *, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        key = token.lower()
        return (t - self.last.get(key, 0.0)) >= self.wait.get(key, self.cooldown_s)

    def note(self, token: str, *, now: float | None = None,
             ok: bool = True) -> None:
        """Record an attempt. A FAILED exit comes back in seconds, not a minute."""
        key = token.lower()
        self.last[key] = time.time() if now is None else now
        self.wait[key] = self.cooldown_s if ok else self.retry_s
