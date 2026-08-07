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
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # ERC20 Transfer
POOLS_SLOT = 6
EXTSLOAD = "0x1e2eaeaf"  # extsload(bytes32)
SNIPE_BLOCKS = int(os.getenv("RH_SNIPE_BLOCKS", "3"))  # launch-block window = snipers

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


def analyze_swaps(addr, topics, frm, weth_is_t0, creation_blk, invert=False):
    """One pass over a pool's Swap logs -> the metrics that resist gaming.

    Counts are noise (1 big buy vs 10 tiny sells reads bearish but is flat), and
    snipers/routers make raw buy-count meaningless. So we compute:
      gross_usd  : total |WETH| traded (liveness)
      net_usd    : SIGNED WETH into the pool = accumulation(+) vs distribution(-).
                   This is the honest 'is money flowing in' number.
      sniper_share: fraction of BUY volume that landed in the first SNIPE_BLOCKS
                   after launch (bots in at block 0 -> dump-prone).
      buys/sells : kept for display only, NOT for scoring.
    invert=True for v4 (swapper-perspective amounts; see the sign fix).
    """
    gross = net = sniper_buy = 0.0
    buys = sells = 0
    for lg in get_logs(topics, frm, addr):
        w = _words(lg["data"], 4 if len(topics) > 1 else 5)  # v4 filtered vs v3
        leg = _signed(w[0]) if weth_is_t0 else _signed(w[1])
        if invert:
            leg = -leg
        usd = leg / 1e18 * WETH_USD           # signed: +ve = WETH into pool = a buy
        gross += abs(usd)
        net += usd
        if usd > 0:
            buys += 1
            blk = lg.get("blockNumber")
            blk = int(blk, 16) if isinstance(blk, str) else (blk or 0)
            if creation_blk and blk and blk <= creation_blk + SNIPE_BLOCKS:
                sniper_buy += usd
        elif usd < 0:
            sells += 1
    buy_vol = (gross + net) / 2               # positive half of the signed flow
    sniper_share = (sniper_buy / buy_vol) if buy_vol > 1e-9 else 0.0
    return {"gross": gross, "net": net, "buys": buys, "sells": sells,
            "sniper_share": min(max(sniper_share, 0.0), 1.0)}


def transfer_fanout(token, frm, pool):
    """Wallet-to-wallet ERC20 transfers (not trades) = the transfer-wallet tell.

    A deployer fanning tokens out to fresh wallets fakes holder count / distribution
    and preps a coordinated dump. Count Transfer logs whose from/to are neither the
    pool nor the zero address (mint/burn) — real off-market movement — and return how
    many DISTINCT recipients got tokens that way. High = a farmed/insider book.
    """
    try:
        logs = get_logs([TRANSFER], frm, token)
    except Exception:
        return None
    pool_l = pool.lower()
    recipients = set()
    for lg in logs:
        tps = lg.get("topics", [])
        if len(tps) < 3:
            continue
        frm_a, to_a = _addr(tps[1]).lower(), _addr(tps[2]).lower()
        if frm_a in (pool_l, NATIVE) or to_a in (pool_l, NATIVE):
            continue                          # skip trades (pool side) and mint/burn
        recipients.add(to_a)
    return len(recipients)


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


# Runner score from gaming-resistant signals, not trade counts:
#   NET flow  -> is WETH actually accumulating? (immune to 1-buy/10-sell noise)
#   SNIPER    -> was the early buying just launch-block bots? (discount it)
#   FAN-OUT   -> deployer transferring to fresh wallets? (farmed book, discount it)
# Fail closed: unknown liq or dead pool = not a runner. HOT = money flowing IN,
# organically, not sniped.
MIN_LIQ = float(os.getenv("RH_MIN_LIQ", "5000"))      # thinner than this = ignore
MIN_GROSS = float(os.getenv("RH_MIN_GROSS", "1000"))  # under this traded = not moving
FLOW_TARGET = float(os.getenv("RH_FLOW_TARGET", "5000"))  # net WETH-in that maxes score
FANOUT_MAX = int(os.getenv("RH_FANOUT_MAX", "25"))    # fresh-wallet fan-out cap


def runner_score(liq, m, fanout):
    """m = analyze_swaps dict. Returns (score, reasons[])."""
    if liq is None or liq < MIN_LIQ or m is None or m["gross"] < MIN_GROSS:
        return 0.0, []
    net_n = max(min(m["net"] / FLOW_TARGET, 1.0), 0.0)   # 0 if net is flat/negative
    sniper_factor = 1.0 - 0.7 * m["sniper_share"]        # fully sniped -> keep 30%
    farm_factor = 1.0 if not fanout else max(0.3, 1.0 - fanout / (2.0 * FANOUT_MAX))
    score = round(net_n * sniper_factor * farm_factor, 3)
    reasons = []
    if m["net"] <= 0:
        reasons.append("net OUT (distribution)")
    if m["sniper_share"] >= 0.5:
        reasons.append(f"{m['sniper_share']:.0%} sniped")
    if fanout and fanout >= FANOUT_MAX:
        reasons.append(f"{fanout} fan-out wallets")
    return score, reasons


scored = []
for h in hits[:ENRICH]:
    blk, q_is0 = h["blk"], h["q_is0"]
    life_from = hex(max(blk - 1, 0))          # from launch = this pool's whole life
    try:
        if h["ver"] == "v4":
            m = analyze_swaps(V4_PM, [V4_SWAP, h["pool"]], life_from, q_is0, blk, invert=True)
        else:
            m = analyze_swaps(h["pool"], [V3_SWAP], life_from, q_is0, blk)
    except Exception:
        m = None
    try:
        if h["ver"] == "v4":
            liq = v4_liquidity_usd(h["pool"], q_is0)
        else:
            bal = int(rpc("eth_call", [{"to": WETH, "data": "0x70a08231" + "0" * 24 + h["pool"][2:]}, "latest"]), 16)
            liq = bal / 1e18 * WETH_USD * 2
    except Exception:
        liq = None
    fanout = transfer_fanout(h["token"], life_from, h["pool"])
    try:
        age = (now - block_ts(hex(blk))) / 60 if now else None
    except Exception:
        age = None
    sc, reasons = runner_score(liq, m, fanout)
    tier = "HOT " if sc >= 0.60 else "WATCH" if sc >= 0.30 else "  .  "
    scored.append((sc, tier, h, age, liq, m, fanout, reasons))

# runners first (by net inflow score), sniped/farmed/dead sink to the bottom
scored.sort(key=lambda r: -r[0])
runners = sum(1 for r in scored if r[0] >= 0.30)
print(f"{runners} runner(s) of {len(scored)} deep-read (rest not accumulating / "
      f"sniped / farmed), ranked by NET inflow:\n")
for sc, tier, h, age, liq, m, fanout, reasons in scored:
    agestr = f"{age:5.0f}m" if age is not None else "   ?  "
    if m is not None:
        net = ("+" if m["net"] >= 0 else "") + money(m["net"])
        flow, snipe = f"{m['buys']}b/{m['sells']}s", f"{m['sniper_share']:.0%}snipe"
        gross = money(m["gross"])
    else:
        net = flow = snipe = gross = "?"
    fo = f"fan{fanout}" if fanout is not None else ""
    tail = ("  <- " + ", ".join(reasons)) if reasons else ""
    print(f"[{tier}] {sc:>5.2f}  [{h['ver']}] {h['token']}  age {agestr}  "
          f"liq {money(liq):>11}  net {net:>10}  gross {gross:>9}  "
          f"{flow:>9} {snipe:>8} {fo}{tail}")
    print(f"        pool {h['pool']}" + (f"  hook {h['hook']}" if h["hook"] != "-" else ""))

print("\nHOT = net WETH accumulating, not sniped, not farmed. Counts shown for "
      "context only — net flow + sniper + fan-out drive the score.")
if len(hits) > ENRICH:
    print(f"(+{len(hits) - ENRICH} older pools not deep-read — raise RH_ENRICH to see more)")
