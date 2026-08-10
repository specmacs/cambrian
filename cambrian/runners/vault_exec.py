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

import time

from . import config as rcfg
from . import definitive as D

# How long after submitting an exit before the same token may be exited again.
EXIT_COOLDOWN_S = 60.0


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

    book: dict = {}
    for r in rows:
        asset = r.get("asset") or {}
        token = (r.get("address") or asset.get("address") or r.get("assetAddress")
                 or "")
        if not token or token.lower() in skip:
            continue
        try:
            raw, dec, human = held(client, token, vault)
        except Exception:
            continue
        if raw <= 0:
            continue
        value = _f(r.get("notional")) or _f(r.get("notionalValue")) \
            or _f(r.get("usdValue"))
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
                         peak_usd=float(basis))
        book[token] = {"position": pos, "decimals": dec, "held": human,
                       "basis_known": basis_known,
                       # basis0 is the ORIGINAL cost, kept because `P.apply`
                       # reduces `cost_usd` as rungs are trimmed. Realized P&L
                       # has to measure against what was actually paid, not
                       # against the remainder.
                       "basis0": float(basis), "banked": 0.0, "pad": "vault",
                       "symbol": (r.get("symbol") or asset.get("symbol")
                                  or symbol_of(client, token))}
    return book


class ExitGuard:
    """Stops one exit intent from being submitted over and over.

    The mark loop re-evaluates every couple of seconds and an intent stays true
    until the position changes, so this is not a nicety — without it a single
    stop becomes a stream of duplicate sells.
    """

    def __init__(self, cooldown_s: float = EXIT_COOLDOWN_S):
        self.cooldown_s = cooldown_s
        self.last: dict[str, float] = {}

    def allow(self, token: str, *, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        key = token.lower()
        if t - self.last.get(key, 0.0) < self.cooldown_s:
            return False
        return True

    def note(self, token: str, *, now: float | None = None) -> None:
        self.last[token.lower()] = time.time() if now is None else now
