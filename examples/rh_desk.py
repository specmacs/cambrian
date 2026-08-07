"""RH AGENT TRADING DESK — a professional automated-trading dashboard for Robinhood
Chain, in your browser.

Specialist agents generate signals; a confluence engine + LLM PM decide; a PAPER
trading engine opens/marks/closes positions from real live prices (Flash quotes),
so the desk shows LIVE trades, holdings, and P&L with zero risk and no wallet. When
you're ready, a wallet (Privy) + live execution replaces the paper engine.

RUN:  pip install requests
      python rh_desk.py inf_YOURKEY        (Surplus key -> the PM; omit = confluence only)
Then open  http://localhost:8787 . Ctrl+C to stop. Env: RH_PORT, RH_SIZE (USD/trade),
RH_STOP, RH_TP, RH_MODEL, RH_INTERVAL, RH_BLOCKS.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
LLM_BASE = os.getenv("RH_LLM_BASE", "https://api.surplusintelligence.ai/min30/v1")
LLM_MODEL = os.getenv("RH_LLM_MODEL", "claude-opus-4.7")
LLM_KEY = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RH_LLM_KEY", "")).strip()
PORT = int(os.getenv("RH_PORT", "8787"))
INTERVAL = int(os.getenv("RH_INTERVAL", "20"))
BLOCKS = int(os.getenv("RH_BLOCKS", "6000"))
ENRICH = int(os.getenv("RH_ENRICH", "14"))
SIZE_USD = float(os.getenv("RH_SIZE", "50"))         # paper size per trade
STOP_PCT = float(os.getenv("RH_STOP", "0.30"))
TP_MULT = float(os.getenv("RH_TP", "2.0"))
MAX_POS = int(os.getenv("RH_MAX_POS", "8"))
WETH_USD = 3000.0
MIN_LIQ = 5000.0
MIN_GROSS = 400.0
SNIPE_BLOCKS = 3

V4_PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951".lower()
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa".lower()
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NAT = "0x0000000000000000000000000000000000000000"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
PC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
V3S = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4S = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TR = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EXT = "0x1e2eaeaf"
SLOT = 6
FLASH_BASE = "https://flash.definitive.fi/v1"
FLASH_KEY = "dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b"

PAD_BY_HOOK = {"0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544": "bankr"}
PAD_BY_DEPLOYER = {"0x0000ffffbe8efe702c8703ae3477ff5de3d319c0": "pons",
                   "0x58daec3116aae6d93017baaea7749052e8a04fa7": "pools-trade"}
PAD_BY_SUFFIX = {"ba3": "bankr", "777": "flap"}

_M = (1 << 64) - 1
_RC = [0x1, 0x8082, 0x800000000000808A, 0x8000000080008000, 0x808B, 0x80000001,
       0x8000000080008081, 0x8000000000008009, 0x8A, 0x88, 0x80008009, 0x8000000A,
       0x8000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x80000001, 0x8000000080008008]


def _rol(a, b):
    b %= 64
    return ((a << b) | (a >> (64 - b))) & _M


def _f(s):
    L = [[s[x + 5 * y] for y in range(5)] for x in range(5)]
    for rnd in range(24):
        c = [L[x][0] ^ L[x][1] ^ L[x][2] ^ L[x][3] ^ L[x][4] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                L[x][y] ^= d[x]
        x, y = 1, 0
        cur = L[x][y]
        for t in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            cur, L[x][y] = L[x][y], _rol(cur, (t + 1) * (t + 2) // 2)
        for yy in range(5):
            row = [L[xx][yy] for xx in range(5)]
            for xx in range(5):
                L[xx][yy] = row[xx] ^ ((~row[(xx + 1) % 5]) & row[(xx + 2) % 5])
        L[0][0] ^= _RC[rnd]
    return [L[x][y] for y in range(5) for x in range(5)]


def kec(data):
    s = [0] * 25
    m = bytearray(data)
    m.append(0x01)
    while len(m) % 136:
        m.append(0)
    m[-1] ^= 0x80
    for off in range(0, len(m), 136):
        for i in range(17):
            s[i] ^= int.from_bytes(m[off + i * 8:off + i * 8 + 8], "little")
        s = _f(s)
    return b"".join(x.to_bytes(8, "little") for x in s)[:32]


def rpc(method, params):
    d = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=40).json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def logs(topics, frm, addr=None):
    q = {"fromBlock": frm, "toBlock": "latest", "topics": topics}
    if addr:
        q["address"] = addr
    return rpc("eth_getLogs", [q])


def A(w):
    return "0x" + w[-40:]


def sg(w):
    v = int(w, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def wd(data, n):
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def bts(b):
    x = rpc("eth_getBlockByNumber", [b, False])
    return int(x["timestamp"], 16) if x else None


def txto(h):
    try:
        return ((rpc("eth_getTransactionByHash", [h]) or {}).get("to") or "").lower()
    except Exception:
        return ""


def analyze(addr, topics, frm, w0, cblk, inv=False):
    gross = net = snip = 0.0
    buys = sells = 0
    for lg in logs(topics, frm, addr):
        w = wd(lg["data"], 4 if len(topics) > 1 else 5)
        leg = sg(w[0]) if w0 else sg(w[1])
        if inv:
            leg = -leg
        usd = leg / 1e18 * WETH_USD
        gross += abs(usd)
        net += usd
        if usd > 0:
            buys += 1
            b = lg.get("blockNumber")
            b = int(b, 16) if isinstance(b, str) else (b or 0)
            if cblk and b and b <= cblk + SNIPE_BLOCKS:
                snip += usd
        elif usd < 0:
            sells += 1
    bv = (gross + net) / 2
    return {"gross": gross, "net": net, "buys": buys, "sells": sells,
            "snipe": min(max((snip / bv) if bv > 1e-9 else 0.0, 0.0), 1.0)}


def fanout(tok, frm, pool):
    try:
        lg = logs([TR], frm, tok)
    except Exception:
        return None
    pl, rec = pool.lower(), set()
    for x in lg:
        tp = x.get("topics", [])
        if len(tp) < 3:
            continue
        a, b = A(tp[1]).lower(), A(tp[2]).lower()
        if a in (pl, NAT) or b in (pl, NAT):
            continue
        rec.add(b)
    return len(rec)


def v4liq(pid, q0):
    p = bytes.fromhex(pid[2:])
    base = int.from_bytes(kec(p.rjust(32, b"\x00") + SLOT.to_bytes(32, "big")), "big")

    def ld(sl):
        return int(rpc("eth_call", [{"to": V4_PM, "data": EXT + sl.to_bytes(32, "big").hex()}, "latest"]), 16)
    liq = ld(base + 3) & ((1 << 128) - 1)
    sp = ld(base) & ((1 << 160) - 1)
    if not liq or not sp:
        return None
    q = 1 << 96
    res = (liq * q / sp) if q0 else (liq * sp / q)
    return res / 1e18 * WETH_USD * 2


def fquote(target, contra, qty, side):
    body = {"targetChain": "robinhood", "contraChain": "robinhood",
            "targetAsset": target, "contraAsset": contra, "side": side,
            "qty": str(qty), "orderType": "market", "maxSlippage": "0.2", "quickTrade": True}
    return requests.post(FLASH_BASE + "/quote", json=body, timeout=40,
                         headers={"content-type": "application/json",
                                  "x-definitive-api-key": FLASH_KEY}).json()


def buy_quote(token, usd):
    """Returns (tokens_out, usd_in) for spending ~usd, or (None,None)."""
    try:
        b = fquote(token, WETH, round(usd / WETH_USD, 6), "buy")
        if "error" in b or not b.get("to"):
            return None, None
        return float(b["to"]["amount"]), float(b["from"].get("notional") or 0)
    except Exception:
        return None, None


def sell_value(token, tokens):
    """Current USD you'd get selling `tokens` back — mark-to-market. None if can't."""
    try:
        s = fquote(token, WETH, tokens, "sell")
        if "error" in s or not s.get("to"):
            return None
        return float(s["to"].get("notional") or 0)
    except Exception:
        return None


def honeypot_ok(token):
    tks, usd_in = buy_quote(token, 30)
    if not tks or not usd_in:
        return False
    out = sell_value(token, tks)
    if out is None:
        return False
    return (out / usd_in) >= 0.75 if usd_in > 0 else False


def pad_and_verify(tok, hook, txh):
    h = (hook or "").lower()
    if h in PAD_BY_HOOK:
        return PAD_BY_HOOK[h], True
    dep = txto(txh) if txh else ""
    if dep in PAD_BY_DEPLOYER:
        return PAD_BY_DEPLOYER[dep], True
    for suf, name in PAD_BY_SUFFIX.items():
        if tok.lower().endswith(suf):
            return name + "?", False
    if h and h != NAT:
        return "hook:" + h[2:8], False
    return "?", False


def ag_flow(net, gross):
    if not gross or gross <= 0:
        return 0.0
    if net is None or net <= 0:
        return 0.0
    return round(min(net / 5000.0, 1.0), 2)


def ag_sniper(sh):
    return round(max(0.0, 1.0 - min(max(sh, 0.0), 1.0)), 2)


def ag_farm(fo):
    return 0.4 if fo is None else round(max(0.0, 1.0 - fo / 50.0), 2)


def ag_mom(b, s):
    t = (b or 0) + (s or 0)
    return 0.0 if not t else round(max((b / t - 0.5) * 2, 0.0), 2)


WEIGHTS = {"flow": 0.4, "sniper": 0.22, "farm": 0.22, "mom": 0.16}


def confluence(sig, verified, sellable):
    ws = sum(WEIGHTS.values())
    score = round(sum(sig[k] * WEIGHTS[k] for k in sig) / ws, 2)
    agree = sum(1 for v in sig.values() if v >= 0.5)
    safety_ok = verified or sellable is True
    if not safety_ok:
        return score, agree, "BLOCKED"
    if score >= 0.6 and agree >= 3:
        return score, agree, "STRONG"
    if score >= 0.4 and agree >= 2:
        return score, agree, "watch"
    return score, agree, "weak"


SYS = ("You are the PM of an automated memecoin desk on Robinhood Chain. Analysts scored "
       "a fresh launch and it passed the honeypot gate. Decide the final call. Be strict. "
       'Reply ONLY JSON: {"decision":"buy"|"skip","confidence":0..1,"reason":"<=8 words"}.')


def pm(facts):
    if not LLM_KEY:
        return {"decision": "buy", "confidence": 0, "reason": "confluence (no PM)"}
    body = {"model": LLM_MODEL, "temperature": 0, "max_tokens": 90, "stream": False,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": "\n".join(f"{k}: {v}" for k, v in facts.items())}]}
    try:
        r = requests.post(LLM_BASE.rstrip("/") + "/chat/completions",
                          headers={"Authorization": "Bearer " + LLM_KEY, "Content-Type": "application/json"},
                          json=body, timeout=40)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return {"decision": "skip", "confidence": 0, "reason": "pm error"}
    s = txt.strip()
    if "{" in s and "}" in s:
        s = s[s.index("{"):s.rindex("}") + 1]
    try:
        d = json.loads(s)
    except Exception:
        return {"decision": "skip", "confidence": 0, "reason": "parse error"}
    dec = str(d.get("decision", "skip")).lower()
    return {"decision": "buy" if dec == "buy" else "skip",
            "confidence": d.get("confidence", 0), "reason": str(d.get("reason", ""))[:40]}


# ---- paper trading engine ---------------------------------------------------
STATE = {"block": 0, "updated": "starting...", "err": "", "mode": "PAPER",
         "model": LLM_MODEL if LLM_KEY else "confluence only", "size": SIZE_USD,
         "equity": 0.0, "realized": 0.0, "unrealized": 0.0, "wins": 0, "losses": 0,
         "positions": [], "closed": [], "blotter": [], "scouting": []}
BOOK = {}          # token -> position dict
CLOSED = []
BLOTTER = []
SEEN = set()
REALIZED = [0.0]


def now_hms():
    return time.strftime("%H:%M:%S")


def open_paper(token, pad, agent, ver, reason):
    if token in BOOK or len(BOOK) >= MAX_POS:
        return
    tokens, usd_in = buy_quote(token, SIZE_USD)
    if not tokens or not usd_in or usd_in <= 0:
        return
    entry = usd_in / tokens
    BOOK[token] = {"token": token, "pad": pad, "agent": agent, "ver": ver,
                   "tokens": tokens, "cost": usd_in, "entry": entry,
                   "stop": entry * (1 - STOP_PCT), "tp": entry * TP_MULT,
                   "opened": time.time(), "reason": reason}
    BLOTTER.append({"t": now_hms(), "side": "BUY", "token": token, "pad": pad,
                    "agent": agent, "usd": round(usd_in, 2), "px": entry, "pnl": None})


def close_paper(token, value, why):
    p = BOOK.pop(token, None)
    if not p:
        return
    pnl = value - p["cost"]
    REALIZED[0] += pnl
    CLOSED.append({**p, "exit_value": round(value, 2), "pnl": round(pnl, 2),
                   "why": why, "closed": time.time()})
    BLOTTER.append({"t": now_hms(), "side": "SELL", "token": token, "pad": p["pad"],
                    "agent": p["agent"], "usd": round(value, 2),
                    "px": value / p["tokens"] if p["tokens"] else 0, "pnl": round(pnl, 2)})


def mark_and_exit():
    """Mark every open position to live price; exit on stop/TP."""
    unreal = 0.0
    for token, p in list(BOOK.items()):
        val = sell_value(token, p["tokens"])
        if val is None:
            continue
        px = val / p["tokens"] if p["tokens"] else 0
        p["mark"] = px
        p["value"] = val
        p["upnl"] = val - p["cost"]
        if px <= p["stop"]:
            close_paper(token, val, "stop")
        elif px >= p["tp"]:
            close_paper(token, val, "take-profit")
        else:
            unreal += p["upnl"]
    return unreal


def publish(block):
    wins = sum(1 for c in CLOSED if c["pnl"] > 0)
    losses = sum(1 for c in CLOSED if c["pnl"] <= 0)
    pos = sorted(BOOK.values(), key=lambda p: -(p.get("upnl") or 0))
    STATE.update(
        block=block, updated=now_hms(), realized=round(REALIZED[0], 2),
        unrealized=round(sum(p.get("upnl", 0.0) for p in BOOK.values()), 2),
        equity=round(REALIZED[0] + sum(p.get("upnl", 0.0) for p in BOOK.values()), 2),
        wins=wins, losses=losses,
        positions=[{"token": p["token"], "pad": p["pad"], "agent": p["agent"],
                    "cost": round(p["cost"], 2), "value": round(p.get("value", p["cost"]), 2),
                    "upnl": round(p.get("upnl", 0.0), 2),
                    "entry": p["entry"], "mark": p.get("mark", p["entry"]),
                    "stop": p["stop"], "tp": p["tp"],
                    "age": round((time.time() - p["opened"]) / 60, 1)} for p in pos],
        closed=[{"token": c["token"], "pad": c["pad"], "agent": c["agent"],
                 "cost": round(c["cost"], 2), "exit_value": c["exit_value"],
                 "pnl": c["pnl"], "why": c["why"]} for c in CLOSED[-30:]][::-1],
        blotter=BLOTTER[-40:][::-1])


def scan_and_trade():
    block = int(rpc("eth_blockNumber", []), 16)
    now = bts("latest")
    frm = hex(max(block - BLOCKS, 0))
    hits = []
    for lg in logs([INIT], frm, V4_PM):
        tp, w = lg["topics"], wd(lg["data"], 5)
        c0, c1 = A(tp[2]).lower(), A(tp[3]).lower()
        if not ({WETH, NAT} & {c0, c1}):
            continue
        q0 = c0 in {WETH, NAT}
        tok = A(tp[3]) if q0 else A(tp[2])
        hits.append(("v4", tok, tp[1], A(w[2]), int(lg["blockNumber"], 16), q0, lg.get("transactionHash")))
    for lg in logs([PC], frm, V3_FACTORY):
        tp, data = lg["topics"], lg["data"][2:]
        t0, t1 = A(tp[1]).lower(), A(tp[2]).lower()
        if WETH not in (t0, t1):
            continue
        w0 = t0 == WETH
        tok = A(tp[2]) if w0 else A(tp[1])
        hits.append(("v3", tok, A(data[64:128]), "-", int(lg["blockNumber"], 16), w0, lg.get("transactionHash")))
    hits.sort(key=lambda h: -h[4])
    scouting = []
    for ver, tok, pool, hook, blk, q0, txh in hits[:ENRICH]:
        if tok in SEEN or tok in BOOK:
            continue
        life = hex(max(blk - 1, 0))
        try:
            if ver == "v4":
                m = analyze(V4_PM, [V4S, pool], life, q0, blk, inv=True)
                liq = v4liq(pool, q0)
            else:
                m = analyze(pool, [V3S], life, q0, blk)
                bal = int(rpc("eth_call", [{"to": WETH, "data": "0x70a08231" + "0" * 24 + pool[2:]}, "latest"]), 16)
                liq = bal / 1e18 * WETH_USD * 2
        except Exception:
            continue
        if liq is None or liq < MIN_LIQ or m["gross"] < MIN_GROSS:
            continue
        pad, verified = pad_and_verify(tok, hook, txh)
        sellable = True if verified else honeypot_ok(tok)
        fo = fanout(tok, life, pool)
        sig = {"flow": ag_flow(m["net"], m["gross"]), "sniper": ag_sniper(m["snipe"]),
               "farm": ag_farm(fo), "mom": ag_mom(m["buys"], m["sells"])}
        conf, agree, tier = confluence(sig, verified, sellable)
        scouting.append({"pad": pad, "token": tok, "conf": conf, "tier": tier,
                         "net": round(m["net"]), "liq": round(liq)})
        if tier == "STRONG":
            v = pm({"pad": pad, "net_flow_usd": round(m["net"]), "liquidity_usd": round(liq),
                    "sniper_share": round(m["snipe"], 2), "fanout": fo,
                    "buys": m["buys"], "sells": m["sells"], "confluence": conf})
            SEEN.add(tok)
            if v["decision"] == "buy":
                open_paper(tok, pad, "sniper", ver, v["reason"] or f"conf {conf}")
    STATE["scouting"] = scouting[:12]
    unreal = mark_and_exit()
    publish(block)


def worker():
    while True:
        try:
            scan_and_trade()
        except Exception as e:
            STATE.update(err=f"scan error: {e}", updated=now_hms())
        time.sleep(INTERVAL)


PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>RH Agent Desk</title>
<style>
:root{--bg:#080b12;--pnl:#0d1320;--pnl2:#111a2b;--bd:#1a2436;--bd2:#131b2c;--tx:#d2dcec;
--mut:#7387a8;--dim:#455168;--cy:#57d0ff;--gr:#2fe3a3;--rd:#ff5d73;--am:#ffc24b}
*{box-sizing:border-box}body{background:var(--bg);color:var(--tx);margin:0;
font:13px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--pnl);
border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5}
.logo{font-weight:700;font-size:15px;letter-spacing:.16em;color:var(--cy)}.logo b{color:var(--tx)}
.pill{font-size:10.5px;font-weight:700;letter-spacing:.08em;padding:3px 9px;border-radius:20px}
.paper{background:rgba(87,208,255,.14);color:var(--cy);border:1px solid rgba(87,208,255,.35)}
.spacer{flex:1}.chip{font-size:11.5px;color:var(--mut)}.chip b{color:var(--tx)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--gr);display:inline-block;animation:p 2s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(47,227,163,.5)}70%{box-shadow:0 0 0 7px rgba(47,227,163,0)}100%{box-shadow:0 0 0 0 rgba(47,227,163,0)}}
.connect{background:#182238;border:1px solid var(--bd);color:var(--tx);padding:7px 14px;
border-radius:8px;font-weight:600;font-size:12px;cursor:pointer}.connect:hover{border-color:var(--cy);color:var(--cy)}
.kpis{display:flex;gap:12px;padding:14px 20px}
.kpi{flex:1;background:var(--pnl);border:1px solid var(--bd2);border-radius:12px;padding:12px 16px}
.kpi .n{font-size:26px;font-weight:700}.kpi .l{font-size:10.5px;letter-spacing:.1em;color:var(--mut);text-transform:uppercase}
.pos-eq .n{color:var(--gr)}.neg-eq .n{color:var(--rd)}
.grid{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;padding:0 20px 20px}
.card{background:var(--pnl);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
.card h3{margin:0;padding:10px 16px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
color:var(--mut);border-bottom:1px solid var(--bd2);background:var(--pnl2);display:flex;justify-content:space-between}
.card h3 span{color:var(--dim)}
table{width:100%;border-collapse:collapse}
th{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);text-align:right;padding:8px 12px;border-bottom:1px solid var(--bd2)}
th:first-child,td:first-child{text-align:left}td{padding:9px 12px;border-bottom:1px solid var(--bd2);text-align:right}
tr:hover{background:#0f1728}
.tag{font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:5px;background:rgba(115,135,168,.14);color:var(--mut)}
.tag.v{background:rgba(87,208,255,.14);color:var(--cy)}
.tok{color:var(--mut);text-decoration:none}.tok:hover{color:var(--cy)}
.pos{color:var(--gr)}.neg{color:var(--rd)}.dim{color:var(--dim)}
.side-BUY{color:var(--cy);font-weight:700}.side-SELL{color:var(--am);font-weight:700}
.empty{color:var(--dim);text-align:center;padding:22px}
.full{grid-column:1/3}
.sc{display:inline-block;font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px}
.sc-STRONG{background:rgba(47,227,163,.16);color:var(--gr)}.sc-watch{background:rgba(255,194,75,.14);color:var(--am)}
.sc-weak{color:var(--dim)}.sc-BLOCKED{background:rgba(255,93,115,.14);color:var(--rd)}
</style></head><body>
<header><span class=logo>&#9670; RH AGENT <b>DESK</b></span>
<span class="pill paper" id=mode>PAPER</span><span class=spacer></span>
<span class=chip id=meta></span><span class=dot></span>
<button class=connect onclick="alert('Wallet connect (Privy) wires in for LIVE mode. Paper desk needs no wallet.')">Connect Wallet</button>
</header>
<div class=kpis>
 <div class="kpi" id=kEq><div class=n id=eq>$0</div><div class=l>Equity P&amp;L</div></div>
 <div class=kpi><div class=n id=real>$0</div><div class=l>Realized</div></div>
 <div class=kpi><div class=n id=unreal>$0</div><div class=l>Unrealized</div></div>
 <div class=kpi><div class=n id=open>0</div><div class=l>Open positions</div></div>
 <div class=kpi><div class=n id=wr>-</div><div class=l>Win rate</div></div>
</div>
<div class=grid>
 <div class=card><h3>Open positions &middot; holdings <span id=posn></span></h3>
  <table><thead><tr><th>Token</th><th>Pad</th><th>Agent</th><th>Size</th><th>Value</th><th>uPnL</th><th>Stop</th><th>TP</th><th>Age</th></tr></thead>
  <tbody id=positions></tbody></table></div>
 <div class=card><h3>Blotter &middot; live trades</h3>
  <table><thead><tr><th>Time</th><th>Side</th><th>Token</th><th>Agent</th><th>USD</th><th>PnL</th></tr></thead>
  <tbody id=blotter></tbody></table></div>
 <div class=card><h3>Closed trades</h3>
  <table><thead><tr><th>Token</th><th>Pad</th><th>Cost</th><th>Exit</th><th>PnL</th><th>Why</th></tr></thead>
  <tbody id=closed></tbody></table></div>
 <div class=card><h3>Scouting &middot; agent A (sniper) <span>candidates</span></h3>
  <table><thead><tr><th>Token</th><th>Pad</th><th>Net</th><th>Liq</th><th>Conf</th></tr></thead>
  <tbody id=scouting></tbody></table></div>
</div>
<script>
const EXP="https://robinhoodchain.blockscout.com/address/";
function d$(n){const s=n>=0?'':'-',a=Math.abs(n);return s+'$'+(a>=1000?(a/1000).toFixed(1)+'k':a.toFixed(2))}
function cls(n){return n>0?'pos':n<0?'neg':'dim'}
function tk(t){return `<a class="tok mono" href="${EXP}${t}" target=_blank title="${t}">${t.slice(0,8)}&hellip;${t.slice(-4)}</a>`}
function pad(p,v){return `<span class="tag ${v?'v':''}">${p}</span>`}
async function tick(){let d;try{d=await(await fetch('/data')).json()}catch(e){return}
 document.getElementById('meta').innerHTML=`block <b>${d.block}</b> &middot; PM <b>${d.model}</b> &middot; $${d.size}/trade &middot; ${d.updated}`+(d.err?` <span style="color:var(--rd)">${d.err}</span>`:'');
 document.getElementById('eq').textContent=d$(d.equity);
 document.getElementById('kEq').className='kpi '+(d.equity>=0?'pos-eq':'neg-eq');
 document.getElementById('real').innerHTML=`<span class=${cls(d.realized)}>${d$(d.realized)}</span>`;
 document.getElementById('unreal').innerHTML=`<span class=${cls(d.unrealized)}>${d$(d.unrealized)}</span>`;
 document.getElementById('open').textContent=d.positions.length;
 const tot=d.wins+d.losses;document.getElementById('wr').textContent=tot?Math.round(100*d.wins/tot)+'%':'-';
 document.getElementById('posn').textContent=d.positions.length;
 document.getElementById('positions').innerHTML=d.positions.length?d.positions.map(p=>
  `<tr><td>${tk(p.token)}</td><td>${pad(p.pad,true)}</td><td class=dim>${p.agent}</td><td class=mono>${d$(p.cost)}</td>`+
  `<td class=mono>${d$(p.value)}</td><td class="mono ${cls(p.upnl)}">${d$(p.upnl)}</td>`+
  `<td class="mono dim">${p.stop.toPrecision(3)}</td><td class="mono dim">${p.tp.toPrecision(3)}</td><td class=mono>${p.age}m</td></tr>`).join(''):
  '<tr><td colspan=9 class=empty>no open positions &mdash; waiting for a STRONG signal</td></tr>';
 document.getElementById('blotter').innerHTML=d.blotter.length?d.blotter.map(b=>
  `<tr><td class="mono dim">${b.t}</td><td class=side-${b.side}>${b.side}</td><td>${tk(b.token)}</td><td class=dim>${b.agent}</td>`+
  `<td class=mono>${d$(b.usd)}</td><td class="mono ${b.pnl==null?'dim':cls(b.pnl)}">${b.pnl==null?'&mdash;':d$(b.pnl)}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>no trades yet</td></tr>';
 document.getElementById('closed').innerHTML=d.closed.length?d.closed.map(c=>
  `<tr><td>${tk(c.token)}</td><td>${pad(c.pad,true)}</td><td class=mono>${d$(c.cost)}</td><td class=mono>${d$(c.exit_value)}</td>`+
  `<td class="mono ${cls(c.pnl)}">${d$(c.pnl)}</td><td class=dim>${c.why}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>none closed yet</td></tr>';
 document.getElementById('scouting').innerHTML=d.scouting.length?d.scouting.map(s=>
  `<tr><td>${tk(s.token)}</td><td>${pad(s.pad,false)}</td><td class="mono ${cls(s.net)}">${d$(s.net)}</td>`+
  `<td class=mono>${d$(s.liq)}</td><td><span class="sc sc-${s.tier}">${s.conf} ${s.tier}</span></td></tr>`).join(''):
  '<tr><td colspan=5 class=empty>scanning&hellip;</td></tr>';
}
tick();setInterval(tick,4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/data"):
            body = json.dumps(STATE).encode()
            ct = "application/json"
        else:
            body = PAGE.encode()
            ct = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=worker, daemon=True).start()
    print(f"\n  RH AGENT DESK (paper) running.  ->  open  http://localhost:{PORT}\n"
          f"  PM: {STATE['model']}   size ${SIZE_USD}/trade   stop -{STOP_PCT:.0%}  tp {TP_MULT}x   (Ctrl+C to stop)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
