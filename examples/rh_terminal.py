"""RH RUNNER TERMINAL — a live multi-agent confluence dashboard for Robinhood Chain.

A team of specialist agents each judge every fresh launch (flow, snipers, farming,
momentum, safety/honeypot, pad-verification); a confluence engine only lights up a
runner when enough of them AGREE and safety passes; an LLM 'PM' makes the final
call. DRY — it decides and displays, it buys nothing.

RUN:
    pip install requests rich
    python rh_terminal.py inf_YOURKEY        (Surplus key -> the PM; omit to run
                                              confluence-only with no model)
Ctrl+C to quit. Env knobs: RH_LLM_MODEL, RH_INTERVAL, RH_BLOCKS, RH_ENRICH.
"""

import os
import sys
import time

import requests

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except Exception:
    raise SystemExit("need rich:  pip install rich")

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
LLM_BASE = os.getenv("RH_LLM_BASE", "https://api.surplusintelligence.ai/min30/v1")
LLM_MODEL = os.getenv("RH_LLM_MODEL", "claude-opus-4.7")
LLM_KEY = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RH_LLM_KEY", "")).strip()
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

# ---- keccak (v4 liquidity) --------------------------------------------------
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


def honeypot_check(token):
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
    """Returns (pad_label, verified_bool). Hook/deployer = authoritative (verified);
    suffix = a spoofable claim (unverified)."""
    h = (hook or "").lower()
    if h in PAD_BY_HOOK:
        return PAD_BY_HOOK[h], True
    dep = txto(txh) if txh else ""
    if dep in PAD_BY_DEPLOYER:
        return PAD_BY_DEPLOYER[dep], True
    for suf, name in PAD_BY_SUFFIX.items():
        if tok.lower().endswith(suf):
            return name + "?", False        # claims the pad, not proven
    if h and h != NAT:
        return "hook:" + h[2:8], False
    return "?", False


# ---- specialist agents (each a pro) -----------------------------------------
def ag_flow(net, gross):
    if not gross or gross <= 0:
        return 0.0, "no vol"
    if net is None or net <= 0:
        return 0.0, "net out"
    return round(min(net / 5000.0, 1.0), 2), f"+${net:,.0f}"


def ag_sniper(sh):
    return round(max(0.0, 1.0 - min(max(sh, 0.0), 1.0)), 2), f"{sh:.0%}snp"


def ag_farm(fo):
    if fo is None:
        return 0.4, "fan?"
    return round(max(0.0, 1.0 - fo / 50.0), 2), f"fan{fo}"


def ag_mom(b, s):
    t = (b or 0) + (s or 0)
    if not t:
        return 0.0, "0trd"
    return round(max((b / t - 0.5) * 2, 0.0), 2), f"{b/t:.0%}buy"


def ag_safety(verified, sellable):
    if verified:
        return 1.0, "pad✓"
    if sellable is True:
        return 0.8, "sim ok"
    if sellable is False:
        return 0.0, "HONEYPOT"
    return 0.3, "unchk"


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


# ---- LLM PM -----------------------------------------------------------------
SYS = ("You are the PM of a memecoin sniping desk on Robinhood Chain. Your analysts "
       "already scored a fresh launch and it passed the honeypot gate. Given their "
       "signals, make the final call. Be strict. Reply ONLY JSON: "
       '{"decision":"buy"|"skip","confidence":0..1,"reason":"<=10 words"}.')


def pm(facts):
    if not LLM_KEY:
        return {"decision": "-", "confidence": 0, "reason": "no PM (confluence only)"}
    import json
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
            "confidence": d.get("confidence", 0), "reason": str(d.get("reason", ""))[:44]}


# ---- render -----------------------------------------------------------------
CON = Console()


def color_tier(t):
    return {"STRONG": "bold green", "watch": "yellow", "weak": "dim",
            "BLOCKED": "bold red"}.get(t, "white")


def render(block, rows, buys, err):
    head = Text.assemble(
        ("RH RUNNER TERMINAL", "bold cyan"), ("   block ", "dim"), (str(block), "white"),
        ("   PM ", "dim"), (LLM_MODEL if LLM_KEY else "off", "white"),
        ("   runners ", "dim"), (str(len(rows)), "white"),
        ("   DRY RUN", "bold yellow"))
    t = Table(expand=True, header_style="bold")
    for col in ("PAD", "TOKEN", "AGE", "LIQ", "NET", "safety", "flow", "sniper",
                "farm", "mom", "CONF", "PM"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["pad"], r["tok"], r["age"], r["liq"], r["net"], r["safety"],
                  r["flow"], r["sniper"], r["farm"], r["mom"],
                  Text(f"{r['conf']} {r['tier']}", style=color_tier(r["tier"])),
                  Text(r["pm"], style="green" if r["pm"].startswith("buy") else "dim"))
    panels = [head, t]
    if buys:
        panels.append(Panel("\n".join(buys[-6:]), title="signals", border_style="green"))
    if err:
        panels.append(Text(err, style="red"))
    return Group(*panels)


def main():
    block = 0
    rows = []
    buys = []
    err = ""
    with Live(render(block, rows, buys, err), console=CON, refresh_per_second=2,
              screen=False) as live:
        while True:
            try:
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
                    hits.append(("v4", tok, tp[1], A(w[2]), int(lg["blockNumber"], 16), q0,
                                 lg.get("transactionHash")))
                for lg in logs([PC], frm, V3_FACTORY):
                    tp, data = lg["topics"], lg["data"][2:]
                    t0, t1 = A(tp[1]).lower(), A(tp[2]).lower()
                    if WETH not in (t0, t1):
                        continue
                    w0 = t0 == WETH
                    tok = A(tp[2]) if w0 else A(tp[1])
                    hits.append(("v3", tok, A(data[64:128]), "-", int(lg["blockNumber"], 16), w0,
                                 lg.get("transactionHash")))
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
                    sellable = True if verified else honeypot_check(tok)
                    fo = fanout(tok, life, pool)
                    fs, fr = ag_flow(m["net"], m["gross"])
                    ss, sr = ag_sniper(m["snipe"])
                    ars, arr = ag_farm(fo)
                    ms, mr = ag_mom(m["buys"], m["sells"])
                    safs, safr = ag_safety(verified, sellable)
                    conf, agree, tier = confluence({"flow": fs, "sniper": ss, "farm": ars, "mom": ms}, safs)
                    pmv = {"decision": "-", "confidence": 0, "reason": ""}
                    if tier == "STRONG":
                        pmv = pm({"pad": pad, "age_min": round((now - bts(hex(blk))) / 60, 1) if now else "?",
                                  "liquidity_usd": round(liq), "net_flow_usd": round(m["net"]),
                                  "sniper_share": round(m["snipe"], 2), "fanout": fo,
                                  "buys": m["buys"], "sells": m["sells"], "safety": safr, "confluence": conf})
                        if pmv["decision"] == "buy":
                            line = f"BUY {pad} {tok[:10]}.. conf {conf} pm {pmv['confidence']} - {pmv['reason']}"
                            if line not in buys:
                                buys.append(line)
                    age = f"{(now - bts(hex(blk)))/60:.0f}m" if now else "?"
                    rows.append({"pad": pad, "tok": tok[:10] + "..", "age": age,
                                 "liq": f"${liq/1000:.0f}k", "net": ("+" if m["net"] >= 0 else "") + f"${m['net']:,.0f}",
                                 "safety": safr, "flow": f"{fs} {fr}", "sniper": f"{ss} {sr}",
                                 "farm": f"{ars} {arr}", "mom": f"{ms} {mr}",
                                 "conf": conf, "tier": tier,
                                 "pm": (f"{pmv['decision']} {pmv['confidence']}" if pmv["decision"] != "-" else "-")})
                rows.sort(key=lambda r: -r["conf"])
                err = ""
            except Exception as e:
                err = f"tick error: {e}"
            live.update(render(block, rows, buys, err))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
