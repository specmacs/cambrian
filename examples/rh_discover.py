"""Standalone: surface FRESH RH Chain runners (Uniswap v4 + v3). No repo needed.

Uses the chain-verified contracts from `rh_find_contracts.py`:
  v4 PoolManager  0x8366a39cc670b4001a1121b8f6a443a643e40951  (dominant on RH)
  v3 factory      0x1f7d7550b1b028f7571e69a784071f0205fd2efa

For each new WETH/ETH-paired pool in the window it decodes the token, pool,
hook (v4), age, 5-minute volume and a liquidity proxy, then prints them newest
first. Save anywhere and run:  python rh_discover.py

Only dependency is `requests`. This mirrors cambrian/runners/{feed,uniswap_v3,
uniswap_v4}.py; the scorer/tiering lives in the package — this is the live read.
"""

import os
import time
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = int(os.getenv("RH_BLOCKS", "20000"))          # how far back to scan
WINDOW = int(os.getenv("RH_WINDOW_BLOCKS", "3000"))    # ~5m volume window
WETH_USD = float(os.getenv("RH_WETH_USD", "3000"))

V4_PM = os.getenv("RH_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951").lower()
V3_FACTORY = os.getenv("RH_V3_FACTORY", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa").lower()
WETH = os.getenv("RH_WETH", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73").lower()
NATIVE = "0x0000000000000000000000000000000000000000"

INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
POOLS_SLOT = 6
EXTSLOAD = "0x1e2eaeaf"  # extsload(bytes32)

# ---- minimal pure-Python keccak256 (Ethereum, 0x01 padding) -----------------
_MASK = (1 << 64) - 1
_RC = [0x1, 0x8082, 0x800000000000808A, 0x8000000080008000, 0x808B, 0x80000001,
       0x8000000080008081, 0x8000000000008009, 0x8A, 0x88, 0x80008009, 0x8000000A,
       0x8000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x80000001, 0x8000000080008008]


def _rol(a, b):
    b %= 64
    return ((a << b) | (a >> (64 - b))) & _MASK


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


def keccak256(data):
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


# ---- RPC + decode helpers ---------------------------------------------------
def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=40)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def get_logs(topics, frm, addr=None):
    q = {"fromBlock": frm, "toBlock": "latest", "topics": topics}
    if addr:
        q["address"] = addr
    return rpc("eth_getLogs", [q])


def _addr(word):
    return "0x" + word[-40:]


def _signed(word):
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def _words(data, n):
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def block_ts(blk_hex):
    b = rpc("eth_getBlockByNumber", [blk_hex, False])
    return int(b["timestamp"], 16) if b else None


def swap_volume(addr, topics, frm, weth_is_t0):
    """Sum |WETH leg| over swaps in the window -> USD volume + buy/sell counts."""
    vol = buys = sells = 0.0
    for lg in get_logs(topics, frm, addr):
        w = _words(lg["data"], 4 if len(topics) > 1 else 5)  # v4 filtered vs v3
        a0, a1 = _signed(w[0]), _signed(w[1])
        leg = a0 if weth_is_t0 else a1
        vol += abs(leg) / 1e18 * WETH_USD
        if leg > 0:
            buys += 1
        elif leg < 0:
            sells += 1
    return vol, int(buys), int(sells)


def v4_liquidity_usd(pool_id, quote_is_t0):
    pid = bytes.fromhex(pool_id[2:])
    base = int.from_bytes(keccak256(pid.rjust(32, b"\x00") + POOLS_SLOT.to_bytes(32, "big")), "big")
    def load(slot):
        return int(rpc("eth_call", [{"to": V4_PM, "data": EXTSLOAD + slot.to_bytes(32, "big").hex()}, "latest"]), 16)
    slot0 = load(base)
    liq = load(base + 3) & ((1 << 128) - 1)
    sqrtp = slot0 & ((1 << 160) - 1)
    if not liq or not sqrtp:
        return None
    q = 1 << 96
    reserve = (liq * q / sqrtp) if quote_is_t0 else (liq * sqrtp / q)
    return reserve / 1e18 * WETH_USD * 2


# ---- discover (fast) then enrich (slow, newest first, with progress) --------
ENRICH = int(os.getenv("RH_ENRICH", "25"))   # how many newest pools to deep-read

latest = int(rpc("eth_blockNumber", []), 16)
now = block_ts("latest")
frm = hex(max(latest - BLOCKS, 0))
win = hex(max(latest - WINDOW, 0))
print(f"connected — block {latest}, scanning back {BLOCKS} blocks "
      f"(~{BLOCKS * 0.1 / 60:.0f} min of chain)\n")

# Phase 1: discovery — two log queries, fast. Just find the fresh pools.
hits = []
quotes = {WETH, NATIVE}
for lg in get_logs([INITIALIZE], frm, V4_PM):
    t, w = lg["topics"], _words(lg["data"], 5)
    c0, c1 = _addr(t[2]).lower(), _addr(t[3]).lower()
    if not (quotes & {c0, c1}):
        continue
    q_is0 = c0 in quotes
    hits.append({"ver": "v4", "token": _addr(t[3]) if q_is0 else _addr(t[2]),
                 "pool": t[1], "hook": _addr(w[2]), "blk": int(lg["blockNumber"], 16),
                 "q_is0": q_is0})
for lg in get_logs([POOLCREATED], frm, V3_FACTORY):
    t, data = lg["topics"], lg["data"][2:]
    t0, t1 = _addr(t[1]).lower(), _addr(t[2]).lower()
    if WETH not in (t0, t1):
        continue
    weth_is0 = t0 == WETH
    hits.append({"ver": "v3", "token": _addr(t[2]) if weth_is0 else _addr(t[1]),
                 "pool": _addr(data[64:128]), "hook": "-",
                 "blk": int(lg["blockNumber"], 16), "q_is0": weth_is0})

n4 = sum(1 for h in hits if h["ver"] == "v4")
print(f"discovered {len(hits)} fresh WETH/ETH pools ({n4} v4, {len(hits) - n4} v3). "
      f"deep-reading the {min(ENRICH, len(hits))} newest...\n")
if not hits:
    print("none in this window — raise RH_BLOCKS (e.g. $env:RH_BLOCKS='60000') and retry")
    raise SystemExit

# Phase 2: enrich newest-first, printing each as it lands so it never looks hung.
hits.sort(key=lambda h: -h["blk"])


def money(x):
    return f"${x:,.0f}" if x is not None else "?"


for i, h in enumerate(hits[:ENRICH], 1):
    blk, q_is0 = h["blk"], h["q_is0"]
    try:
        if h["ver"] == "v4":
            vol, buys, sells = swap_volume(V4_PM, [V4_SWAP, h["pool"]], win, q_is0)
        else:
            vol, buys, sells = swap_volume(h["pool"], [V3_SWAP], win, q_is0)
    except Exception:
        vol = buys = sells = None
    try:
        if h["ver"] == "v4":
            liq = v4_liquidity_usd(h["pool"], q_is0)
        else:
            bal = int(rpc("eth_call", [{"to": WETH, "data": "0x70a08231" + "0" * 24 + h["pool"][2:]}, "latest"]), 16)
            liq = bal / 1e18 * WETH_USD * 2
    except Exception:
        liq = None
    try:
        age = (now - block_ts(hex(blk))) / 60 if now else None
    except Exception:
        age = None
    agestr = f"{age:5.0f}m" if age is not None else "   ?  "
    flow = f"{buys}b/{sells}s" if buys is not None else "?"
    print(f"{i:>2}. [{h['ver']}] {h['token']}  blk {blk}  age {agestr}  "
          f"liq {money(liq):>12}  vol5m {money(vol):>10}  {flow}")
    print(f"      pool {h['pool']}" + (f"  hook {h['hook']}" if h["hook"] != "-" else ""))

print("\nfresh + rising vol + non-zero liq + skewed to buys = a runner starting.")
if len(hits) > ENRICH:
    print(f"(+{len(hits) - ENRICH} older pools not shown — raise RH_ENRICH to see more)")
