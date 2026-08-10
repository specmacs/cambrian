"""The live terminal — what the vault actually holds, and the controls to act.

The paper terminal answers "would this have worked". This one answers "what am I
in, right now, and how do I get out" — so every number on it is a real quote
against real holdings, and two of its controls spend money.

**The engine runs whether or not a browser is open.** The page is a view onto a
loop that is already running; closing the tab stops nothing, and that is the
point. A desk that only manages positions while you are looking at it is not
managing them.

**STOP halts automation, it does not close anything.** Those are different
intentions and conflating them is how a panic click becomes a market sell.
STOP means "stop deciding for me"; CLOSE means "sell this now". Manual closes
still work while halted — halting is about the automation, not about your hands.

Bound to 127.0.0.1 only. Any process on this machine can reach the controls, so
this is a personal desk, not a shared one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ui_desk
from .runners import config as rcfg
from .runners import definitive as D
from .runners import positions as P
from .runners import vault_exec as VX

PORT = int(os.getenv("RH_TERMINAL_PORT", "8799"))
MAX_EVENTS = 200

# Reentrant, and not by accident: the engine holds the lock while adopting and
# calls `_event` inside that block, which takes it again. With a plain Lock the
# first successful adoption deadlocked the server — every request hung, and the
# bug stayed invisible for as long as adoption kept returning nothing.
_lock = threading.RLock()
STATE: dict = {
    "vault": None, "wallet": None, "halted": False, "book": {}, "events": [],
    "marks": 0, "mark_ms": 0.0, "last_error": None, "started_at": time.time(),
    "auto_exit": True, "realized": 0.0, "closed": [], "fills": [], "block": 0,
    "cash": {}, "adopt_note": "", "adopted_once": False,
    # --- entry side ---------------------------------------------------------
    "auto_buy": True, "scouting": [], "trace": [], "scan_ms": 0.0,
    "buys_this_hour": [], "last_scan": 0.0, "eth": 0.0,
    "max_usd": 8.0, "max_positions": 3, "buys_per_hour": 6, "scan_blocks": 400,
    # Fraction of a ticket that must survive an immediate round trip. 0.75 is
    # the value the original desk used; below it the token is a trap or the tax
    # is lying.
    "min_recovery": 0.75,
}


def _event(kind: str, text: str, token: str = "") -> None:
    with _lock:
        STATE["events"].insert(0, {"t": time.time(), "kind": kind, "text": text,
                                   "token": token})
        del STATE["events"][MAX_EVENTS:]


# --- engine ------------------------------------------------------------------

def _engine(client, *, slippage: float, interval: float) -> None:
    """Mark continuously, act when the policy says to.

    Marks run concurrently: serial marking makes the loop's period the sum of
    every position's round trip, and the last position marked is the one whose
    stop misses.
    """
    from concurrent.futures import ThreadPoolExecutor

    guard = VX.ExitGuard()
    vault = STATE["vault"]
    last_adopt = 0.0
    while True:
        try:
            # Re-adopt periodically so a position bought elsewhere (by hand, or
            # by `trade-vault`) starts being managed without a restart.
            if time.time() - last_adopt > 30:
                last_adopt = time.time()
                fresh = VX.adopt(client, vault)
                try:
                    STATE["cash"] = VX.cash(client, vault)
                except Exception:
                    pass
                with _lock:
                    # "You hold nothing" and "I could not read the response"
                    # render identically as an empty table, and only one of them
                    # means everything is fine. Say which.
                    if not fresh and not STATE["book"]:
                        raw = json.dumps(VX.LAST_RAW)[:200]
                        STATE["adopt_note"] = (
                            "No positions adopted. Vault response: %s" % raw)
                    else:
                        STATE["adopt_note"] = ""
                    STATE["adopted_once"] = True
                    for tok, entry in fresh.items():
                        if tok not in STATE["book"]:
                            STATE["book"][tok] = entry
                            _event("adopt", "now managing %s (basis $%.2f%s)"
                                   % (entry.get("symbol") or tok[:10],
                                      entry["position"].cost_usd,
                                      "" if entry["basis_known"] else ", assumed"),
                                   tok)

            with _lock:
                live = [(t, e) for t, e in STATE["book"].items()
                        if not e["position"].closed]
            if not live:
                time.sleep(max(interval, 1.0))
                continue

            t0 = time.time()
            with ThreadPoolExecutor(max_workers=min(8, len(live))) as pool:
                plans = list(pool.map(
                    lambda it: VX.plan_sell(client, token=it[0], vault=vault), live))
            elapsed = (time.time() - t0) * 1000

            with _lock:
                STATE["marks"] += 1
                STATE["mark_ms"] = elapsed
                STATE["last_error"] = None

            for (token, entry), plan in zip(live, plans):
                pos = entry["position"]
                if plan.get("held") is not None:
                    entry["held"] = plan["held"]
                # Sold somewhere else — through Definitive's own UI, or by hand.
                # A zero balance means the position is GONE, not that it cannot
                # be sold, and conflating the two would count unsellable
                # failures until the rug rule fired an exit against nothing.
                if plan.get("held", 0.0) > 0:
                    entry["seen_balance"] = True
                if not entry.get("seen_balance", True):
                    # Bought, not settled. There is nothing on chain to quote,
                    # so a mark here reads as zero and fires a stop at -100%
                    # against a position that does not exist yet. Do not mark
                    # and do not decide until a balance actually appears.
                    entry["signal"] = "awaiting fill"
                    continue
                if (plan.get("held") == 0.0 and not plan.get("ok")
                        and entry.get("seen_balance", True)):
                    with _lock:
                        entry["position"] = P.apply(pos, "close", 1.0)
                        STATE["closed"].insert(0, {
                            "token": token, "sym": entry.get("symbol") or "",
                            "pad": entry.get("pad") or "vault",
                            "cost": round(entry.get("basis0", pos.cost_usd), 2),
                            # Proceeds are unknown: the sale did not go through
                            # this desk. Better blank than a fabricated P&L.
                            "exit_value": None, "pnl": None,
                            "why": "closed outside the desk"})
                        del STATE["closed"][30:]
                    _event("close", "left the vault — sold outside the desk", token)
                    continue
                value = (plan.get("cost") or {}).get("receive_usd") \
                    if plan.get("ok") else None
                P.mark_from_exit_quote(pos, value)
                # exit_decision MUTATES: a rung it reports is a rung it records
                # as taken. So it must only ever be called when we can actually
                # act on the answer. Calling it to render a signal while halted
                # silently burned the 2x and 3x take-profits — they were marked
                # hit, nothing was sold, and on resume they could never fire
                # again. Evaluate only when the result can be executed.
                if STATE["halted"] or not STATE["auto_exit"]:
                    entry["signal"] = "halted"
                    continue
                if not guard.allow(token):
                    entry["signal"] = "cooling down"
                    continue
                action, fraction, why = P.exit_decision(pos)
                entry["signal"] = why if action else ""
                if not action:
                    continue
                _submit_exit(client, token, entry, fraction, why, guard,
                             slippage=slippage, plan=plan)
        except Exception as e:                  # the loop must outlive any tick
            with _lock:
                STATE["last_error"] = str(e)[:200]
            time.sleep(1.0)
        if interval:
            time.sleep(interval)


def _scanner(client, *, slippage: float) -> None:
    """Discover across every pad, gate, size, and buy. The point of the desk.

    This is `scanner.sweep` — the same discover -> resolve -> gate -> size pass
    the CLI uses — run on a loop with execution attached. Nothing about which
    tokens are acceptable is decided here: the gate already encodes the tax
    ceiling, the stock-pair exception and the liquidity checks, and duplicating
    any of that would let the two paths disagree about what is safe.

    Three caps stand between a bad scan and an empty vault: a per-ticket dollar
    cap, a maximum number of concurrent positions, and a per-hour buy count. A
    launch missed costs nothing; a runaway loop costs everything.
    """
    from .runners import config as _rcfg
    from .runners import scanner as SC
    from .runners.execution import live_eth_usd

    vault = STATE["vault"]
    eth = live_eth_usd(fallback=_rcfg.WETH_USD)
    usdg = _rcfg.CONTRACTS["usdg"]
    while True:
        try:
            t0 = time.time()
            latest = client.block_number()
            result = SC.sweep(client, from_block=max(latest - STATE["scan_blocks"], 0),
                              to_block=latest, quote_price_usd=eth,
                              bankroll_usd=STATE.get("bankroll"))
            rows = result["rows"]
            with _lock:
                STATE["block"] = latest
                STATE["eth"] = eth
                STATE["scan_ms"] = (time.time() - t0) * 1000
                STATE["last_scan"] = time.time()
                STATE["scouting"] = [_scout_row(r) for r in rows[:12]]
                # The trace is what the gate actually decided, not a narrative:
                # every blocked row carries its own reason already.
                STATE["trace"] = [{
                    "agent": "gate", "sym": r.get("symbol") or (r.get("token") or "")[:8],
                    "decision": "buy" if r.get("ok") else "skip",
                    "reason": "; ".join(r.get("reasons") or ["cleared the gate"]),
                    "conf": "", "net": 0.0, "liq": r.get("market_cap_usd") or 0.0,
                    "pad": r.get("pad") or "?", "t": time.strftime("%H:%M:%S"),
                } for r in rows[:20]]
            _maybe_buy(client, rows, vault=vault, usdg=usdg, slippage=slippage)
        except Exception as e:
            with _lock:
                STATE["last_error"] = "scan: %s" % str(e)[:160]
        time.sleep(max(float(os.getenv("RH_SCAN_INTERVAL_S", "8")), 2.0))


def _scout_row(r: dict) -> dict:
    """One candidate, in the shape the Scouting pane already renders."""
    return {"token": r.get("token") or "", "sym": r.get("symbol") or "",
            "pad": r.get("pad") or "?", "mc": r.get("market_cap_usd"),
            "net": r.get("size_usd") or 0.0,
            "liq": (r.get("real_backing_usd") or r.get("market_cap_usd") or 0.0),
            "conf": "OK" if r.get("ok") else "—",
            "tier": "" if r.get("ok") else (r.get("reasons") or ["blocked"])[0][:28],
            "verified": bool(r.get("ok"))}


def _room_to_buy() -> tuple[bool, str]:
    """Whether the caps allow another entry right now, and why not if they don't."""
    with _lock:
        if STATE["halted"] or not STATE["auto_buy"]:
            return False, "halted"
        open_n = sum(1 for e in STATE["book"].values() if not e["position"].closed)
        if open_n >= STATE["max_positions"]:
            return False, "at %d open positions" % open_n
        cutoff = time.time() - 3600
        STATE["buys_this_hour"] = [t for t in STATE["buys_this_hour"] if t > cutoff]
        if len(STATE["buys_this_hour"]) >= STATE["buys_per_hour"]:
            return False, "hourly buy cap"
    return True, ""


def _maybe_buy(client, rows, *, vault, usdg, slippage) -> None:
    """Buy the best candidate that clears the gate AND can be quoted."""
    from .runners import positions as P

    ok, _why = _room_to_buy()
    if not ok:
        return
    for r in rows:
        token = r.get("token")
        if not r.get("ok") or not token:
            continue
        with _lock:
            if token in STATE["book"] and not STATE["book"][token]["position"].closed:
                continue
            if token in STATE.get("bought_ever", set()):
                continue        # never re-enter the same token in one session
        size = min(r.get("size_usd") or 0.0, STATE["max_usd"])
        if size <= 0:
            continue
        plan = VX.plan_buy(client, token=token, vault=vault, usd=size,
                                  contra=usdg)
        if not plan.get("ok"):
            _event("skip", "%s — %s" % ((r.get("symbol") or token[:10]),
                                        plan.get("why", "")[:80]), token)
            continue
        # Simulate the EXIT before taking the entry. The gate's tax and
        # liquidity checks cannot see a transfer-logic trap; only trying to sell
        # can. This is the check that was missing when the scanner bought a
        # honeypot in its first seconds.
        sellable, recovery, why_not = VX.round_trip_ok(
            plan, min_recovery=STATE["min_recovery"])
        if not sellable:
            _event("honeypot", "%s BLOCKED — %s"
                   % (r.get("symbol") or token[:10], why_not), token)
            with _lock:
                STATE.setdefault("bought_ever", set()).add(token)   # never retry
            continue
        cost = plan["cost"]
        spend = cost.get("spend_usd") or size
        if spend > STATE["max_usd"] * 1.05:
            _event("skip", "quote wanted $%.2f, over the $%.2f cap"
                   % (spend, STATE["max_usd"]), token)
            continue
        try:
            out = VX.execute_buy(plan, slippage=slippage, confirm=True)
        except Exception as e:
            _event("fail", "buy failed: %s" % str(e)[:120], token)
            continue
        sym = r.get("symbol") or VX.symbol_of(client, token) or token[:8]
        with _lock:
            STATE["buys_this_hour"].append(time.time())
            STATE.setdefault("bought_ever", set()).add(token)
            pos = P.Position(token=token, venue_kind="vault", pool="", tokens=0,
                             cost_usd=float(spend), opened_at=time.time(),
                             peak_usd=float(spend))
            STATE["book"][token] = {
                "position": pos, "decimals": 18, "held": 0.0,
                "basis_known": True, "basis0": float(spend), "banked": 0.0,
                "pad": r.get("pad") or "?", "symbol": sym,
                "mc": r.get("market_cap_usd"),
                # Until the fill lands the on-chain balance is zero, and a zero
                # balance is otherwise read as "sold elsewhere". This says the
                # position has never been seen on chain yet, so do not close it.
                "seen_balance": False}
            STATE["fills"].insert(0, {
                "t": time.strftime("%H:%M:%S"), "side": "BUY", "sym": sym,
                "token": token, "pad": r.get("pad") or "?", "agent": "scan",
                "usd": round(spend, 2), "pnl": None})
            del STATE["fills"][60:]
        _event("buy", "bought $%.2f of %s (%s, MC $%s, round trip %.0f%%)"
               % (spend, sym, r.get("pad") or "?",
                  ("%.0f" % r["market_cap_usd"]) if r.get("market_cap_usd") else "?",
                  (recovery or 0) * 100),
               token)
        return          # one entry per scan, deliberately


def _submit_exit(client, token, entry, fraction, why, guard, *, slippage,
                 plan=None) -> dict:
    """Sell `fraction` of a position and record it. Shared by auto and manual."""
    vault = STATE["vault"]
    sell = plan if (plan and plan.get("ok") and fraction >= 1.0) else \
        VX.plan_sell(client, token=token, vault=vault, fraction=fraction)
    if not sell.get("ok"):
        guard.note(token)
        _event("fail", "exit refused (%s): %s" % (why, sell.get("why")), token)
        return {"ok": False, "why": sell.get("why")}
    try:
        out = VX.execute_sell(sell, slippage=slippage, confirm=True)
    except Exception as e:
        guard.note(token)
        _event("fail", "submit failed: %s" % str(e)[:120], token)
        return {"ok": False, "why": str(e)[:200]}
    guard.note(token)
    pos = entry["position"]
    # Applied on submit rather than on fill: the intent stays true until the
    # position changes, so waiting for confirmation re-sells every tick.
    entry["position"] = P.apply(pos, "close" if fraction >= 1.0 else "trim",
                                fraction)
    proceeds = (sell.get("cost") or {}).get("receive_usd") or 0.0
    with _lock:
        entry["banked"] = entry.get("banked", 0.0) + proceeds
        STATE["fills"].insert(0, {
            "t": time.strftime("%H:%M:%S"), "side": "SELL", "sym": entry.get("symbol") or "",
            "token": token, "pad": entry.get("pad") or "vault", "agent": why,
            "usd": round(proceeds, 2),
            "pnl": round(proceeds - pos.cost_usd * fraction, 2)})
        del STATE["fills"][60:]
        if entry["position"].closed:
            # Realized is banked-minus-basis, counted once, when the last of the
            # position leaves. Counting it per trim would double-count the rungs.
            realized = entry["banked"] - entry.get("basis0", pos.cost_usd)
            STATE["realized"] += realized
            STATE["closed"].insert(0, {
                "token": token, "sym": entry.get("symbol") or "",
                "pad": entry.get("pad") or "vault",
                "cost": round(entry.get("basis0", pos.cost_usd), 2),
                "exit_value": round(entry["banked"], 2),
                "pnl": round(realized, 2), "why": why})
            del STATE["closed"][30:]
    _event("exit", "sold %.0f%% — %s (%s)"
           % (fraction * 100, why, json.dumps(out)[:60]), token)
    return {"ok": True, "order": out}


# --- snapshot ----------------------------------------------------------------

def snapshot() -> dict:
    """The payload `ui_desk.PAGE` already knows how to render.

    Written to the page's existing contract rather than the other way round: the
    interface was designed once and should not be re-cut to suit whatever the
    backend finds convenient. Fields the vault has no analogue for (scouting,
    agent traces) come back empty rather than invented — an empty pane reads as
    "nothing here", a fabricated one reads as a lie.
    """
    with _lock:
        positions, unreal = [], 0.0
        for token, entry in STATE["book"].items():
            pos = entry["position"]
            if pos.closed:
                continue
            mark = pos.mark_usd if pos.mark_usd is not None else pos.cost_usd
            basis = entry.get("basis0", pos.cost_usd)
            upnl = (mark + entry.get("banked", 0.0)) - basis
            unreal += mark - pos.cost_usd
            positions.append({
                "token": token, "sym": entry.get("symbol") or token[:6],
                "pad": entry.get("pad") or "vault", "agent": "exit-policy",
                "cost": round(pos.cost_usd, 2), "value": round(mark, 2),
                "upnl": round(upnl, 2),
                "chg": round(100 * (mark / pos.cost_usd - 1), 1) if pos.cost_usd else 0.0,
                "opened": int(pos.opened_at), "mc": entry.get("mc"),
                "peak_mult": round(max(pos.peak_usd / pos.cost_usd, 1.0), 2)
                             if pos.cost_usd else 1.0,
                "rungs": len(pos.rungs_hit), "banked": round(entry.get("banked", 0.0), 2),
                "unsellable": pos.fails > 0,
            })
        positions.sort(key=lambda p: -p["upnl"])
        book_value = sum(
            (e["position"].mark_usd if e["position"].mark_usd is not None
             else e["position"].cost_usd)
            for e in STATE["book"].values() if not e["position"].closed)
        cash_usd = sum(c["usd"] for c in STATE["cash"].values()
                       if c.get("usd") is not None)
        closed = list(STATE["closed"])
        wins = sum(1 for c in closed if c["pnl"] > 0)
        return {
            "mode": "Vault", "halted": STATE["halted"],
            "updated": time.strftime("%H:%M:%S"),
            "realized": round(STATE["realized"], 2),
            "unrealized": round(unreal, 2),
            # Account value, not a paper P&L that starts at zero: cash plus what
            # the open book would fetch right now. That is the number you check
            # to know where you stand.
            "equity": round(cash_usd + book_value, 2),
            "book_value": round(book_value, 2), "cash_usd": round(cash_usd, 2),
            "adopt_note": STATE["adopt_note"],
            "wins": wins, "losses": len(closed) - wins,
            "positions": positions, "closed": closed,
            "blotter": list(STATE["fills"]),
            # No scout pane and no agent trace on a vault desk: this manages what
            # is held, it does not hunt. Empty beats fabricated.
            "scouting": list(STATE["scouting"]), "trace": list(STATE["trace"]),
            "memory": {"n": 0}, "auto_buy": STATE["auto_buy"],
            "agents": [{"name": "Scanner",
                        "on": STATE["auto_buy"] and not STATE["halted"]},
                       {"name": "Exit policy", "on": not STATE["halted"]},
                       {"name": "Mark loop", "on": True}],
            "block": STATE["block"], "model": "gate+exit-policy",
            "size": STATE["max_usd"], "eth": round(STATE["eth"], 2),
            "marked": int(STATE["mark_ms"] / 1000) if STATE["mark_ms"] else 0,
            "scan_ms": STATE["scan_ms"], "err": STATE["last_error"],
            "now": int(time.time()), "wallets": 0,
            "vault": STATE["vault"],
        }


# --- server ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    client = None
    slippage = 0.05

    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/data") or self.path.startswith("/api/state"):
            return self._json(snapshot())
        if self.path.startswith("/wallets"):
            return self._json({"wallets": [], "count": 0})
        body = ui_desk.PAGE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        path = self.path.split("?")[0]

        if path == "/api/scan":
            with _lock:
                STATE["auto_buy"] = bool(payload.get("on", True))
            _event("scan", "entries %s"
                   % ("ON" if STATE["auto_buy"] else "OFF — nothing will be bought"))
            return self._json({"auto_buy": STATE["auto_buy"]})

        if path == "/api/halt":
            with _lock:
                STATE["halted"] = bool(payload.get("halted", True))
            _event("halt", "automation %s"
                   % ("HALTED — no automatic exits will fire"
                      if STATE["halted"] else "resumed"))
            return self._json({"halted": STATE["halted"]})

        if path == "/api/close":
            token = payload.get("token") or ""
            pct = float(payload.get("pct") or 100.0)
            with _lock:
                entry = STATE["book"].get(token)
            if entry is None:
                return self._json({"ok": False, "why": "unknown position"}, 404)
            # A manual close is intentional and immediate: it bypasses the
            # cooldown (which exists to stop a repeating automatic signal, not a
            # person) but still goes through the same submit path.
            res = _submit_exit(self.client, token, entry, pct / 100.0,
                               "manual close", VX.ExitGuard(cooldown_s=0),
                               slippage=self.slippage)
            return self._json(res, 200 if res.get("ok") else 400)

        if path == "/api/close-all":
            with _lock:
                items = [(t, e) for t, e in STATE["book"].items()
                         if not e["position"].closed]
            out = []
            for token, entry in items:
                out.append({"token": token,
                            **_submit_exit(self.client, token, entry, 1.0,
                                           "close all", VX.ExitGuard(cooldown_s=0),
                                           slippage=self.slippage)})
            return self._json({"results": out})

        if path == "/wallets":
            return self._json({"ok": True, "count": 0})
        return self._json({"ok": False, "why": "unknown endpoint"}, 404)


def serve(client, *, wallet: str, slippage: float = 0.05,
          interval: float = 0.0, port: int = PORT) -> int:
    vault = VX.vault_address(wallet)
    with _lock:
        STATE["vault"], STATE["wallet"] = vault, wallet
    _event("boot", "vault %s" % vault)
    Handler.client, Handler.slippage = client, slippage
    threading.Thread(target=_engine, args=(client,),
                     kwargs={"slippage": slippage, "interval": interval},
                     daemon=True).start()
    threading.Thread(target=_scanner, args=(client,),
                     kwargs={"slippage": slippage}, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("CAMBRIAN live terminal  ->  http://127.0.0.1:%d" % port)
    print("vault %s" % vault)
    print("entries %s  (<= $%.2f each, <= %d open, <= %d/hour)   exits %s"
          % ("ON" if STATE["auto_buy"] else "OFF", STATE["max_usd"],
             STATE["max_positions"], STATE["buys_per_hour"],
             "ON" if STATE["auto_exit"] else "OFF"))
    print("Stop automation halts BOTH. It does not sell.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
