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
MARK_SECS = int(os.getenv("RH_MARK_SECS", "8"))
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


def fquote(target, contra, qty, side, quick=False):
    """quick=True reuses Flash's recently-computed quote (fast, for ENTRY). Never
    use it to VALUE a position — a cached quote freezes mark-to-market."""
    body = {"targetChain": "robinhood", "contraChain": "robinhood",
            "targetAsset": target, "contraAsset": contra, "side": side,
            "qty": str(qty), "orderType": "market", "maxSlippage": "0.2"}
    if quick:
        body["quickTrade"] = True
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
        b = fquote(token, WETH, round(usd / WETH_USD, 6), "buy", quick=True)
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
         "positions": [], "closed": [], "blotter": [], "scouting": [], "trace": [], "marked": None,
         "wallets": len(WALLETS), "eth": WETH_USD, "agents": [
             {"id": "sniper", "name": "A · Sniper", "desc": "fresh launches", "on": True},
             {"id": "volume", "name": "B · Volume", "desc": "all-RH momentum", "on": False},
             {"id": "wallets", "name": "C · Wallets", "desc": "smart-money copy", "on": False},
             {"id": "intel", "name": "D · Intel", "desc": "X / narrative", "on": False}]}
BOOK = {}
CLOSED = []
BLOTTER = []
TRACE = []
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


MARKED = [0.0]


def mark_and_exit():
    """Re-price every open position against a FRESH sell quote. Runs on its own
    fast cadence so P&L doesn't sit frozen behind a slow discovery sweep."""
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
    MARKED[0] = time.time()


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
                    "age_s": int(time.time() - p["opened"])} for p in pos],
        closed=[{"token": c["token"], "sym": c["sym"], "pad": c["pad"],
                 "cost": round(c["cost"], 2), "exit_value": c["exit_value"],
                 "pnl": c["pnl"], "why": c["why"]} for c in CLOSED[-30:]][::-1],
        blotter=BLOTTER[-40:][::-1], trace=TRACE[-60:][::-1],
        marked=int(time.time() - MARKED[0]) if MARKED[0] else None)


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
            TRACE.append({"t": now_hms(), "agent": "A · Sniper", "sym": symb, "token": tok,
                          "pad": pad, "decision": v["decision"], "conf": conf,
                          "net": round(m["net"]), "liq": round(liq),
                          "reason": v["reason"] or "confluence"})
            if v["decision"] == "buy":
                open_paper(tok, symb, pad, "sniper", v["reason"] or f"conf {conf}")
    STATE["scouting"] = scouting[:14]
    publish(block)


def worker():
    """Slow loop: discover + decide (many RPC calls per sweep)."""
    while True:
        try:
            scan_and_trade()
        except Exception as e:
            STATE.update(err=f"scan error: {e}", updated=now_hms())
        time.sleep(INTERVAL)


def marker():
    """Fast loop: mark the book to live prices and honour stops/targets. Runs
    independently so an open position re-prices every MARK_SECS regardless of how
    long a discovery sweep takes."""
    while True:
        try:
            if BOOK:
                mark_and_exit()
                publish(STATE.get("block", 0))
        except Exception:
            pass
        time.sleep(MARK_SECS)


PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Cambrian</title>
<link rel=icon href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2032%2032%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M5.5%2010.5%20L13%2016%20L5.5%2021.5%22%20stroke%3D%22%23E6E9EF%22%20stroke-width%3D%222.8%22%20stroke-linejoin%3D%22miter%22/%3E%3Cpath%20d%3D%22M15%206%20L25.5%2016%20L15%2026%22%20stroke%3D%22%238A7BFF%22%20stroke-width%3D%223.4%22%20stroke-linejoin%3D%22miter%22/%3E%3C/svg%3E">
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap" rel=stylesheet>
<style>
/* ── tokens ─────────────────────────────────────────────────────────────── */
:root{
--bg:#0B0E14;--surface:#12161F;--surface-raised:#1A1F2B;--border:#232936;--border-strong:#333B4D;
--text:#E6E9EF;--text-dim:#8B94A7;--text-faint:#5A6376;
--up:#2ED3A7;--down:#FF6B7A;--agent:#8A7BFF;--pending:#F2B544;--focus:#4DA6FF;
--fill-hover:color-mix(in oklab,var(--text) 4%,transparent);
--fill-selected:color-mix(in oklab,var(--text) 8%,transparent);
--fill-up:color-mix(in oklab,var(--up) 16%,transparent);
--fill-down:color-mix(in oklab,var(--down) 16%,transparent);
--fill-agent:color-mix(in oklab,var(--agent) 16%,transparent);
--fill-pending:color-mix(in oklab,var(--pending) 16%,transparent);
--font-ui:"Geist",ui-sans-serif,system-ui,-apple-system,sans-serif;
--font-mono:"Commit Mono","Geist Mono",ui-monospace,SFMono-Regular,monospace;
--t-micro:10px;--lh-micro:14px;--t-xs:11px;--lh-xs:16px;--t-sm:12px;--lh-sm:18px;
--t-base:13px;--lh-base:20px;--t-md:15px;--lh-md:22px;--t-lg:20px;--lh-lg:26px;
--t-xl:28px;--lh-xl:32px;--tracking-micro:.08em;--tracking-tight:-.02em;
--w-body:400;--w-ui:500;--w-emph:600;
--s-1:4px;--s-2:8px;--s-3:12px;--s-4:16px;--s-6:24px;--s-8:32px;
--row-dense:28px;--row-default:32px;--row-comfy:40px;
--r-sm:2px;--r-md:4px;--hairline:1px;--gutter:2px;--rail-w:320px;--nav-w:48px;
--dur-fast:120ms;--dur-enter:160ms;--dur-flash:400ms;--ease:cubic-bezier(.2,0,0,1);
--shadow-overlay:0 8px 32px rgba(0,0,0,.5)}

/* ── palettes ── same token names, different values. Structure never changes.
   --agent stays a hue used for NOTHING else within each palette. ────────────── */
[data-theme="graphite"]{
--bg:#0E0F11;--surface:#15171A;--surface-raised:#1D2024;--border:#272B31;--border-strong:#373D45;
--text:#E9EBEE;--text-dim:#8D949E;--text-faint:#5C636D;
--up:#2ED3A7;--down:#FF6B7A;--agent:#9B8CFF;--pending:#F2B544;--focus:#4DA6FF}
[data-theme="void"]{
--bg:#050507;--surface:#0B0B0F;--surface-raised:#131318;--border:#1E1E26;--border-strong:#2E2E3A;
--text:#F2F4F8;--text-dim:#8E93A3;--text-faint:#585D6D;
--up:#00E5A0;--down:#FF5470;--agent:#A78BFF;--pending:#FFC043;--focus:#5AC8FA}
[data-theme="ember"]{
--bg:#0F0C0A;--surface:#171310;--surface-raised:#211B16;--border:#2C241D;--border-strong:#3E3328;
--text:#F2EAE1;--text-dim:#A2917F;--text-faint:#6E6154;
--up:#43C79A;--down:#FF6B5A;--agent:#C79BFF;--pending:#F2B544;--focus:#5AA9FF}
[data-theme="nocturne"]{
--bg:#090A18;--surface:#101228;--surface-raised:#181B36;--border:#242848;--border-strong:#343A62;
--text:#E7E9FA;--text-dim:#8C93BE;--text-faint:#5A6091;
--up:#3FE0B0;--down:#FF6B8E;--agent:#B49BFF;--pending:#F5C155;--focus:#5B9DFF}
[data-theme="daylight"]{
--bg:#F6F7F9;--surface:#FFFFFF;--surface-raised:#EEF1F5;--border:#DDE2EA;--border-strong:#BFC7D4;
--text:#121722;--text-dim:#5A6376;--text-faint:#8B94A7;
--up:#0E9E77;--down:#D8394E;--agent:#6741E8;--pending:#A9760A;--focus:#0B6BCB}

*{box-sizing:border-box;margin:0}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--border)}
::-webkit-scrollbar-track{background:transparent}
html,body{height:100%}
body{background:var(--bg);color:var(--text);font-family:var(--font-ui);font-size:var(--t-base);
line-height:var(--lh-base);font-weight:var(--w-body);-webkit-font-smoothing:antialiased;
display:flex;overflow:hidden}
.num{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.label-micro{font-size:var(--t-micro);line-height:var(--lh-micro);letter-spacing:var(--tracking-micro);
text-transform:uppercase;color:var(--text-dim);font-weight:var(--w-ui)}
.up{color:var(--up)}.down{color:var(--down)}.dim{color:var(--text-dim)}.faint{color:var(--text-faint)}
/* ── nav rail ───────────────────────────────────────────────────────────── */
nav{width:var(--nav-w);flex-shrink:0;background:var(--surface);border-right:var(--hairline) solid var(--border);
display:flex;flex-direction:column;align-items:center;padding-top:var(--s-2)}
.mark{width:26px;height:26px;margin-bottom:var(--s-4);display:block;flex-shrink:0}
.ni{width:32px;height:32px;border-radius:var(--r-sm);display:grid;place-items:center;color:var(--text-faint);
cursor:pointer;margin-bottom:var(--s-1);font-size:15px;transition:color var(--dur-fast) var(--ease),background var(--dur-fast) var(--ease)}
.ni:hover{color:var(--text);background:var(--fill-hover)}
.ni.on{color:var(--text);background:var(--fill-selected)}
/* ── main column ────────────────────────────────────────────────────────── */
main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{height:var(--row-comfy);flex-shrink:0;display:flex;align-items:center;gap:var(--s-4);
padding:0 var(--s-4);border-bottom:var(--hairline) solid var(--border);background:var(--surface)}
.wordmark{font-size:var(--t-sm);font-weight:var(--w-ui);letter-spacing:.18em;text-transform:uppercase}
.chip{height:18px;display:inline-flex;align-items:center;padding:0 var(--s-2);border-radius:var(--r-sm);
font-size:var(--t-xs);font-weight:var(--w-ui);background:var(--fill-agent);color:var(--agent)}
.chip.pending{background:var(--fill-pending);color:var(--pending)}
.sep{color:var(--text-faint)}
.tbmeta{font-size:var(--t-xs);color:var(--text-dim)}
.tbmeta b{color:var(--text);font-weight:var(--w-ui)}
.grow{flex:1}
.btn{height:28px;padding:0 var(--s-3);border-radius:var(--r-sm);font-family:var(--font-ui);
font-size:var(--t-sm);font-weight:var(--w-ui);cursor:pointer;border:var(--hairline) solid var(--border);
background:transparent;color:var(--text-dim);transition:color var(--dur-fast) var(--ease)}
.btn:hover{color:var(--text);border-color:var(--border-strong)}
.btn.primary{background:var(--text);color:var(--bg);border-color:var(--text)}
/* ── kpi strip ──────────────────────────────────────────────────────────── */
.kpis{display:flex;flex-shrink:0;border-bottom:var(--hairline) solid var(--border);background:var(--surface)}
.kpi{padding:var(--s-2) var(--s-4);border-right:var(--hairline) solid var(--border);min-width:132px}
.kpi:last-child{border-right:0}
.kpi .v{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-size:var(--t-md);
line-height:var(--lh-md);font-weight:var(--w-ui);margin-top:2px}
.kpi.hero .v{font-size:var(--t-xl);line-height:var(--lh-xl);letter-spacing:var(--tracking-tight)}
/* ── panes ──────────────────────────────────────────────────────────────── */
.panes{flex:1;display:flex;flex-direction:column;min-height:0}
.pane{display:flex;flex-direction:column;min-height:0;border-bottom:var(--hairline) solid var(--border)}
.pane.fill{flex:1}
.ph{height:var(--row-default);flex-shrink:0;display:flex;align-items:center;gap:var(--s-4);
padding:0 var(--s-4);border-bottom:var(--hairline) solid var(--border);background:var(--surface)}
.tab{font-size:var(--t-sm);font-weight:var(--w-ui);color:var(--text-faint);cursor:pointer;
padding:var(--s-1) 0;border-bottom:var(--hairline) solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--text-dim)}.tab.on{color:var(--text);border-bottom-color:var(--text)}
.tab .c{color:var(--text-faint);margin-left:var(--s-1);font-family:var(--font-mono)}
.body{flex:1;overflow-y:auto;min-height:0}
.view{display:none}.view.on{display:block}
/* ── tables ─────────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;z-index:1;background:var(--bg);font-size:var(--t-micro);
line-height:var(--lh-micro);letter-spacing:var(--tracking-micro);text-transform:uppercase;
color:var(--text-dim);font-weight:var(--w-ui);text-align:right;padding:var(--s-2) var(--s-3);
border-bottom:var(--hairline) solid var(--border);white-space:nowrap}
thead th:first-child{text-align:left;padding-left:calc(var(--gutter) + var(--s-3))}
tbody td{height:var(--row-dense);padding:0 var(--s-3);text-align:right;white-space:nowrap;
border-bottom:var(--hairline) solid var(--border);font-size:var(--t-base)}
tbody td:first-child{text-align:left;padding-left:calc(var(--gutter) + var(--s-3))}
tbody tr{position:relative;transition:background var(--dur-fast) var(--ease)}
tbody tr:hover{background:var(--fill-hover)}
tbody tr td:first-child::before{content:"";position:absolute;left:0;top:0;bottom:0;width:var(--gutter);background:transparent}
tr[data-origin="agent"] td:first-child::before{background:var(--agent)}
tr[data-origin="agent-proposed"] td:first-child::before{background:color-mix(in oklab,var(--agent) 40%,transparent)}
tr[data-origin="pending"] td:first-child::before{background:var(--pending)}
@keyframes flash-up{from{background:var(--fill-up)}to{background:transparent}}
@keyframes flash-down{from{background:var(--fill-down)}to{background:transparent}}
.flash-up{animation:flash-up var(--dur-flash) var(--ease)}
.flash-down{animation:flash-down var(--dur-flash) var(--ease)}
.sym{font-weight:var(--w-ui);cursor:pointer}
.sym:hover{color:var(--focus)}
.lk{font-size:var(--t-micro);letter-spacing:.04em;color:var(--text-faint);text-decoration:none;
border:var(--hairline) solid var(--border);border-radius:var(--r-sm);padding:0 var(--s-1);margin-left:var(--s-1)}
.lk:hover{color:var(--focus);border-color:var(--focus)}
.lks{opacity:0;transition:opacity var(--dur-fast) var(--ease)}
tr:hover .lks{opacity:1}
.tag{height:18px;display:inline-flex;align-items:center;padding:0 var(--s-2);border-radius:var(--r-sm);
font-size:var(--t-xs);font-weight:var(--w-ui);background:var(--fill-hover);color:var(--text-dim)}
.tag.ok{background:color-mix(in oklab,var(--up) 16%,transparent);color:var(--up)}
.empty{padding:var(--s-6) var(--s-4);color:var(--text-faint);font-size:var(--t-sm)}
/* ── agent rail ─────────────────────────────────────────────────────────── */
.rail{width:var(--rail-w);flex-shrink:0;border-left:var(--hairline) solid var(--border);
background:var(--surface);display:flex;flex-direction:column}
.rh{height:var(--row-comfy);flex-shrink:0;display:flex;align-items:center;gap:var(--s-2);
padding:0 var(--s-4);border-bottom:var(--hairline) solid var(--border)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--agent)}
.trace{flex:1;overflow-y:auto;min-height:0}
@keyframes enter{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.tr{padding:var(--s-2) var(--s-4) var(--s-2) calc(var(--s-4) - var(--gutter));
border-bottom:var(--hairline) solid var(--border);border-left:var(--gutter) solid var(--agent);
animation:enter var(--dur-enter) var(--ease)}
.tr.skip{border-left-color:color-mix(in oklab,var(--agent) 40%,transparent)}
.tr .h{display:flex;align-items:baseline;gap:var(--s-2);margin-bottom:2px}
.tr .who{font-size:var(--t-xs);color:var(--agent);font-weight:var(--w-ui)}
.tr .act{font-size:var(--t-sm);font-weight:var(--w-ui)}
.tr .t{margin-left:auto;font-family:var(--font-mono);font-size:var(--t-micro);color:var(--text-faint)}
.tr .why{font-size:var(--t-xs);line-height:var(--lh-xs);color:var(--text-dim)}
.tr .meta{font-family:var(--font-mono);font-size:var(--t-micro);color:var(--text-faint);margin-top:2px}
.agents{flex-shrink:0;border-top:var(--hairline) solid var(--border)}
.ag{height:var(--row-default);display:flex;align-items:center;gap:var(--s-2);padding:0 var(--s-4);
font-size:var(--t-sm);color:var(--text-faint)}
.ag .s{width:6px;height:6px;border-radius:50%;background:var(--border-strong)}
.ag.on{color:var(--text)}.ag.on .s{background:var(--agent)}
.ag .st{margin-left:auto;font-size:var(--t-micro);letter-spacing:var(--tracking-micro);text-transform:uppercase}
/* ── wallets ────────────────────────────────────────────────────────────── */
.wv{padding:var(--s-4);max-width:640px}
textarea{width:100%;height:240px;background:var(--surface-raised);border:var(--hairline) solid var(--border);
border-radius:var(--r-sm);color:var(--text);padding:var(--s-3);font-family:var(--font-mono);
font-size:var(--t-sm);resize:vertical}
:where(a,button,input,textarea,[tabindex]):focus-visible{outline:var(--hairline) solid var(--focus);
outline-offset:1px;border-color:var(--focus)}
.status{height:var(--row-default);flex-shrink:0;display:flex;align-items:center;gap:var(--s-4);
padding:0 var(--s-4);border-top:var(--hairline) solid var(--border);background:var(--surface);
font-size:var(--t-xs);color:var(--text-faint)}
.status b{color:var(--text-dim);font-weight:var(--w-ui);font-family:var(--font-mono)}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition-duration:.01ms!important;
animation-duration:.01ms!important}.flash-up{animation:flash-up var(--dur-flash) var(--ease)!important}
.flash-down{animation:flash-down var(--dur-flash) var(--ease)!important}}
</style></head><body>
<nav>
 <svg class=mark viewBox="0 0 32 32" fill="none" aria-label="Cambrian"><path d="M5.5 10.5 L13 16 L5.5 21.5" stroke="var(--text)" stroke-width="2.8" stroke-linejoin="miter"/><path d="M15 6 L25.5 16 L15 26" stroke="var(--agent)" stroke-width="3.4" stroke-linejoin="miter"/></svg>
 <div class="ni on" data-v=desk title="Desk (1)">▤</div>
 <div class=ni data-v=scout title="Scouting (2)">◎</div>
 <div class=ni data-v=wallets title="Wallets (3)">◈</div>
</nav>
<main>
 <div class=topbar>
  <span class=wordmark>Cambrian</span>
  <span class=chip id=mode>Paper</span>
  <span class=tbmeta id=meta></span>
  <span class=grow></span>
  <span class=tbmeta>Equity</span>
  <span class="num" id=eqTop style="font-size:var(--t-md);font-weight:var(--w-ui)">$0.00</span>
  <button id=theme class=btn title="Palette — click to cycle (T)">Instrument</button>
  <button class="btn primary" onclick="alert('Wallet connect (Privy) arrives with live mode. Paper needs no wallet.')">Connect</button>
 </div>
 <div class=kpis>
  <div class="kpi hero"><div class=label-micro>Equity P&L</div><div class=v id=eq>$0.00</div></div>
  <div class=kpi><div class=label-micro>Realized</div><div class=v id=real>$0.00</div></div>
  <div class=kpi><div class=label-micro>Unrealized</div><div class=v id=unreal>$0.00</div></div>
  <div class=kpi><div class=label-micro>Open</div><div class=v id=open>0</div></div>
  <div class=kpi><div class=label-micro>Win rate</div><div class=v id=wr>—</div></div>
  <div class=kpi><div class=label-micro>Scouting</div><div class=v id=scn>0</div></div>
 </div>
 <div class=panes id=v-desk>
  <div class="pane fill">
   <div class=ph><span class=label-micro>Positions</span><span class="tbmeta num" id=posn></span></div>
   <div class=body><table><thead><tr><th>Instrument</th><th>Pad</th><th>Agent</th><th>Size</th>
    <th>Value</th><th>Δ</th><th>Unreal</th><th>Age</th></tr></thead><tbody id=positions></tbody></table></div>
  </div>
  <div class=pane style="height:38%">
   <div class=ph><span class="tab on" data-t=blo>Fills<span class="c num" id=cblo></span></span>
    <span class=tab data-t=clo>Closed<span class="c num" id=cclo></span></span></div>
   <div class=body>
    <div class="view on" id=p-blo><table><thead><tr><th>Time</th><th>Side</th><th>Instrument</th>
     <th>Agent</th><th>Notional</th><th>P&L</th></tr></thead><tbody id=blotter></tbody></table></div>
    <div class=view id=p-clo><table><thead><tr><th>Instrument</th><th>Pad</th><th>Cost</th><th>Exit</th>
     <th>P&L</th><th>Reason</th></tr></thead><tbody id=closed></tbody></table></div>
   </div>
  </div>
 </div>
 <div class=panes id=v-scout style=display:none>
  <div class="pane fill">
   <div class=ph><span class=label-micro>Scouting</span><span class=tbmeta>agent-proposed candidates</span></div>
   <div class=body><table><thead><tr><th>Instrument</th><th>Pad</th><th>Net flow</th><th>Liquidity</th>
    <th>Confluence</th></tr></thead><tbody id=scoutfull></tbody></table></div>
  </div>
 </div>
 <div class=panes id=v-wallets style=display:none>
  <div class="pane fill"><div class=ph><span class=label-micro>Wallet tracker</span>
   <span class=tbmeta>agent C feed</span></div>
   <div class=body><div class=wv>
    <div class=tbmeta style="margin-bottom:var(--s-2)">One address per line. Agent C copy-trades these once enabled.</div>
    <textarea id=wtext placeholder="0x…"></textarea>
    <div style="margin-top:var(--s-2);display:flex;gap:var(--s-2);align-items:center">
     <button class="btn primary" onclick=saveWallets()>Save</button>
     <span class=tbmeta id=wcount></span></div>
   </div></div>
  </div>
 </div>
 <div class=status><span>Block <b id=stblk>—</b></span><span>PM <b id=stpm>—</b></span>
  <span>Size <b id=stsz>—</b></span><span>ETH <b id=steth>—</b></span><span>Marked <b id=stmk>—</b></span>
  <span class=grow></span><span id=sterr></span><span>Robinhood Chain</span></div>
</main>
<aside class=rail>
 <div class=rh><span class=dot></span><span class=label-micro style=color:var(--agent)>Agent trace</span>
  <span class=grow></span><span class="tbmeta num" id=trn></span></div>
 <div class=trace id=trace></div>
 <div class=agents id=agents></div>
</aside>
<script>
const DEX=t=>`https://dexscreener.com/search?q=${t}`,EXP=t=>`https://robinhoodchain.blockscout.com/address/${t}`;
const PADURL={"pools-trade":"https://pools.trade/token/","flap":"https://flap.sh/token/",
"bankr":"https://bankr.bot/token/","pons":"https://pons.fun/token/"};
let PREV={};
function dur(s){if(s==null)return'—';s=Math.max(0,Math.floor(s));
 if(s<60)return s+'s';
 if(s<3600){const m=Math.floor(s/60),r=s%60;return m+'m'+String(r).padStart(2,'0')+'s'}
 const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h'+String(m).padStart(2,'0')+'m'}
const PAL=['instrument','graphite','void','ember','nocturne','daylight'];
function setTheme(v){if(v==='instrument')document.documentElement.removeAttribute('data-theme');
 else document.documentElement.setAttribute('data-theme',v);
 localStorage.cambrianPalette=v;
 document.getElementById('theme').textContent=v.charAt(0).toUpperCase()+v.slice(1)}
function nextTheme(){setTheme(PAL[(PAL.indexOf(localStorage.cambrianPalette||'instrument')+1)%PAL.length])}
setTheme(localStorage.cambrianPalette||'instrument');
document.getElementById('theme').onclick=nextTheme;
function d$(n){const s=n<0?'-':'',a=Math.abs(n);
 return s+'$'+(a>=1e6?(a/1e6).toFixed(2)+'M':a>=1e3?(a/1e3).toFixed(1)+'K':a.toFixed(2))}
function sgn(n){return n>0?'up':n<0?'down':'faint'}
function lks(a,pad){const p=(pad||'').replace('?','');
 return `<span class=lks><a class=lk href="${DEX(a)}" target=_blank>DEX</a>`+
 (PADURL[p]?`<a class=lk href="${PADURL[p]+a}" target=_blank>PAD</a>`:'')+
 `<a class=lk href="${EXP(a)}" target=_blank>SCAN</a></span>`}
function inst(sym,addr,pad){return `<span class=sym title="${addr} — click to copy" `+
 `onclick="navigator.clipboard.writeText('${addr}')">${sym||'—'}</span>${lks(addr,pad)}`}
function tag(p,v){return `<span class="tag ${v?'ok':''}">${p}</span>`}
function flash(id,val){const el=document.getElementById(id);if(!el)return;
 const p=PREV[id];if(p!==undefined&&p!==val){el.classList.remove('flash-up','flash-down');void el.offsetWidth;
 el.classList.add(val>p?'flash-up':'flash-down')}PREV[id]=val}
document.querySelectorAll('.ni').forEach(n=>n.onclick=()=>{
 document.querySelectorAll('.ni').forEach(x=>x.classList.remove('on'));n.classList.add('on');
 ['desk','scout','wallets'].forEach(v=>document.getElementById('v-'+v).style.display=v==n.dataset.v?'':'none')});
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
 document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));t.classList.add('on');
 document.querySelectorAll('.view').forEach(v=>v.classList.remove('on'));
 document.getElementById('p-'+t.dataset.t).classList.add('on')});
document.addEventListener('keydown',e=>{if(e.target.tagName=='TEXTAREA')return;
 if(e.key.toLowerCase()==='t'){nextTheme();return}
 const k={'1':'desk','2':'scout','3':'wallets'}[e.key];
 if(k)document.querySelector(`.ni[data-v=${k}]`).click()});
async function saveWallets(){
 const l=document.getElementById('wtext').value.split('\n').map(s=>s.trim()).filter(s=>/^0x[a-fA-F0-9]{40}$/.test(s));
 const d=await(await fetch('/wallets',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(l)})).json();
 document.getElementById('wcount').textContent=`${d.count} tracked`}
async function loadWallets(){const d=await(await fetch('/wallets')).json();
 document.getElementById('wtext').value=d.wallets.join('\n');
 document.getElementById('wcount').textContent=`${d.count} tracked`}
async function tick(){let d;try{d=await(await fetch('/data')).json()}catch(e){return}
 document.getElementById('meta').innerHTML=`<b>${d.mode}</b> <span class=sep>·</span> ${d.updated}`;
 const et=document.getElementById('eqTop');et.textContent=d$(d.equity);et.className='num '+sgn(d.equity);
 const eq=document.getElementById('eq');eq.textContent=d$(d.equity);eq.className='v '+sgn(d.equity);
 flash('eq',d.equity);
 const rl=document.getElementById('real');rl.textContent=d$(d.realized);rl.className='v '+sgn(d.realized);
 const ur=document.getElementById('unreal');ur.textContent=d$(d.unrealized);ur.className='v '+sgn(d.unrealized);
 flash('unreal',d.unrealized);
 document.getElementById('open').textContent=d.positions.length;
 const t=d.wins+d.losses;document.getElementById('wr').textContent=t?Math.round(100*d.wins/t)+'%':'—';
 document.getElementById('scn').textContent=d.scouting.length;
 document.getElementById('posn').textContent=d.positions.length;
 document.getElementById('cblo').textContent=d.blotter.length;
 document.getElementById('cclo').textContent=d.closed.length;
 document.getElementById('stblk').textContent=d.block;
 document.getElementById('stpm').textContent=d.model;
 document.getElementById('stsz').textContent='$'+d.size;
 document.getElementById('steth').textContent='$'+(d.eth||0).toLocaleString();
 document.getElementById('stmk').textContent=d.marked==null?'—':d.marked+'s ago';
 document.getElementById('sterr').innerHTML=d.err?`<span class=down>${d.err}</span>`:'';
 document.getElementById('positions').innerHTML=d.positions.length?d.positions.map(p=>
  `<tr data-origin=agent><td>${inst(p.sym,p.token,p.pad)}</td><td>${tag(p.pad,true)}</td>`+
  `<td class=dim>${p.agent}</td><td class=num>${d$(p.cost)}</td><td class=num>${d$(p.value)}</td>`+
  `<td class="num ${sgn(p.chg)}">${p.chg>0?'+':''}${p.chg}%</td>`+
  `<td class="num ${sgn(p.upnl)}">${d$(p.upnl)}</td><td class="num faint">${dur(p.age_s)}</td></tr>`).join(''):
  '<tr><td colspan=8 class=empty>No open positions. Agents are scanning.</td></tr>';
 document.getElementById('blotter').innerHTML=d.blotter.length?d.blotter.map(b=>
  `<tr data-origin=agent><td class="num faint">${b.t}</td>`+
  `<td class="${b.side=='BUY'?'up':'down'}">${b.side=='BUY'?'Buy':'Sell'}</td>`+
  `<td>${inst(b.sym,b.token,b.pad)}</td><td class=dim>${b.agent}</td>`+
  `<td class=num>${d$(b.usd)}</td>`+
  `<td class="num ${b.pnl==null?'faint':sgn(b.pnl)}">${b.pnl==null?'—':d$(b.pnl)}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>No fills.</td></tr>';
 document.getElementById('closed').innerHTML=d.closed.length?d.closed.map(c=>
  `<tr data-origin=agent><td>${inst(c.sym,c.token,c.pad)}</td><td>${tag(c.pad,true)}</td>`+
  `<td class=num>${d$(c.cost)}</td><td class=num>${d$(c.exit_value)}</td>`+
  `<td class="num ${sgn(c.pnl)}">${d$(c.pnl)}</td><td class=dim>${c.why}</td></tr>`).join(''):
  '<tr><td colspan=6 class=empty>No closed trades.</td></tr>';
 document.getElementById('scoutfull').innerHTML=d.scouting.length?d.scouting.map(s=>
  `<tr data-origin=agent-proposed><td>${inst(s.sym,s.token,s.pad)}</td><td>${tag(s.pad,s.verified)}</td>`+
  `<td class="num ${sgn(s.net)}">${d$(s.net)}</td><td class=num>${d$(s.liq)}</td>`+
  `<td class=num>${s.conf} <span class=faint>${s.tier}</span></td></tr>`).join(''):
  '<tr><td colspan=5 class=empty>Scanning Robinhood Chain.</td></tr>';
 // agent rail — reasoning trace
 document.getElementById('trn').textContent=(d.trace||[]).length;
 document.getElementById('trace').innerHTML=(d.trace||[]).length?d.trace.map(x=>
  `<div class="tr ${x.decision=='buy'?'':'skip'}"><div class=h>`+
  `<span class=who>${x.agent}</span>`+
  `<span class="act ${x.decision=='buy'?'up':'dim'}">${x.decision=='buy'?'Bought':'Passed'} ${x.sym}</span>`+
  `<span class=t>${x.t}</span></div>`+
  `<div class=why>${x.reason}</div>`+
  `<div class=meta>conf ${x.conf} · net ${d$(x.net)} · liq ${d$(x.liq)} · ${x.pad}</div></div>`).join(''):
  '<div class=empty>No agent decisions yet.</div>';
 document.getElementById('agents').innerHTML=d.agents.map(a=>
  `<div class="ag ${a.on?'on':''}"><span class=s></span>${a.name}<span class="st ${a.on?'':'faint'}">${a.on?'Live':'Idle'}</span></div>`).join('');
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
    threading.Thread(target=marker, daemon=True).start()
    print(f"\n  ◆ CAMBRIAN desk (paper) running  ->  http://localhost:{PORT}")
    print(f"    PM {STATE['model']}  ·  ${SIZE_USD}/trade  ·  stop -{STOP_PCT:.0%}  tp {TP_MULT}x  ·  "
          f"{len(WALLETS)} wallets tracked   (Ctrl+C to stop)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
