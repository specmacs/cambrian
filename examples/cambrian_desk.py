"""CAMBRIAN — an agent trading desk for Robinhood Chain.

Pro-grade web terminal: agents scan every fresh launch, a confluence engine +
LLM PM decide, and a PAPER engine opens/marks/closes positions at real Flash
prices — live blotter, holdings, PnL. Tickers resolved on-chain; every token
links to Dexscreener / its launchpad / Blockscout. Wallet importer feeds the
wallet-tracker agent. LIVE mode (Privy wallet) replaces the paper engine later.

RUN:  pip install requests
      python cambrian_desk.py inf_YOURKEY     (Surplus key -> PM; omit = confluence only)
Open  http://localhost:8787 . Ctrl+C stops. Env: RH_PORT, RH_SIZE, RH_STOP, RH_TP,
RH_LLM_MODEL, RH_INTERVAL, RH_BLOCKS. Wallets persist to ~/cambrian_wallets.json.
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
SIZE_USD = float(os.getenv("RH_SIZE", "50"))
STOP_PCT = float(os.getenv("RH_STOP", "0.30"))
TP_MULT = float(os.getenv("RH_TP", "2.0"))
MAX_POS = int(os.getenv("RH_MAX_POS", "8"))
WALLETS_FILE = os.getenv("RH_WALLETS", os.path.expanduser("~/cambrian_wallets.json"))
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
# Launchpad token-page URL templates ({} = token address). Best-guess formats —
# edit if a pad uses a different path; UI falls back to Dexscreener regardless.
PAD_URLS = {"pools-trade": "https://pools.trade/token/{}",
            "flap": "https://flap.sh/token/{}",
            "bankr": "https://bankr.bot/token/{}",
            "pons": "https://pons.fun/token/{}"}

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


SYM_CACHE = {}


def symbol(token):
    """Token ticker via on-chain symbol() — cached."""
    t = token.lower()
    if t in SYM_CACHE:
        return SYM_CACHE[t]
    try:
        raw = rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:                      # ABI string
            ln = int.from_bytes(b[32:64], "big")
            s = b[64:64 + ln].decode("utf-8", "ignore").strip()
        else:                                 # bytes32 style
            s = b.rstrip(b"\x00").decode("utf-8", "ignore").strip()
        s = "".join(ch for ch in s if ch.isprintable())[:12] or "?"
    except Exception:
        s = "?"
    SYM_CACHE[t] = s
    return s


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
    try:
        b = fquote(token, WETH, round(usd / WETH_USD, 6), "buy")
        if "error" in b or not b.get("to"):
            return None, None
        return float(b["to"]["amount"]), float(b["from"].get("notional") or 0)
    except Exception:
        return None, None


def sell_value(token, tokens):
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
    if not gross or gross <= 0 or net is None or net <= 0:
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
    if not (verified or sellable is True):
        return score, agree, "BLOCKED"
    if score >= 0.6 and agree >= 3:
        return score, agree, "STRONG"
    if score >= 0.4 and agree >= 2:
        return score, agree, "watch"
    return score, agree, "weak"


SYS = ("You are the PM of an automated memecoin desk on Robinhood Chain. Analysts "
       "scored a fresh launch that passed the honeypot gate. Decide. Be strict. "
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


# ---- wallets (agent C feed) -------------------------------------------------
def load_wallets():
    try:
        with open(WALLETS_FILE) as fh:
            return [w for w in json.load(fh) if isinstance(w, str)]
    except Exception:
        return []


def save_wallets(ws):
    try:
        with open(WALLETS_FILE, "w") as fh:
            json.dump(sorted(set(ws)), fh, indent=0)
    except Exception:
        pass


WALLETS = load_wallets()

# ---- paper book -------------------------------------------------------------
STATE = {"block": 0, "updated": "starting...", "err": "", "mode": "PAPER",
         "model": LLM_MODEL if LLM_KEY else "confluence only", "size": SIZE_USD,
         "equity": 0.0, "realized": 0.0, "unrealized": 0.0, "wins": 0, "losses": 0,
         "positions": [], "closed": [], "blotter": [], "scouting": [],
         "wallets": len(WALLETS), "agents": [
             {"id": "sniper", "name": "A · Sniper", "desc": "fresh launches", "on": True},
             {"id": "volume", "name": "B · Volume", "desc": "all-RH momentum", "on": False},
             {"id": "wallets", "name": "C · Wallets", "desc": "smart-money copy", "on": False},
             {"id": "intel", "name": "D · Intel", "desc": "X / narrative", "on": False}]}
BOOK = {}
CLOSED = []
BLOTTER = []
SEEN = set()
REALIZED = [0.0]


def now_hms():
    return time.strftime("%H:%M:%S")


def open_paper(token, symb, pad, agent, reason):
    if token in BOOK or len(BOOK) >= MAX_POS:
        return
    tokens, usd_in = buy_quote(token, SIZE_USD)
    if not tokens or not usd_in or usd_in <= 0:
        return
    entry = usd_in / tokens
    BOOK[token] = {"token": token, "sym": symb, "pad": pad, "agent": agent,
                   "tokens": tokens, "cost": usd_in, "entry": entry,
                   "stop": entry * (1 - STOP_PCT), "tp": entry * TP_MULT,
                   "opened": time.time(), "reason": reason}
    BLOTTER.append({"t": now_hms(), "side": "BUY", "token": token, "sym": symb,
                    "pad": pad, "agent": agent, "usd": round(usd_in, 2), "pnl": None})


def close_paper(token, value, why):
    p = BOOK.pop(token, None)
    if not p:
        return
    pnl = value - p["cost"]
    REALIZED[0] += pnl
    CLOSED.append({**p, "exit_value": round(value, 2), "pnl": round(pnl, 2),
                   "why": why, "closed": time.time()})
    BLOTTER.append({"t": now_hms(), "side": "SELL", "token": token, "sym": p["sym"],
                    "pad": p["pad"], "agent": p["agent"], "usd": round(value, 2),
                    "pnl": round(pnl, 2)})


def mark_and_exit():
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


def publish(block):
    wins = sum(1 for c in CLOSED if c["pnl"] > 0)
    losses = sum(1 for c in CLOSED if c["pnl"] <= 0)
    pos = sorted(BOOK.values(), key=lambda p: -(p.get("upnl") or 0))
    STATE.update(
        block=block, updated=now_hms(), realized=round(REALIZED[0], 2),
        unrealized=round(sum(p.get("upnl", 0.0) for p in BOOK.values()), 2),
        equity=round(REALIZED[0] + sum(p.get("upnl", 0.0) for p in BOOK.values()), 2),
        wins=wins, losses=losses, wallets=len(WALLETS),
        positions=[{"token": p["token"], "sym": p["sym"], "pad": p["pad"],
                    "agent": p["agent"], "cost": round(p["cost"], 2),
                    "value": round(p.get("value", p["cost"]), 2),
                    "upnl": round(p.get("upnl", 0.0), 2),
                    "chg": round(100 * ((p.get("mark", p["entry"]) / p["entry"]) - 1), 1),
                    "age": round((time.time() - p["opened"]) / 60, 1)} for p in pos],
        closed=[{"token": c["token"], "sym": c["sym"], "pad": c["pad"],
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
        if tok in BOOK:
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
        symb = symbol(tok)
        scouting.append({"pad": pad, "verified": verified, "token": tok, "sym": symb,
                         "conf": conf, "tier": tier, "net": round(m["net"]),
                         "liq": round(liq)})
        if tier == "STRONG" and tok not in SEEN:
            v = pm({"ticker": symb, "pad": pad, "net_flow_usd": round(m["net"]),
                    "liquidity_usd": round(liq), "sniper_share": round(m["snipe"], 2),
                    "fanout": fo, "buys": m["buys"], "sells": m["sells"], "confluence": conf})
            SEEN.add(tok)
            if v["decision"] == "buy":
                open_paper(tok, symb, pad, "sniper", v["reason"] or f"conf {conf}")
    STATE["scouting"] = scouting[:14]
    mark_and_exit()
    publish(block)


def worker():
    while True:
        try:
            scan_and_trade()
        except Exception as e:
            STATE.update(err=f"scan error: {e}", updated=now_hms())
        time.sleep(INTERVAL)


PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>CAMBRIAN</title>
<style>
:root{--bg:#07090f;--sb:#0a0d15;--pnl:#0d1119;--pnl2:#10151f;--bd:#181e2c;--bd2:#12172227;
--tx:#dbe3f0;--mut:#6b7c9c;--dim:#3c4763;--ac:#37e0b0;--ac2:#22c896;--cy:#4fc3ff;
--rd:#ff5470;--am:#f5b84c;--grad:linear-gradient(135deg,#37e0b0,#4fc3ff)}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);font:13px/1.45 Inter,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;display:flex;min-height:100vh}
.mono{font-family:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
/* sidebar */
aside{width:198px;background:var(--sb);border-right:1px solid var(--bd);padding:16px 12px;
position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.brand{display:flex;align-items:center;gap:9px;padding:2px 8px 16px}
.gem{width:26px;height:26px;border-radius:8px;background:var(--grad);display:grid;place-items:center;
color:#04241a;font-weight:800;font-size:14px}
.brand b{font-size:15px;letter-spacing:.22em;font-weight:800}
.nav{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:9px;color:var(--mut);
cursor:pointer;font-weight:600;font-size:12.5px;border:1px solid transparent}
.nav:hover{color:var(--tx);background:var(--pnl2)}
.nav.on{color:var(--ac);background:rgba(55,224,176,.08);border-color:rgba(55,224,176,.18)}
.nav .ic{width:16px;text-align:center;opacity:.9}
.agents{margin-top:auto;border-top:1px solid var(--bd);padding-top:12px}
.agents h4{font-size:9.5px;letter-spacing:.14em;color:var(--dim);padding:0 10px 8px;text-transform:uppercase}
.ag{display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:11.5px;color:var(--mut)}
.ag .st{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.ag.on .st{background:var(--ac);box-shadow:0 0 6px rgba(55,224,176,.6)}
.ag.on{color:var(--tx)}.ag small{margin-left:auto;color:var(--dim);font-size:10px}
/* main */
main{flex:1;min-width:0}
topbar{display:flex;align-items:center;gap:16px;padding:12px 22px;border-bottom:1px solid var(--bd);
background:rgba(10,13,21,.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:6}
.pill{font-size:10px;font-weight:800;letter-spacing:.1em;padding:4px 10px;border-radius:6px}
.paper{background:rgba(79,195,255,.12);color:var(--cy);border:1px solid rgba(79,195,255,.3)}
.meta{font-size:11.5px;color:var(--mut)}.meta b{color:var(--tx)}
.sp{flex:1}
.eqbig{font-size:17px;font-weight:800}
.connect{background:var(--grad);color:#04241a;border:0;padding:8px 16px;border-radius:9px;
font-weight:800;font-size:12px;cursor:pointer;letter-spacing:.02em}
.connect:hover{filter:brightness(1.1)}
/* kpis */
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:16px 22px 6px}
.kpi{background:var(--pnl);border:1px solid var(--bd);border-radius:12px;padding:12px 15px}
.kpi .l{font-size:9.5px;letter-spacing:.13em;color:var(--dim);text-transform:uppercase;margin-bottom:4px}
.kpi .n{font-size:21px;font-weight:800}
/* cards */
.view{padding:12px 22px 24px;display:none}.view.on{display:block}
.g2{display:grid;grid-template-columns:1.4fr 1fr;gap:12px}
.card{background:var(--pnl);border:1px solid var(--bd);border-radius:13px;overflow:hidden;margin-bottom:12px}
.card h3{padding:11px 16px;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);
border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px}
.card h3 .cnt{margin-left:auto;color:var(--dim);font-weight:600}
table{width:100%;border-collapse:collapse}
th{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);text-align:right;
padding:8px 13px;border-bottom:1px solid var(--bd2)}
th:first-child,td:first-child{text-align:left}
td{padding:9px 13px;border-bottom:1px solid var(--bd2);text-align:right;font-size:12.5px}
tbody tr{transition:background .12s}tbody tr:hover{background:var(--pnl2)}
.sym{font-weight:800;font-size:13px}
.links{display:inline-flex;gap:6px;margin-left:8px;vertical-align:1px}
.lk{font-size:9.5px;font-weight:700;color:var(--dim);text-decoration:none;border:1px solid var(--bd);
padding:1px 6px;border-radius:5px}
.lk:hover{color:var(--cy);border-color:var(--cy)}
.padtag{font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;background:rgba(107,124,156,.12);color:var(--mut)}
.padtag.v{background:rgba(79,195,255,.12);color:var(--cy)}
.pos{color:var(--ac)}.neg{color:var(--rd)}.dim{color:var(--dim)}
.side-BUY{color:var(--ac);font-weight:800}.side-SELL{color:var(--am);font-weight:800}
.sc{font-size:10px;font-weight:800;padding:2px 8px;border-radius:6px}
.sc-STRONG{background:rgba(55,224,176,.14);color:var(--ac)}.sc-watch{background:rgba(245,184,76,.12);color:var(--am)}
.sc-weak{color:var(--dim)}.sc-BLOCKED{background:rgba(255,84,112,.14);color:var(--rd)}
.empty{color:var(--dim);text-align:center;padding:26px;font-size:12px}
/* wallets view */
.wbox{max-width:660px}
textarea{width:100%;height:170px;background:var(--pnl2);border:1px solid var(--bd);border-radius:10px;
color:var(--tx);padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:12px;resize:vertical}
.btn{background:var(--grad);color:#04241a;border:0;padding:9px 18px;border-radius:9px;font-weight:800;cursor:pointer;margin-top:10px}
.hint{color:var(--dim);font-size:11.5px;margin-top:8px}
.wcount{font-size:13px;color:var(--ac);font-weight:700;margin-top:12px}
</style></head><body>
<aside>
 <div class=brand><div class=gem>◆</div><b>CAMBRIAN</b></div>
 <div class="nav on" data-v=desk><span class=ic>▦</span>Desk</div>
 <div class=nav data-v=scout><span class=ic>◎</span>Scouting</div>
 <div class=nav data-v=wallets><span class=ic>◈</span>Wallets</div>
 <div class=agents><h4>Agents</h4><div id=agents></div></div>
</aside>
<main>
<topbar>
 <span class="pill paper" id=mode>PAPER</span>
 <span class=meta id=meta></span>
 <span class=sp></span>
 <span class="eqbig mono" id=eqTop>$0.00</span>
 <button class=connect onclick="alert('Wallet connect (Privy) arrives with LIVE mode. The paper desk needs no wallet.')">Connect Wallet</button>
</topbar>
<div class=kpis>
 <div class=kpi><div class=l>Equity P&L</div><div class="n mono" id=eq>$0</div></div>
 <div class=kpi><div class=l>Realized</div><div class="n mono" id=real>$0</div></div>
 <div class=kpi><div class=l>Unrealized</div><div class="n mono" id=unreal>$0</div></div>
 <div class=kpi><div class=l>Open</div><div class="n mono" id=open>0</div></div>
 <div class=kpi><div class=l>Win rate</div><div class="n mono" id=wr>–</div></div>
</div>
<div class="view on" id=v-desk>
 <div class=card><h3>Open positions<span class=cnt id=posn></span></h3>
  <table><thead><tr><th>Token</th><th>Pad</th><th>Agent</th><th>Size</th><th>Value</th><th>Δ%</th><th>uPnL</th><th>Age</th></tr></thead>
  <tbody id=positions></tbody></table></div>
 <div class=g2>
  <div class=card><h3>Blotter · live tape</h3>
   <table><thead><tr><th>Time</th><th></th><th>Token</th><th>Agent</th><th>USD</th><th>PnL</th></tr></thead>
   <tbody id=blotter></tbody></table></div>
  <div class=card><h3>Closed trades</h3>
   <table><thead><tr><th>Token</th><th>Cost</th><th>Exit</th><th>PnL</th><th>Why</th></tr></thead>
   <tbody id=closed></tbody></table></div>
 </div>
</div>
<div class=view id=v-scout>
 <div class=card><h3>Scouting · fresh launches</h3>
  <table><thead><tr><th>Token</th><th>Pad</th><th>Net flow</th><th>Liquidity</th><th>Confluence</th></tr></thead>
  <tbody id=scouting></tbody></table></div>
</div>
<div class=view id=v-wallets>
 <div class="card wbox"><h3>Wallet tracker · Agent C feed</h3>
  <div style="padding:16px">
   <div class=hint style="margin:0 0 10px">Paste wallet addresses to track — one per line (0x…). Agent C will copy-trade
   their buys once enabled. Stored locally in <span class=mono>~/cambrian_wallets.json</span>.</div>
   <textarea id=wtext placeholder="0xabc...&#10;0xdef..."></textarea>
   <button class=btn onclick=saveWallets()>Save wallets</button>
   <div class=wcount id=wcount></div>
  </div></div>
</div>
</main>
<script>
const DEX=t=>`https://dexscreener.com/search?q=${t}`;
const EXP=t=>`https://robinhoodchain.blockscout.com/address/${t}`;
const PADURL={"pools-trade":"https://pools.trade/token/","flap":"https://flap.sh/token/","bankr":"https://bankr.bot/token/","pons":"https://pons.fun/token/"};
function d$(n){const s=n>=0?'':'-',a=Math.abs(n);return s+'$'+(a>=1000?(a/1000).toFixed(1)+'k':a.toFixed(2))}
function cls(n){return n>0?'pos':n<0?'neg':'dim'}
function tok(sym,addr,pad){
 const p=(pad||'').replace('?','');const pu=PADURL[p]?`<a class=lk href="${PADURL[p]+addr}" target=_blank>PAD</a>`:'';
 return `<span class=sym title="${addr}">${sym||'?'}</span><span class=links>`+
   `<a class=lk href="${DEX(addr)}" target=_blank>DEX</a>${pu}<a class=lk href="${EXP(addr)}" target=_blank>SCAN</a></span>`}
function padtag(p,v){return `<span class="padtag ${v?'v':''}">${p}${v?' ✓':''}</span>`}
document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>{
 document.querySelectorAll('.nav').forEach(x=>x.classList.remove('on'));n.classList.add('on');
 document.querySelectorAll('.view').forEach(x=>x.classList.remove('on'));
 document.getElementById('v-'+n.dataset.v).classList.add('on')});
async function saveWallets(){
 const lines=document.getElementById('wtext').value.split('\n').map(s=>s.trim()).filter(s=>/^0x[a-fA-F0-9]{40}$/.test(s));
 const r=await fetch('/wallets',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(lines)});
 const d=await r.json();document.getElementById('wcount').textContent=`${d.count} wallets tracked`}
async function loadWallets(){const d=await(await fetch('/wallets')).json();
 document.getElementById('wtext').value=d.wallets.join('\n');
 document.getElementById('wcount').textContent=`${d.count} wallets tracked`}
function agents(a){document.getElementById('agents').innerHTML=a.map(x=>
 `<div class="ag ${x.on?'on':''}"><span class=st></span>${x.name}<small>${x.on?'live':'soon'}</small></div>`).join('')}
async function tick(){let d;try{d=await(await fetch('/data')).json()}catch(e){return}
 document.getElementById('meta').innerHTML=`block <b>${d.block}</b> · PM <b>${d.model}</b> · $${d.size}/trade · ${d.updated}`+(d.err?` <span class=neg>${d.err}</span>`:'');
 const eq=document.getElementById('eqTop');eq.textContent=d$(d.equity);eq.className='eqbig mono '+cls(d.equity);
 document.getElementById('eq').innerHTML=`<span class=${cls(d.equity)}>${d$(d.equity)}</span>`;
 document.getElementById('real').innerHTML=`<span class=${cls(d.realized)}>${d$(d.realized)}</span>`;
 document.getElementById('unreal').innerHTML=`<span class=${cls(d.unrealized)}>${d$(d.unrealized)}</span>`;
 document.getElementById('open').textContent=d.positions.length;
 const t=d.wins+d.losses;document.getElementById('wr').textContent=t?Math.round(100*d.wins/t)+'%':'–';
 document.getElementById('posn').textContent=d.positions.length;
 agents(d.agents);
 document.getElementById('positions').innerHTML=d.positions.length?d.positions.map(p=>
  `<tr><td>${tok(p.sym,p.token,p.pad)}</td><td>${padtag(p.pad,true)}</td><td class=dim>${p.agent}</td>`+
  `<td class=mono>${d$(p.cost)}</td><td class=mono>${d$(p.value)}</td>`+
  `<td class="mono ${cls(p.chg)}">${p.chg>0?'+':''}${p.chg}%</td>`+
  `<td class="mono ${cls(p.upnl)}">${d$(p.upnl)}</td><td class="mono dim">${p.age}m</td></tr>`).join(''):
  '<tr><td colspan=8 class=empty>flat — waiting for a STRONG signal</td></tr>';
 document.getElementById('blotter').innerHTML=d.blotter.length?d.blotter.map(b=>
  `<tr><td class="mono dim">${b.t}</td><td class=side-${b.side}>${b.side}</td><td>${tok(b.sym,b.token,b.pad)}</td>`+
  `<td class=dim>${b.agent}</td><td class=mono>${d$(b.usd)}</td>`+
  `<td class="mono ${b.pnl==null?'dim':cls(b.pnl)}">${b.pnl==null?'—':d$(b.pnl)}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>no trades yet</td></tr>';
 document.getElementById('closed').innerHTML=d.closed.length?d.closed.map(c=>
  `<tr><td>${tok(c.sym,c.token,c.pad)}</td><td class=mono>${d$(c.cost)}</td><td class=mono>${d$(c.exit_value)}</td>`+
  `<td class="mono ${cls(c.pnl)}">${d$(c.pnl)}</td><td class=dim>${c.why}</td></tr>`).join(''):
  '<tr><td colspan=5 class=empty>none yet</td></tr>';
 document.getElementById('scouting').innerHTML=d.scouting.length?d.scouting.map(s=>
  `<tr><td>${tok(s.sym,s.token,s.pad)}</td><td>${padtag(s.pad,s.verified)}</td>`+
  `<td class="mono ${cls(s.net)}">${d$(s.net)}</td><td class=mono>${d$(s.liq)}</td>`+
  `<td><span class="sc sc-${s.tier}">${s.conf} ${s.tier}</span></td></tr>`).join(''):
  '<tr><td colspan=5 class=empty>scanning Robinhood Chain…</td></tr>';
}
tick();setInterval(tick,4000);loadWallets();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/data"):
            self._send(json.dumps(STATE).encode(), "application/json")
        elif self.path.startswith("/wallets"):
            self._send(json.dumps({"wallets": WALLETS, "count": len(WALLETS)}).encode(),
                       "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/wallets"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"[]")
                ws = [w.lower() for w in data if isinstance(w, str)
                      and w.startswith("0x") and len(w) == 42]
                WALLETS.clear()
                WALLETS.extend(sorted(set(ws)))
                save_wallets(WALLETS)
                self._send(json.dumps({"ok": True, "count": len(WALLETS)}).encode(),
                           "application/json")
            except Exception as e:
                self._send(json.dumps({"ok": False, "err": str(e)}).encode(),
                           "application/json")
        else:
            self._send(b"{}", "application/json")


def main():
    threading.Thread(target=worker, daemon=True).start()
    print(f"\n  ◆ CAMBRIAN desk (paper) running  ->  http://localhost:{PORT}")
    print(f"    PM {STATE['model']}  ·  ${SIZE_USD}/trade  ·  stop -{STOP_PCT:.0%}  tp {TP_MULT}x  ·  "
          f"{len(WALLETS)} wallets tracked   (Ctrl+C to stop)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
