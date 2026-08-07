"""RH RUNNER WEB TERMINAL — a live multi-agent confluence dashboard in your browser.

Runs a tiny local web server (Python stdlib, no Flask). A background thread scans
Robinhood Chain, a team of specialist agents scores every fresh launch, a
confluence engine lights up only when they AGREE and safety passes, and an LLM PM
makes the final call. Your browser polls it and renders a live dashboard. DRY —
it decides and displays, buys nothing.

RUN:
    pip install requests
    python rh_web.py inf_YOURKEY      (Surplus key -> the PM; omit for confluence-only)
Then open the URL it prints (http://localhost:8787) in your browser. Ctrl+C to stop.
Env: RH_PORT, RH_LLM_MODEL, RH_INTERVAL, RH_BLOCKS, RH_ENRICH.
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

# ---- keccak -----------------------------------------------------------------
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


# ---- rpc + decode -----------------------------------------------------------
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


def honeypot_ok(token):
    try:
        b = fquote(token, WETH, 0.02, "buy")
        if "error" in b or not b.get("to"):
            return False
        tokens = b["to"]["amount"]
        usd_in = float(b["from"].get("notional") or 0)
        s = fquote(token, WETH, tokens, "sell")
        if "error" in s or not s.get("to"):
            return False
        usd_out = float(s["to"].get("notional") or 0)
        return (usd_out / usd_in) >= 0.75 if usd_in > 0 else False
    except Exception:
        return None


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


# ---- agents + confluence ----------------------------------------------------
def ag_flow(net, gross):
    if not gross or gross <= 0:
        return 0.0, "no vol"
    if net is None or net <= 0:
        return 0.0, "net out"
    return round(min(net / 5000.0, 1.0), 2), f"+${net:,.0f}"


def ag_sniper(sh):
    return round(max(0.0, 1.0 - min(max(sh, 0.0), 1.0)), 2), f"{sh:.0%} snp"


def ag_farm(fo):
    if fo is None:
        return 0.4, "fan?"
    return round(max(0.0, 1.0 - fo / 50.0), 2), f"fan{fo}"


def ag_mom(b, s):
    t = (b or 0) + (s or 0)
    if not t:
        return 0.0, "0 trd"
    return round(max((b / t - 0.5) * 2, 0.0), 2), f"{b/t:.0%} buy"


def ag_safety(verified, sellable):
    if verified:
        return 1.0, "pad verified"
    if sellable is True:
        return 0.8, "sim: sellable"
    if sellable is False:
        return 0.0, "HONEYPOT"
    return 0.3, "unchecked"


WEIGHTS = {"flow": 0.4, "sniper": 0.22, "farm": 0.22, "mom": 0.16}


def confluence(sig, safety):
    ws = sum(WEIGHTS.values())
    score = round(sum(sig[k] * WEIGHTS[k] for k in sig) / ws, 2)
    agree = sum(1 for v in sig.values() if v >= 0.5)
    if safety <= 0.0:
        return score, agree, "BLOCKED"
    if score >= 0.6 and agree >= 3:
        return score, agree, "STRONG"
    if score >= 0.4 and agree >= 2:
        return score, agree, "watch"
    return score, agree, "weak"


SYS = ("You are the PM of a memecoin sniping desk on Robinhood Chain. Analysts scored "
       "a fresh launch and it passed the honeypot gate. Given their signals, make the "
       "final call. Be strict. Reply ONLY JSON: "
       '{"decision":"buy"|"skip","confidence":0..1,"reason":"<=10 words"}.')


def pm(facts):
    if not LLM_KEY:
        return {"decision": "-", "confidence": 0, "reason": "confluence only"}
    body = {"model": LLM_MODEL, "temperature": 0, "max_tokens": 100, "stream": False,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": "\n".join(f"{k}: {v}" for k, v in facts.items())}]}
    try:
        r = requests.post(LLM_BASE.rstrip("/") + "/chat/completions",
                          headers={"Authorization": "Bearer " + LLM_KEY, "Content-Type": "application/json"},
                          json=body, timeout=40)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return {"decision": "skip", "confidence": 0, "reason": f"pm err:{e}"[:40]}
    s = txt.strip()
    if "{" in s and "}" in s:
        s = s[s.index("{"):s.rindex("}") + 1]
    try:
        d = json.loads(s)
    except Exception:
        return {"decision": "skip", "confidence": 0, "reason": "parse err"}
    dec = str(d.get("decision", "skip")).lower()
    return {"decision": "buy" if dec == "buy" else "skip",
            "confidence": d.get("confidence", 0), "reason": str(d.get("reason", ""))[:50]}


# ---- shared state + scan loop ----------------------------------------------
STATE = {"block": 0, "rows": [], "buys": [], "err": "", "updated": "starting...",
         "model": LLM_MODEL if LLM_KEY else "off (confluence only)"}


def scan_once():
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
    rows = []
    for ver, tok, pool, hook, blk, q0, txh in hits[:ENRICH]:
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
            m, liq = {"gross": 0, "net": 0, "buys": 0, "sells": 0, "snipe": 0}, None
        if liq is None or liq < MIN_LIQ or m["gross"] < MIN_GROSS:
            continue
        pad, verified = pad_and_verify(tok, hook, txh)
        sellable = True if verified else honeypot_ok(tok)
        fo = fanout(tok, life, pool)
        fs, fr = ag_flow(m["net"], m["gross"])
        ss, sr = ag_sniper(m["snipe"])
        ars, arr = ag_farm(fo)
        ms, mr = ag_mom(m["buys"], m["sells"])
        safs, safr = ag_safety(verified, sellable)
        conf, agree, tier = confluence({"flow": fs, "sniper": ss, "farm": ars, "mom": ms}, safs)
        age = round((now - bts(hex(blk))) / 60, 1) if now else None
        pmv = {"decision": "-", "confidence": 0, "reason": ""}
        if tier == "STRONG":
            pmv = pm({"pad": pad, "age_min": age, "liquidity_usd": round(liq),
                      "net_flow_usd": round(m["net"]), "sniper_share": round(m["snipe"], 2),
                      "fanout": fo, "buys": m["buys"], "sells": m["sells"],
                      "safety": safr, "confluence": conf})
            if pmv["decision"] == "buy":
                line = f"{time.strftime('%H:%M:%S')}  BUY {pad} {tok[:12]}  conf {conf} / pm {pmv['confidence']} - {pmv['reason']}"
                if line not in STATE["buys"]:
                    STATE["buys"] = (STATE["buys"] + [line])[-40:]
        rows.append({"pad": pad, "verified": verified, "tok": tok, "ver": ver,
                     "age": age, "liq": round(liq), "net": round(m["net"]),
                     "gross": round(m["gross"]), "buys": m["buys"], "sells": m["sells"],
                     "safety": [safs, safr], "flow": [fs, fr], "sniper": [ss, sr],
                     "farm": [ars, arr], "mom": [ms, mr], "conf": conf, "agree": agree,
                     "tier": tier, "pm": pmv})
    rows.sort(key=lambda r: -r["conf"])
    STATE.update(block=block, rows=rows, err="", updated=time.strftime("%H:%M:%S"))


def worker():
    while True:
        try:
            scan_once()
        except Exception as e:
            STATE.update(err=f"scan error: {e}", updated=time.strftime("%H:%M:%S"))
        time.sleep(INTERVAL)


# ---- web server -------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset=utf-8><title>RH Runner Terminal</title>
<style>
 body{background:#0a0e17;color:#c9d4e3;font:13px ui-monospace,Menlo,Consolas,monospace;margin:0}
 header{padding:10px 16px;background:#0d1320;border-bottom:1px solid #1c2740;position:sticky;top:0}
 h1{display:inline;font-size:15px;color:#5ad1ff;letter-spacing:1px;margin:0}
 .dry{color:#ffcf4d;font-weight:bold;margin-left:12px}
 .meta{color:#5f7191;margin-left:12px}
 table{width:100%;border-collapse:collapse}
 th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #131c30;white-space:nowrap}
 th{color:#7d8db0;font-weight:600;position:sticky;top:49px;background:#0a0e17}
 tr.STRONG{background:rgba(45,200,120,.10)}
 tr.BLOCKED{background:rgba(220,70,70,.12)}
 .tier-STRONG{color:#39d98a;font-weight:bold}
 .tier-BLOCKED{color:#ff6b6b;font-weight:bold}
 .tier-watch{color:#ffcf4d}.tier-weak{color:#5f7191}
 .buy{color:#39d98a;font-weight:bold}.padv{color:#5ad1ff}.pad{color:#8aa0c8}
 .tok{color:#8aa0c8}.pos{color:#39d98a}.neg{color:#ff6b6b}.hp{color:#ff6b6b;font-weight:bold}
 .sig{color:#9fb0cf}.d{color:#5f7191}
 #log{padding:8px 16px;border-top:1px solid #1c2740;background:#0d1320;max-height:180px;overflow:auto}
 #log div{color:#39d98a}
</style></head><body>
<header><h1>RH RUNNER TERMINAL</h1><span class=dry>DRY RUN</span>
<span class=meta id=meta></span></header>
<table><thead><tr>
<th>PAD</th><th>TOKEN</th><th>AGE</th><th>LIQ</th><th>NET</th><th>SAFETY</th>
<th>FLOW</th><th>SNIPER</th><th>FARM</th><th>MOM</th><th>CONF</th><th>PM</th></tr></thead>
<tbody id=rows></tbody></table>
<div id=log></div>
<script>
function fmt(n){return n>=1000||n<=-1000?'$'+(n/1000).toFixed(1)+'k':'$'+n}
function sig(a){return `<span class=sig>${a[0]}</span> <span class=d>${a[1]}</span>`}
async function tick(){
 let d; try{d=await (await fetch('/data')).json()}catch(e){return}
 document.getElementById('meta').innerHTML=
   `block ${d.block} &nbsp; PM ${d.model} &nbsp; runners ${d.rows.length} &nbsp; updated ${d.updated}`+
   (d.err?` &nbsp; <span class=hp>${d.err}</span>`:'');
 let h='';
 for(const r of d.rows){
  const net=`<span class="${r.net>=0?'pos':'neg'}">${(r.net>=0?'+':'')+fmt(r.net)}</span>`;
  const saf=r.safety[1]=='HONEYPOT'?`<span class=hp>HONEYPOT</span>`:sig(r.safety);
  const padc=r.verified?'padv':'pad';
  const pm=r.pm.decision=='buy'?`<span class=buy>BUY ${r.pm.confidence}</span>`:
        (r.pm.decision=='-'?'<span class=d>-</span>':`<span class=d>skip</span>`);
  h+=`<tr class=${r.tier}><td class=${padc}>${r.pad}</td>`+
     `<td class=tok title="${r.tok}">${r.tok.slice(0,12)}..</td>`+
     `<td>${r.age==null?'?':r.age+'m'}</td><td>${fmt(r.liq)}</td><td>${net}</td>`+
     `<td>${saf}</td><td>${sig(r.flow)}</td><td>${sig(r.sniper)}</td>`+
     `<td>${sig(r.farm)}</td><td>${sig(r.mom)}</td>`+
     `<td class=tier-${r.tier}>${r.conf} ${r.tier}</td><td>${pm}</td></tr>`;
 }
 document.getElementById('rows').innerHTML=h||'<tr><td colspan=12 class=d>scanning...</td></tr>';
 document.getElementById('log').innerHTML=d.buys.slice().reverse().map(b=>`<div>${b}</div>`).join('');
}
tick();setInterval(tick,4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/data"):
            body = json.dumps(STATE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    threading.Thread(target=worker, daemon=True).start()
    print(f"\n  RH Runner Terminal running.  ->  open  http://localhost:{PORT}  in your browser")
    print(f"  PM model: {STATE['model']}   (Ctrl+C to stop)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
