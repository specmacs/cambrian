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


def refresh_weth_price():
    """Live ETH price from Flash's asset search, so USD figures aren't scaled by a
    stale constant. Falls back to the current value if the lookup fails."""
    global WETH_USD
    try:
        r = requests.get(FLASH_BASE + "/search", params={"query": WETH, "chain": "robinhood",
                                                         "limit": 1}, timeout=20,
                         headers={"x-definitive-api-key": FLASH_KEY})
        a = (r.json().get("assets") or [])
        p = float(a[0]["price"]) if a and a[0].get("price") else 0.0
        if 100 < p < 100000:                      # sanity band
            WETH_USD = p
            STATE["eth"] = round(p, 2)
    except Exception:
        pass


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
         "wallets": len(WALLETS), "eth": WETH_USD, "agents": [
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
    refresh_weth_price()
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
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Inter+Tight:wght@400;500;600;700;800&display=swap" rel=stylesheet>
<style>
/* ── THEMES ── cycle with the button in the nav (or press T). Persists. ── */
:root{--ui:"Geist Mono";--mono:"Geist Mono";
--bg:#000000;--pnl:#08080a;--pnl2:#0e0e11;--rail:#050506;--bd:#18181c;--bd2:#121215;
--tx:#f0f0f2;--mut:#8a8a94;--dim:#4d4d57;--gr:#00ff9d;--rd:#ff3b5c;--cy:#4dd8ff;--am:#ffc043;
--pu:#b98bff;--grad:linear-gradient(135deg,#00ff9d,#4dd8ff)}
[data-t="carbon"]{--ui:"Geist";--mono:"Geist Mono";
--bg:#0c0c0d;--pnl:#131315;--pnl2:#1a1a1d;--rail:#0f0f11;--bd:#232326;--bd2:#1b1b1e;
--tx:#ededf0;--mut:#96969e;--dim:#5a5a63;--gr:#4ade80;--rd:#f87171;--cy:#60c8f8;--am:#fbbf24;
--pu:#a78bfa;--grad:linear-gradient(135deg,#4ade80,#60c8f8)}
[data-t="midnight"]{--ui:"Space Grotesk";--mono:"JetBrains Mono";
--bg:#060911;--pnl:#0b1020;--pnl2:#111829;--rail:#080c17;--bd:#1a2440;--bd2:#141c33;
--tx:#e4ecfb;--mut:#8296bd;--dim:#4a5b80;--gr:#2fe3a3;--rd:#ff5470;--cy:#4cc9ff;--am:#ffb340;
--pu:#a78bfa;--grad:linear-gradient(135deg,#2fe3a3,#4cc9ff)}
[data-t="phosphor"]{--ui:"IBM Plex Mono";--mono:"IBM Plex Mono";
--bg:#050705;--pnl:#0a0f0a;--pnl2:#0f160f;--rail:#070b07;--bd:#16211a;--bd2:#111a14;
--tx:#d6f5e0;--mut:#6f9d84;--dim:#3f5f4d;--gr:#00e676;--rd:#ff5252;--cy:#64d8cb;--am:#ffca28;
--pu:#9ccc65;--grad:linear-gradient(135deg,#00e676,#64d8cb)}
[data-t="slate"]{--ui:"Inter Tight";--mono:"JetBrains Mono";
--bg:#0f1115;--pnl:#171a21;--pnl2:#1e222b;--rail:#12151a;--bd:#272c37;--bd2:#1f232c;
--tx:#e9edf5;--mut:#98a2b8;--dim:#5b6478;--gr:#34d399;--rd:#fb7185;--cy:#38bdf8;--am:#fbbf24;
--pu:#a78bfa;--grad:linear-gradient(135deg,#34d399,#38bdf8)}
*{box-sizing:border-box;margin:0}
::-webkit-scrollbar{width:7px;height:7px}::-webkit-scrollbar-thumb{background:#1e1e2a;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}
body{background:var(--bg);color:var(--tx);height:100vh;overflow:hidden;display:flex;flex-direction:column;
font:500 12.5px/1.4 var(--ui),-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;letter-spacing:-.01em}
.m,th,.n,.eqn{font-family:var(--mono),ui-monospace,Menlo,Consolas,monospace;
font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1,"zero" 1}
/* top nav */
nav{display:flex;align-items:center;gap:22px;padding:0 16px;height:48px;background:var(--pnl);
border-bottom:1px solid var(--bd);flex-shrink:0}
.brand{display:flex;align-items:center;gap:8px;padding-right:8px}
.gem{width:24px;height:24px;border-radius:7px;background:var(--grad);display:grid;place-items:center;
color:#04241a;font-weight:800;font-size:13px}
.brand b{font-size:13px;letter-spacing:.24em;font-weight:800}
.nv{font-size:12.5px;font-weight:600;color:var(--mut);cursor:pointer;padding:15px 2px;
border-bottom:2px solid transparent;margin-bottom:-1px}
.nv:hover{color:var(--tx)}.nv.on{color:var(--tx);border-bottom-color:var(--gr)}
.sp{flex:1}
.pill{font-size:9.5px;font-weight:800;letter-spacing:.12em;padding:4px 9px;border-radius:5px;
background:rgba(90,200,250,.1);color:var(--cy);border:1px solid rgba(90,200,250,.25)}
.eqn{font-size:14px;font-weight:700}
.btn{background:var(--grad);color:#04241a;border:0;padding:7px 15px;border-radius:8px;
font:800 11.5px "Inter Tight";cursor:pointer;letter-spacing:.02em}
.btn:hover{filter:brightness(1.12)}
/* 3-col shell */
.shell{flex:1;display:grid;grid-template-columns:282px 1fr 300px;overflow:hidden}
.rail{background:var(--rail);border-right:1px solid var(--bd);overflow-y:auto}
.rail.r{border-right:0;border-left:1px solid var(--bd)}
.rh{padding:11px 14px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
font-weight:700;border-bottom:1px solid var(--bd2);position:sticky;top:0;background:var(--rail);z-index:2;
display:flex;align-items:center;gap:7px}
.rh .c{margin-left:auto;color:var(--dim)}
center{display:block;overflow-y:auto;background:var(--bg)}
/* kpi strip */
.kpis{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--bd)}
.kpi{padding:11px 16px;border-right:1px solid var(--bd2)}
.kpi:last-child{border-right:0}
.kpi .l{font-size:9px;letter-spacing:.13em;color:var(--dim);text-transform:uppercase;font-weight:700}
.kpi .n{font-size:19px;font-weight:700;margin-top:3px}
/* tabs */
.tabs{display:flex;gap:20px;padding:0 16px;border-bottom:1px solid var(--bd);background:var(--pnl)}
.tb{font-size:12px;font-weight:600;color:var(--mut);cursor:pointer;padding:11px 1px;
border-bottom:2px solid transparent;margin-bottom:-1px}
.tb:hover{color:var(--tx)}.tb.on{color:var(--tx);border-bottom-color:var(--gr)}
.tb .c{color:var(--dim);font-weight:500;margin-left:5px}
.pane{display:none}.pane.on{display:block}
/* tables */
table{width:100%;border-collapse:collapse}
th{font-size:9px;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);text-align:right;
padding:8px 14px;font-weight:700;border-bottom:1px solid var(--bd2);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:7px 14px;border-bottom:1px solid var(--bd2);text-align:right;font-size:12.5px;white-space:nowrap}
tbody tr{transition:background .1s}tbody tr:hover{background:var(--pnl2)}
tr.buy{background:rgba(61,220,132,.045)}tr.sell{background:rgba(255,77,109,.045)}
@keyframes fl{0%{background:rgba(61,220,132,.2)}100%{background:transparent}}
tr.new{animation:fl 1.5s ease-out}
/* token cell */
.tk{display:flex;align-items:center;gap:8px}
.av{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-size:9.5px;
font-weight:800;color:#0b0b0f;flex-shrink:0;letter-spacing:-.03em}
.sym{font-weight:700;font-size:12.5px;cursor:pointer}.sym:hover{color:var(--gr)}
.lks{display:flex;gap:4px;opacity:0;transition:opacity .12s}
tr:hover .lks,.card:hover .lks{opacity:1}
.lk{font-size:8.5px;font-weight:700;color:var(--dim);text-decoration:none;border:1px solid var(--bd);
padding:1px 5px;border-radius:4px;letter-spacing:.04em}
.lk:hover{color:var(--cy);border-color:var(--cy)}
.pos{color:var(--gr)}.neg{color:var(--rd)}.dim{color:var(--dim)}.mut{color:var(--mut)}
.tag{font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;background:rgba(139,147,167,.1);color:var(--mut)}
.tag.v{background:rgba(90,200,250,.1);color:var(--cy)}
.sc{font-size:9.5px;font-weight:800;padding:2px 7px;border-radius:5px}
.sc-STRONG{background:rgba(61,220,132,.14);color:var(--gr)}
.sc-watch{background:rgba(255,179,64,.12);color:var(--am)}
.sc-weak{color:var(--dim)}.sc-BLOCKED{background:rgba(255,77,109,.14);color:var(--rd)}
/* rail feed cards */
.card{padding:10px 14px;border-bottom:1px solid var(--bd2);cursor:default}
.card:hover{background:var(--pnl2)}
.ch{display:flex;align-items:center;gap:6px;font-size:11.5px;margin-bottom:7px}
.ch .who{font-weight:700;color:var(--pu)}
.ch .act{font-weight:700}.ch .t{margin-left:auto;color:var(--dim);font-size:10.5px}
.mini{display:flex;align-items:center;gap:9px;padding:8px 10px;background:var(--pnl);
border:1px solid var(--bd);border-radius:9px}
.mini .nm{font-weight:700;font-size:12px}
.mini .sub{font-size:10px;color:var(--dim);margin-top:1px}
.mini .amt{margin-left:auto;font-weight:700;font-size:12.5px}
/* right rail scout cards */
.scard{padding:11px 14px;border-bottom:1px solid var(--bd2)}
.scard:hover{background:var(--pnl2)}
.sct{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.sct .nm{font-weight:700;font-size:13px;cursor:pointer}.sct .nm:hover{color:var(--gr)}
.grid4{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px}
.st .l{font-size:8.5px;letter-spacing:.1em;color:var(--dim);text-transform:uppercase;font-weight:700}
.st .v{font-size:11.5px;font-weight:600;margin-top:1px}
.empty{color:var(--dim);text-align:center;padding:30px 14px;font-size:11.5px}
/* status bar */
.status{display:flex;align-items:center;gap:16px;height:30px;padding:0 14px;background:var(--pnl);
border-top:1px solid var(--bd);font-size:10.5px;color:var(--dim);flex-shrink:0}
.status b{color:var(--mut);font-weight:600}
.dot{width:6px;height:6px;border-radius:50%;background:var(--gr);animation:p 2s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(61,220,132,.5)}70%{box-shadow:0 0 0 6px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
.agl{display:flex;align-items:center;gap:7px;padding:7px 14px;font-size:11px;color:var(--mut);
border-bottom:1px solid var(--bd2)}
.agl .st2{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.agl.on .st2{background:var(--gr);box-shadow:0 0 5px rgba(61,220,132,.7)}
.agl.on{color:var(--tx)}.agl small{margin-left:auto;color:var(--dim);font-size:9.5px;letter-spacing:.06em}
.wv{padding:16px;max-width:620px}
textarea{width:100%;height:220px;background:var(--pnl);border:1px solid var(--bd);border-radius:10px;
color:var(--tx);padding:12px;font-family:"JetBrains Mono",monospace;font-size:11.5px;resize:vertical}
.hint{color:var(--dim);font-size:11px;margin:8px 0}
.toast{position:fixed;bottom:44px;left:50%;transform:translateX(-50%) translateY(70px);background:var(--gr);
color:#04241a;font-weight:700;padding:8px 16px;border-radius:8px;font-size:11.5px;
transition:transform .2s cubic-bezier(.2,.9,.3,1.3);z-index:99}
.toast.on{transform:translateX(-50%) translateY(0)}
</style></head><body>
<nav>
 <div class=brand><div class=gem>◆</div><b>CAMBRIAN</b></div>
 <span class="nv on" data-v=desk>Desk</span>
 <span class=nv data-v=scout>Scouting</span>
 <span class=nv data-v=wallets>Wallets</span>
 <span class=sp></span>
 <span class=pill id=mode>PAPER</span>
 <span class=pill id=themeBtn style="cursor:pointer;background:transparent;border-color:var(--bd);color:var(--mut)"
  title="cycle theme (T)">◐ VOID</span>
 <span class="eqn m" id=eqTop>$0.00</span>
 <button class=btn onclick="alert('Wallet connect (Privy) arrives with LIVE mode. The paper desk needs no wallet.')">Connect</button>
</nav>
<div class=shell>
 <div class=rail>
  <div class=rh>Agent activity<span class=c id=actc></span></div>
  <div id=feed></div>
  <div class=rh style="border-top:1px solid var(--bd)">Agents</div>
  <div id=agents></div>
 </div>
 <center>
  <div class=kpis>
   <div class=kpi><div class=l>Equity</div><div class="n m" id=eq>$0</div></div>
   <div class=kpi><div class=l>Realized</div><div class="n m" id=real>$0</div></div>
   <div class=kpi><div class=l>Unrealized</div><div class="n m" id=unreal>$0</div></div>
   <div class=kpi><div class=l>Open</div><div class="n m" id=open>0</div></div>
   <div class=kpi><div class=l>Win rate</div><div class="n m" id=wr>–</div></div>
  </div>
  <div id=v-desk>
   <div class=tabs><span class="tb on" data-t=pos>Positions<span class=c id=cpos></span></span>
    <span class=tb data-t=clo>Closed<span class=c id=cclo></span></span>
    <span class=tb data-t=blo>Blotter<span class=c id=cblo></span></span></div>
   <div class="pane on" id=p-pos><table><thead><tr><th>Token</th><th>Pad</th><th>Agent</th>
    <th>Size</th><th>Value</th><th>Δ%</th><th>uPnL</th><th>Age</th></tr></thead>
    <tbody id=positions></tbody></table></div>
   <div class=pane id=p-clo><table><thead><tr><th>Token</th><th>Pad</th><th>Cost</th><th>Exit</th>
    <th>PnL</th><th>Exit reason</th></tr></thead><tbody id=closed></tbody></table></div>
   <div class=pane id=p-blo><table><thead><tr><th>Time</th><th></th><th>Token</th><th>Agent</th>
    <th>USD</th><th>PnL</th></tr></thead><tbody id=blotter></tbody></table></div>
  </div>
  <div id=v-scout style=display:none>
   <table><thead><tr><th>Token</th><th>Pad</th><th>Net flow</th><th>Liquidity</th><th>Confluence</th></tr></thead>
    <tbody id=scoutfull></tbody></table></div>
  <div id=v-wallets style=display:none><div class=wv>
   <div class=hint>Wallets for <b>Agent C</b> to copy-trade. One address per line. Saved to
    <span class=m>~/cambrian_wallets.json</span>.</div>
   <textarea id=wtext placeholder="0xabc…&#10;0xdef…"></textarea>
   <button class=btn style="margin-top:10px" onclick=saveWallets()>Save wallets</button>
   <div class=hint id=wcount></div></div></div>
 </center>
 <div class="rail r">
  <div class=rh>Hunting<span class=c id=scc></span></div>
  <div id=scout></div>
 </div>
</div>
<div class=status><span class=dot></span><b id=stmode>paper</b>
 <span>block <b id=stblk>–</b></span><span>PM <b id=stpm>–</b></span>
 <span>size <b id=stsz>–</b></span><span>ETH <b id=steth>–</b></span>
 <span class=sp style=flex:1></span><span id=sterr></span><span>CAMBRIAN · Robinhood Chain</span></div>
<div class=toast id=toast></div>
<script>
const DEX=t=>`https://dexscreener.com/search?q=${t}`,EXP=t=>`https://robinhoodchain.blockscout.com/address/${t}`;
const PADURL={"pools-trade":"https://pools.trade/token/","flap":"https://flap.sh/token/",
 "bankr":"https://bankr.bot/token/","pons":"https://pons.fun/token/"};
const AVC=['#3ddc84','#5ac8fa','#a78bfa','#ffb340','#ff4d6d','#4ade80','#38bdf8','#f472b6'];
let SEEN=new Set(),FIRST=true;
function d$(n){const s=n<0?'-':'',a=Math.abs(n);
 return s+'$'+(a>=1e6?(a/1e6).toFixed(2)+'M':a>=1e3?(a/1e3).toFixed(1)+'K':a.toFixed(2))}
function cls(n){return n>0?'pos':n<0?'neg':'dim'}
function hash(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h)}
function av(sym,addr){const c=AVC[hash(addr)%AVC.length];
 return `<div class=av style="background:${c}">${(sym||'?').replace(/[^A-Za-z0-9]/g,'').slice(0,2).toUpperCase()}</div>`}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('on');
 clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),1300)}
function copy(a){navigator.clipboard.writeText(a).then(()=>toast('copied '+a.slice(0,12)+'…'))}
function lks(addr,pad){const p=(pad||'').replace('?','');
 return `<span class=lks><a class=lk href="${DEX(addr)}" target=_blank>DEX</a>`+
 (PADURL[p]?`<a class=lk href="${PADURL[p]+addr}" target=_blank>PAD</a>`:'')+
 `<a class=lk href="${EXP(addr)}" target=_blank>SCAN</a></span>`}
function tkc(sym,addr,pad){return `<div class=tk>${av(sym,addr)}<span class=sym title="${addr} — click to copy" `+
 `onclick="copy('${addr}')">${sym||'?'}</span>${lks(addr,pad)}</div>`}
function tag(p,v){return `<span class="tag ${v?'v':''}">${p}${v?' ✓':''}</span>`}
document.querySelectorAll('.nv').forEach(n=>n.onclick=()=>{
 document.querySelectorAll('.nv').forEach(x=>x.classList.remove('on'));n.classList.add('on');
 ['desk','scout','wallets'].forEach(v=>document.getElementById('v-'+v).style.display=v==n.dataset.v?'':'none')});
document.querySelectorAll('.tb').forEach(t=>t.onclick=()=>{
 document.querySelectorAll('.tb').forEach(x=>x.classList.remove('on'));t.classList.add('on');
 document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
 document.getElementById('p-'+t.dataset.t).classList.add('on')});
const THEMES=[['void','VOID'],['carbon','CARBON'],['midnight','MIDNIGHT'],['phosphor','PHOSPHOR'],['slate','SLATE']];
let ti=THEMES.findIndex(t=>t[0]==(localStorage.cambrianTheme||'void'));if(ti<0)ti=0;
function setTheme(i){ti=(i+THEMES.length)%THEMES.length;const[v,n]=THEMES[ti];
 if(v=='void')document.documentElement.removeAttribute('data-t');else document.documentElement.setAttribute('data-t',v);
 localStorage.cambrianTheme=v;document.getElementById('themeBtn').textContent='◐ '+n}
setTheme(ti);
document.getElementById('themeBtn').onclick=()=>setTheme(ti+1);
document.addEventListener('keydown',e=>{if(e.target.tagName=='TEXTAREA')return;
 if(e.key.toLowerCase()=='t'){setTheme(ti+1);return}
 const k={'1':'desk','2':'scout','3':'wallets'}[e.key];if(k)document.querySelector(`.nv[data-v=${k}]`).click()});
async function saveWallets(){
 const l=document.getElementById('wtext').value.split('\n').map(s=>s.trim()).filter(s=>/^0x[a-fA-F0-9]{40}$/.test(s));
 const d=await(await fetch('/wallets',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(l)})).json();
 document.getElementById('wcount').textContent=`${d.count} wallets tracked`;toast(`saved ${d.count} wallets`)}
async function loadWallets(){const d=await(await fetch('/wallets')).json();
 document.getElementById('wtext').value=d.wallets.join('\n');
 document.getElementById('wcount').textContent=`${d.count} wallets tracked`}
async function tick(){let d;try{d=await(await fetch('/data')).json()}catch(e){return}
 const eq=document.getElementById('eqTop');eq.textContent=d$(d.equity);eq.className='eqn m '+cls(d.equity);
 document.getElementById('eq').innerHTML=`<span class=${cls(d.equity)}>${d$(d.equity)}</span>`;
 document.getElementById('real').innerHTML=`<span class=${cls(d.realized)}>${d$(d.realized)}</span>`;
 document.getElementById('unreal').innerHTML=`<span class=${cls(d.unrealized)}>${d$(d.unrealized)}</span>`;
 document.getElementById('open').textContent=d.positions.length;
 const t=d.wins+d.losses;document.getElementById('wr').textContent=t?Math.round(100*d.wins/t)+'%':'–';
 document.getElementById('cpos').textContent=d.positions.length;
 document.getElementById('cclo').textContent=d.closed.length;
 document.getElementById('cblo').textContent=d.blotter.length;
 document.getElementById('stblk').textContent=d.block;document.getElementById('stpm').textContent=d.model;
 document.getElementById('stsz').textContent='$'+d.size;
 document.getElementById('steth').textContent='$'+(d.eth||0).toLocaleString();document.getElementById('stmode').textContent=d.mode.toLowerCase();
 document.getElementById('sterr').innerHTML=d.err?`<span class=neg>${d.err}</span>`:'';
 // positions
 document.getElementById('positions').innerHTML=d.positions.length?d.positions.map(p=>
  `<tr><td>${tkc(p.sym,p.token,p.pad)}</td><td>${tag(p.pad,true)}</td><td class=dim>${p.agent}</td>`+
  `<td class=m>${d$(p.cost)}</td><td class=m>${d$(p.value)}</td>`+
  `<td class="m ${cls(p.chg)}">${p.chg>0?'+':''}${p.chg}%</td>`+
  `<td class="m ${cls(p.upnl)}">${d$(p.upnl)}</td><td class="m dim">${p.age}m</td></tr>`).join(''):
  '<tr><td colspan=8 class=empty>flat — agents are hunting</td></tr>';
 document.getElementById('closed').innerHTML=d.closed.length?d.closed.map(c=>
  `<tr><td>${tkc(c.sym,c.token,c.pad)}</td><td>${tag(c.pad,true)}</td><td class=m>${d$(c.cost)}</td>`+
  `<td class=m>${d$(c.exit_value)}</td><td class="m ${cls(c.pnl)}">${d$(c.pnl)}</td>`+
  `<td class=dim>${c.why}</td></tr>`).join(''):'<tr><td colspan=6 class=empty>no closed trades</td></tr>';
 document.getElementById('blotter').innerHTML=d.blotter.length?d.blotter.map(b=>
  `<tr class=${b.side=='BUY'?'buy':'sell'}><td class="m dim">${b.t}</td>`+
  `<td class="${b.side=='BUY'?'pos':'neg'}" style=font-weight:800>${b.side}</td>`+
  `<td>${tkc(b.sym,b.token,b.pad)}</td><td class=dim>${b.agent}</td><td class=m>${d$(b.usd)}</td>`+
  `<td class="m ${b.pnl==null?'dim':cls(b.pnl)}">${b.pnl==null?'—':d$(b.pnl)}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>no trades yet</td></tr>';
 // left rail: agent activity feed (from blotter)
 document.getElementById('actc').textContent=d.blotter.length;
 document.getElementById('feed').innerHTML=d.blotter.length?d.blotter.slice(0,18).map(b=>
  `<div class=card><div class=ch><span class=who>${b.agent}</span>`+
  `<span class="act ${b.side=='BUY'?'pos':'neg'}">${b.side=='BUY'?'bought':'sold'}</span>`+
  `<span class=m>${d$(b.usd)}</span><span class=t>${b.t}</span></div>`+
  `<div class=mini>${av(b.sym,b.token)}<div><div class=nm>${b.sym||'?'}</div>`+
  `<div class=sub>${b.pad}</div></div><span class="amt m ${b.pnl==null?'':cls(b.pnl)}">`+
  `${b.pnl==null?d$(b.usd):(b.pnl>0?'+':'')+d$(b.pnl)}</span></div></div>`).join(''):
  '<div class=empty>no agent activity yet</div>';
 document.getElementById('agents').innerHTML=d.agents.map(a=>
  `<div class="agl ${a.on?'on':''}"><span class=st2></span>${a.name}<small>${a.on?'LIVE':'SOON'}</small></div>`).join('');
 // right rail: hunting
 document.getElementById('scc').textContent=d.scouting.length;
 const sc=d.scouting.map(s=>{const isnew=!SEEN.has(s.token)&&!FIRST;SEEN.add(s.token);
  return `<div class="scard ${isnew?'new':''}"><div class=sct>${av(s.sym,s.token)}`+
  `<span class=nm onclick="copy('${s.token}')" title="${s.token}">${s.sym||'?'}</span>`+
  `<span style=margin-left:auto><span class="sc sc-${s.tier}">${s.tier}</span></span></div>`+
  `<div class=grid4><div class=st><div class=l>Net flow</div><div class="v m ${cls(s.net)}">${d$(s.net)}</div></div>`+
  `<div class=st><div class=l>Liquidity</div><div class="v m">${d$(s.liq)}</div></div>`+
  `<div class=st><div class=l>Pad</div><div class=v>${tag(s.pad,s.verified)}</div></div>`+
  `<div class=st><div class=l>Confluence</div><div class="v m">${s.conf}</div></div></div>`+
  `<div style=margin-top:7px>${lks(s.token,s.pad)}</div></div>`}).join('');
 document.getElementById('scout').innerHTML=sc||'<div class=empty>scanning Robinhood Chain…</div>';
 document.getElementById('scoutfull').innerHTML=d.scouting.length?d.scouting.map(s=>
  `<tr><td>${tkc(s.sym,s.token,s.pad)}</td><td>${tag(s.pad,s.verified)}</td>`+
  `<td class="m ${cls(s.net)}">${d$(s.net)}</td><td class=m>${d$(s.liq)}</td>`+
  `<td><span class="sc sc-${s.tier}">${s.conf} ${s.tier}</span></td></tr>`).join(''):
  '<tr><td colspan=5 class=empty>scanning…</td></tr>';
 FIRST=false;
}
tick();setInterval(tick,4000);loadWallets();
</script></body></html>
"""


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
