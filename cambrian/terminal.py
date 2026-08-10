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
            "scouting": [], "trace": [], "memory": {"n": 0},
            "agents": [{"name": "Exit policy", "on": not STATE["halted"]},
                       {"name": "Mark loop", "on": True}],
            "block": STATE["block"], "model": "exit-policy",
            "size": 0, "eth": 0,
            "marked": int(STATE["mark_ms"] / 1000) if STATE["mark_ms"] else 0,
            "scan_ms": STATE["mark_ms"], "err": STATE["last_error"],
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("CAMBRIAN live terminal  ->  http://127.0.0.1:%d" % port)
    print("vault %s   auto-exit ON   (STOP halts automation, it does not sell)"
          % vault)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
