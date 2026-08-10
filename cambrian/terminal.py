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

from . import ui_css
from .runners import config as rcfg
from .runners import definitive as D
from .runners import positions as P
from .runners import vault_exec as VX

PORT = int(os.getenv("RH_TERMINAL_PORT", "8799"))
MAX_EVENTS = 200

_lock = threading.Lock()
STATE: dict = {
    "vault": None, "wallet": None, "halted": False, "book": {}, "events": [],
    "marks": 0, "mark_ms": 0.0, "last_error": None, "started_at": time.time(),
    "auto_exit": True,
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
                with _lock:
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
    _event("exit", "sold %.0f%% — %s (%s)"
           % (fraction * 100, why, json.dumps(out)[:60]), token)
    return {"ok": True, "order": out}


# --- snapshot ----------------------------------------------------------------

def snapshot() -> dict:
    with _lock:
        rows = []
        book_cost = book_value = 0.0
        for token, entry in STATE["book"].items():
            pos = entry["position"]
            mark = pos.mark_usd
            rows.append({
                "token": token,
                "symbol": entry.get("symbol") or "",
                "held": entry.get("held"),
                "cost": pos.cost_usd,
                "mark": mark,
                "pnl_pct": (100 * (mark / pos.cost_usd - 1))
                           if (mark is not None and pos.cost_usd) else None,
                "peak": pos.peak_usd,
                "age_s": time.time() - pos.opened_at,
                "fails": pos.fails,
                "closed": pos.closed,
                "signal": entry.get("signal") or "",
                "basis_known": entry.get("basis_known", True),
            })
            if not pos.closed:
                book_cost += pos.cost_usd
                book_value += mark or 0.0
        rows.sort(key=lambda r: (r["closed"], -(r["mark"] or 0)))
        return {
            "vault": STATE["vault"], "halted": STATE["halted"],
            "auto_exit": STATE["auto_exit"], "marks": STATE["marks"],
            "mark_ms": STATE["mark_ms"], "error": STATE["last_error"],
            "uptime_s": time.time() - STATE["started_at"],
            "rows": rows, "book_cost": book_cost, "book_value": book_value,
            "book_pnl_pct": (100 * (book_value / book_cost - 1))
                            if book_cost else None,
            "events": STATE["events"][:60],
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
        if self.path.startswith("/api/state"):
            return self._json(snapshot())
        body = PAGE.encode()
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


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>CAMBRIAN — live</title>
<link rel=icon href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2032%2032%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M5.5%2010.5%20L13%2016%20L5.5%2021.5%22%20stroke%3D%22%23E6E9EF%22%20stroke-width%3D%222.8%22/%3E%3Cpath%20d%3D%22M15%206%20L25.5%2016%20L15%2026%22%20stroke%3D%22%238A7BFF%22%20stroke-width%3D%223.4%22/%3E%3C/svg%3E">
<style>__FONTS__</style>
<style>__TOKENS__</style>
<style>
.tag.live{background:var(--up)}
.tag.halted{background:var(--down)}
.controls{margin-left:auto;display:flex;gap:8px;align-items:center}
button{font:600 11px/1 var(--font-ui);letter-spacing:.04em;text-transform:uppercase;
 color:var(--text);background:var(--surface-raised);border:1px solid var(--border-strong);
 border-radius:var(--r-md);padding:8px 12px;cursor:pointer}
button:hover{border-color:var(--focus);color:var(--focus)}
button.danger{border-color:color-mix(in oklab,var(--down) 55%,var(--border-strong))}
button.danger:hover{background:color-mix(in oklab,var(--down) 14%,transparent);
 border-color:var(--down);color:var(--down)}
button.stop{border-color:color-mix(in oklab,var(--pending) 55%,var(--border-strong))}
button.stop:hover{background:color-mix(in oklab,var(--pending) 14%,transparent);
 border-color:var(--pending);color:var(--pending)}
button.stop[data-on="1"]{background:var(--pending);color:var(--bg);border-color:var(--pending)}
button.mini{padding:3px 8px;font-size:10px}
.banner{padding:8px 16px;background:color-mix(in oklab,var(--pending) 16%,transparent);
 border-bottom:1px solid var(--border);color:var(--pending);font-size:12px}
.ev{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:5px 16px;
 border-bottom:1px solid var(--border);font-size:12px}
.ev .k{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-faint)}
.ev.exit .k{color:var(--up)}.ev.fail .k{color:var(--down)}.ev.halt .k{color:var(--pending)}
.empty{padding:24px 16px;color:var(--text-faint)}
</style></head><body>
<header>
  <div class=logo>
    <svg width=22 height=22 viewBox="0 0 32 32" fill=none aria-hidden=true>
      <path d="M5.5 10.5 L13 16 L5.5 21.5" stroke="var(--text)" stroke-width=2.8/>
      <path d="M15 6 L25.5 16 L15 26" stroke="var(--agent)" stroke-width=3.4/></svg>
    CAMBRIAN</div>
  <span class="tag live" id=mode>LIVE</span>
  <div class=stat><b id=bookv>$0.00</b><span>book value</span></div>
  <div class=stat><b id=bookp>—</b><span>p&amp;l</span></div>
  <div class=stat><b id=rate>—</b><span>mark latency</span></div>
  <div class=controls>
    <button class=stop id=stop data-on="0">Stop automation</button>
    <button class=danger id=closeall>Close all</button>
  </div>
</header>
<div class=banner id=banner style=display:none></div>
<div class=grid>
  <section>
    <h2>Positions</h2>
    <table><thead><tr><th>Token</th><th>Held</th><th>Cost</th><th>Mark</th>
      <th>P&amp;L</th><th>Peak</th><th>Age</th><th>Signal</th><th></th></tr></thead>
      <tbody id=rows></tbody></table>
    <div class=empty id=none>Nothing held. Buy with <span class=mono>trade-vault</span>
      and it appears here within 30 seconds.</div>
  </section>
  <section style=border-right:none>
    <h2>Activity</h2><div id=events></div>
  </section>
</div>
<script>
const $ = s => document.querySelector(s);
const fmtAge = s => s < 60 ? Math.floor(s)+'s'
  : s < 3600 ? Math.floor(s/60)+'m'+String(Math.floor(s%60)).padStart(2,'0')+'s'
  : Math.floor(s/3600)+'h'+String(Math.floor(s%3600/60)).padStart(2,'0')+'m';
const usd = v => v==null ? '—' : '$'+v.toFixed(2);
const pct = v => v==null ? '—' : (v>=0?'+':'')+v.toFixed(1)+'%';
const cls = v => v==null ? 'dim' : v>=0 ? 'up' : 'down';

async function post(path, body){
  const r = await fetch(path,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body||{})});
  return r.json();
}

function render(s){
  $('#bookv').textContent = usd(s.book_value);
  const p = $('#bookp'); p.textContent = pct(s.book_pnl_pct);
  p.className = cls(s.book_pnl_pct);
  $('#rate').textContent = s.mark_ms ? Math.round(s.mark_ms)+'ms' : '—';
  const mode = $('#mode');
  mode.textContent = s.halted ? 'HALTED' : 'LIVE';
  mode.className = 'tag ' + (s.halted ? 'halted' : 'live');
  const stop = $('#stop');
  stop.dataset.on = s.halted ? '1' : '0';
  stop.textContent = s.halted ? 'Resume automation' : 'Stop automation';
  const b = $('#banner');
  if (s.error){ b.style.display='block'; b.textContent = 'engine: '+s.error; }
  else if (s.halted){ b.style.display='block';
    b.textContent = 'Automation halted — stops and take-profits will NOT fire. '
      + 'Positions are still marked, and Close still works.'; }
  else b.style.display='none';

  $('#none').style.display = s.rows.length ? 'none' : 'block';
  $('#rows').innerHTML = s.rows.map(r => {
    const short = r.token.slice(0,6)+'…'+r.token.slice(-4);
    return '<tr'+(r.closed?' class=faint':'')+'>'
      + '<td><a href="https://dexscreener.com/robinhood/'+r.token+'" target=_blank '
      + 'class=mono>'+(r.symbol||short)+'</a>'
      + (r.basis_known?'':' <span class=faint title="no cost basis reported — '
        + 'percentages measure from adoption, not entry">~</span>')+'</td>'
      + '<td class=mono>'+(r.held==null?'—':Number(r.held).toLocaleString(undefined,
          {maximumFractionDigits:2}))+'</td>'
      + '<td class=mono>'+usd(r.cost)+'</td>'
      + '<td class=mono>'+usd(r.mark)+'</td>'
      + '<td class="mono '+cls(r.pnl_pct)+'">'+pct(r.pnl_pct)+'</td>'
      + '<td class="mono dim">'+usd(r.peak)+'</td>'
      + '<td class="mono dim" data-age="'+r.age_s+'">'+fmtAge(r.age_s)+'</td>'
      + '<td class=dim>'+(r.signal||(r.fails?('unsellable x'+r.fails):''))+'</td>'
      + '<td>'+(r.closed?'':'<button class="mini danger" data-close="'+r.token+'">'
        + 'Close</button>')+'</td></tr>';
  }).join('');

  $('#events').innerHTML = s.events.map(e =>
    '<div class="ev '+e.kind+'"><span class=k>'+e.kind+'</span>'
    + '<span>'+e.text.replace(/</g,'&lt;')+'</span></div>').join('')
    || '<div class=empty>No activity yet.</div>';
}

document.addEventListener('click', async ev => {
  const stop = ev.target.closest('#stop');
  if (stop){ await post('/api/halt',{halted: stop.dataset.on !== '1'}); return tick(); }
  const all = ev.target.closest('#closeall');
  if (all){
    if (!confirm('Sell EVERY open position at market, right now?')) return;
    all.disabled = true; await post('/api/close-all'); all.disabled = false;
    return tick();
  }
  const one = ev.target.closest('[data-close]');
  if (one){
    if (!confirm('Sell 100% of this position at market?')) return;
    one.disabled = true; await post('/api/close',{token: one.dataset.close, pct: 100});
    one.disabled = false; return tick();
  }
});

async function tick(){
  try { render(await (await fetch('/api/state')).json()); } catch(e){}
}
setInterval(tick, 1000);
setInterval(() => document.querySelectorAll('[data-age]').forEach(td => {
  const v = Number(td.dataset.age) + 1; td.dataset.age = v; td.textContent = fmtAge(v);
}), 1000);
tick();
</script></body></html>"""

PAGE = PAGE.replace("__FONTS__", ui_css.FONT_CSS).replace("__TOKENS__", ui_css.TOKENS_CSS)
