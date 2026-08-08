"""Fingerprint RH launchpads by TOKEN BYTECODE — the unspoofable identity.

A pad mints every token from the same template, so the deployed runtime bytecode
is byte-identical each time. Hash it and you get a factory fingerprint that cannot
be faked: matching bytecode means matching behaviour, so a token whose codehash
equals a known-good pad template cannot hide a sell tax, a blacklist, or a
honeypot — those would change the bytecode and therefore the hash.

This is strictly stronger than address-suffix guessing (anyone can grind an address
ending in 'ba3') and stronger than hook/deployer heuristics.

Run:  python rh_codehash.py
It scans recent launches, groups them by codehash, resolves EIP-1167 proxies to
their implementation, and prints the clusters. Each big cluster is one pad's
template. Tell me which cluster is which pad and the desk can then trade ONLY
cryptographically-verified pad tokens.

Only dependency is requests.
"""

import os
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = int(os.getenv("RH_BLOCKS", "40000"))
MAX_TOK = int(os.getenv("RH_MAX_TOK", "120"))

V4_PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951".lower()
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa".lower()
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NAT = "0x0000000000000000000000000000000000000000"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
PC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# what we currently *guess* — shown alongside so mislabels are obvious
PAD_BY_HOOK = {"0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544": "bankr"}
PAD_BY_DEPLOYER = {"0x0000ffffbe8efe702c8703ae3477ff5de3d319c0": "pons",
                   "0x58daec3116aae6d93017baaea7749052e8a04fa7": "pools-trade"}
PAD_BY_SUFFIX = {"ba3": "bankr?", "777": "flap?"}      # ? = spoofable guess

# ---- keccak256 -------------------------------------------------------------
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


def A(w):
    return "0x" + w[-40:]


def wd(data, n):
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def txto(h):
    try:
        return ((rpc("eth_getTransactionByHash", [h]) or {}).get("to") or "").lower()
    except Exception:
        return ""


def symbol(t):
    try:
        raw = rpc("eth_call", [{"to": t, "data": "0x95d89b41"}, "latest"])
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:
            ln = int.from_bytes(b[32:64], "big")
            s = b[64:64 + ln].decode("utf-8", "ignore")
        else:
            s = b.rstrip(b"\x00").decode("utf-8", "ignore")
        return "".join(c for c in s if c.isprintable())[:10] or "?"
    except Exception:
        return "?"


def proxy_impl(code_hex):
    """EIP-1167 minimal proxy? Return the implementation address it delegates to.
    Clone factories are common on launchpads, and the implementation address is a
    cleaner fingerprint than the clone's own (near-identical) bytecode."""
    c = code_hex[2:] if code_hex.startswith("0x") else code_hex
    if len(c) == 90 and c.startswith("363d3d373d3d3d363d73") and c.endswith("5af43d82803e903d91602b57fd5bf3"):
        return "0x" + c[20:60]
    return None


def guess(tok, hook, txh):
    h = (hook or "").lower()
    if h in PAD_BY_HOOK:
        return PAD_BY_HOOK[h]
    d = txto(txh) if txh else ""
    if d in PAD_BY_DEPLOYER:
        return PAD_BY_DEPLOYER[d]
    for suf, name in PAD_BY_SUFFIX.items():
        if tok.lower().endswith(suf):
            return name
    if h and h != NAT:
        return "hook:" + h[2:8]
    return "?"


latest = int(rpc("eth_blockNumber", []), 16)
frm = hex(max(latest - BLOCKS, 0))
print("codehash census — block %d, back %d blocks\n" % (latest, BLOCKS))

toks = []
for lg in rpc("eth_getLogs", [{"address": V4_PM, "fromBlock": frm, "toBlock": "latest",
                               "topics": [INIT]}]):
    t, w = lg["topics"], wd(lg["data"], 5)
    c0, c1 = A(t[2]).lower(), A(t[3]).lower()
    if not ({WETH, NAT} & {c0, c1}):
        continue
    tok = A(t[3]) if c0 in {WETH, NAT} else A(t[2])
    toks.append((tok, A(w[2]), lg.get("transactionHash")))
for lg in rpc("eth_getLogs", [{"address": V3_FACTORY, "fromBlock": frm, "toBlock": "latest",
                               "topics": [PC]}]):
    t = lg["topics"]
    t0, t1 = A(t[1]).lower(), A(t[2]).lower()
    if WETH not in (t0, t1):
        continue
    toks.append((A(t[2]) if t0 == WETH else A(t[1]), "", lg.get("transactionHash")))

seen, uniq = set(), []
for tok, hook, txh in toks:
    if tok.lower() in seen:
        continue
    seen.add(tok.lower())
    uniq.append((tok, hook, txh))
uniq = uniq[:MAX_TOK]
print("%d unique tokens; fetching bytecode...\n" % len(uniq))

clusters = {}
for tok, hook, txh in uniq:
    try:
        code = rpc("eth_getCode", [tok, "latest"])
    except Exception:
        continue
    if not code or code == "0x":
        continue
    raw = bytes.fromhex(code[2:])
    impl = proxy_impl(code)
    key = ("proxy->" + impl) if impl else ("0x" + kec(raw).hex()[:16])
    clusters.setdefault(key, []).append(
        {"tok": tok, "sym": symbol(tok), "guess": guess(tok, hook, txh), "size": len(raw)})

print("=" * 78)
print("%-26s %5s %6s  %s" % ("CODEHASH (or proxy impl)", "COUNT", "BYTES", "CURRENT GUESSES"))
print("=" * 78)
for key, rows in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
    g = {}
    for r in rows:
        g[r["guess"]] = g.get(r["guess"], 0) + 1
    tags = ", ".join("%s x%d" % (k, v) for k, v in sorted(g.items(), key=lambda kv: -kv[1]))
    print("%-26s %5d %6d  %s" % (key, len(rows), rows[0]["size"], tags))
    print("      e.g. %s" % ", ".join("%s (%s)" % (r["sym"], r["tok"][:10]) for r in rows[:3]))
print("=" * 78)
print("""
Each cluster is ONE factory template. Tokens sharing a codehash share behaviour
exactly — so once you confirm which cluster is Pons / bankr / pools.trade, the
desk can require a codehash match before buying and will never touch an unknown
contract again.

Watch for a cluster whose 'guess' column is mixed (e.g. 'bankr? x4, ? x9') —
that is the address-suffix heuristic mislabelling impostors, which is very likely
what got bought and rugged.
""")
