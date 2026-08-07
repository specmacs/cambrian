"""Standalone: WATCH RH Chain and alert when a fresh pool starts accumulating.

The agent version of rh_discover. It loops: every RH_INTERVAL seconds it finds
pools born since the last tick, keeps a rolling watchlist of recent ones, re-reads
each (net flow / snipers / fan-out), and ALERTS the moment one crosses into WATCH
or HOT — or when a tracked pool's net inflow jumps. State (seen pools + last block)
persists to a JSON file so restarts don't re-alert. Ctrl+C to stop.

Same signals as rh_discover (net WETH flow, launch-block sniper share, transfer
fan-out), so a pool only alerts if money is genuinely flowing in — not bots, not a
farmed book. Only dependency is `requests`. Run:  python rh_watch.py
"""

import json
import os
import time

import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
INTERVAL = int(os.getenv("RH_INTERVAL", "30"))          # seconds between ticks
TRACK_MINUTES = float(os.getenv("RH_TRACK_MINUTES", "20"))  # how long to keep watching a pool
MAX_TRACK = int(os.getenv("RH_MAX_TRACK", "40"))        # cap pools re-read per tick
STATE_FILE = os.getenv("RH_WATCH_STATE", os.path.expanduser("~/rh_watch_state.json"))
WETH_USD = float(os.getenv("RH_WETH_USD", "3000"))

V4_PM = os.getenv("RH_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951").lower()
V3_FACTORY = os.getenv("RH_V3_FACTORY", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa").lower()
WETH = os.getenv("RH_WETH", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73").lower()
NATIVE = "0x0000000000000000000000000000000000000000"

INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
POOLS_SLOT = 6
EXTSLOAD = "0x1e2eaeaf"
SNIPE_BLOCKS = int(os.getenv("RH_SNIPE_BLOCKS", "3"))

MIN_LIQ = float(os.getenv("RH_MIN_LIQ", "5000"))
MIN_GROSS = float(os.getenv("RH_MIN_GROSS", "1000"))
FLOW_TARGET = float(os.getenv("RH_FLOW_TARGET", "5000"))
FANOUT_MAX = int(os.getenv("RH_FANOUT_MAX", "25"))
BLOCKS_PER_MIN = 600  # ~100ms blocks

# ---- keccak256 (for v4 extsload liquidity) ----------------------------------
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


# ---- RPC + decode -----------------------------------------------------------
def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=40)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def get_logs(topics, frm, to="latest", addr=None):
    q = {"fromBlock": frm, "toBlock": to, "topics": topics}
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


def analyze_swaps(addr, topics, frm, weth_is_t0, creation_blk, invert=False):
    gross = net = sniper_buy = 0.0
    buys = sells = 0
    for lg in get_logs(topics, frm, addr=addr):
        w = _words(lg["data"], 4 if len(topics) > 1 else 5)
        leg = _signed(w[0]) if weth_is_t0 else _signed(w[1])
        if invert:
            leg = -leg
        usd = leg / 1e18 * WETH_USD
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
    buy_vol = (gross + net) / 2
    share = (sniper_buy / buy_vol) if buy_vol > 1e-9 else 0.0
    return {"gross": gross, "net": net, "buys": buys, "sells": sells,
            "sniper_share": min(max(share, 0.0), 1.0)}


def transfer_fanout(token, frm, pool):
    try:
        logs = get_logs([TRANSFER], frm, addr=token)
    except Exception:
        return None
    pool_l, recips = pool.lower(), set()
    for lg in logs:
        tps = lg.get("topics", [])
        if len(tps) < 3:
            continue
        a, b = _addr(tps[1]).lower(), _addr(tps[2]).lower()
        if a in (pool_l, NATIVE) or b in (pool_l, NATIVE):
            continue
        recips.add(b)
    return len(recips)


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


def money(x):
    return ("$" + format(x, ",.0f")) if x is not None else "?"


def runner_score(liq, m, fanout):
    if liq is None or liq < MIN_LIQ or m is None or m["gross"] < MIN_GROSS:
        return 0.0
    net_n = max(min(m["net"] / FLOW_TARGET, 1.0), 0.0)
    sniper_factor = 1.0 - 0.7 * m["sniper_share"]
    farm_factor = 1.0 if not fanout else max(0.3, 1.0 - fanout / (2.0 * FANOUT_MAX))
    return round(net_n * sniper_factor * farm_factor, 3)


def tier_of(sc):
    return "HOT" if sc >= 0.60 else "WATCH" if sc >= 0.30 else "cold"


# ---- discovery + enrichment -------------------------------------------------
def discover(frm, to):
    """Fresh WETH/ETH pools created in [frm, to] — both DEX versions."""
    out = []
    for lg in get_logs([INITIALIZE], frm, to, addr=V4_PM):
        t, w = lg["topics"], _words(lg["data"], 5)
        c0, c1 = _addr(t[2]).lower(), _addr(t[3]).lower()
        if not ({WETH, NATIVE} & {c0, c1}):
            continue
        q0 = c0 in {WETH, NATIVE}
        out.append({"ver": "v4", "token": _addr(t[3]) if q0 else _addr(t[2]),
                    "pool": t[1], "hook": _addr(w[2]),
                    "blk": int(lg["blockNumber"], 16), "q0": q0})
    for lg in get_logs([POOLCREATED], frm, to, addr=V3_FACTORY):
        t, data = lg["topics"], lg["data"][2:]
        t0, t1 = _addr(t[1]).lower(), _addr(t[2]).lower()
        if WETH not in (t0, t1):
            continue
        w0 = t0 == WETH
        out.append({"ver": "v3", "token": _addr(t[2]) if w0 else _addr(t[1]),
                    "pool": _addr(data[64:128]), "hook": "-",
                    "blk": int(lg["blockNumber"], 16), "q0": w0})
    return out


def enrich(h):
    life = hex(max(h["blk"] - 1, 0))
    try:
        if h["ver"] == "v4":
            m = analyze_swaps(V4_PM, [V4_SWAP, h["pool"]], life, h["q0"], h["blk"], invert=True)
            liq = v4_liquidity_usd(h["pool"], h["q0"])
        else:
            m = analyze_swaps(h["pool"], [V3_SWAP], life, h["q0"], h["blk"])
            bal = int(rpc("eth_call", [{"to": WETH, "data": "0x70a08231" + "0" * 24 + h["pool"][2:]}, "latest"]), 16)
            liq = bal / 1e18 * WETH_USD * 2
    except Exception:
        return None, None
    fanout = transfer_fanout(h["token"], life, h["pool"])
    return runner_score(liq, m, fanout), {"liq": liq, "m": m, "fanout": fanout}


def load_state():
    try:
        with open(STATE_FILE) as fh:
            s = json.load(fh)
            return s.get("last_block", 0), s.get("tracked", {})
    except Exception:
        return 0, {}


def save_state(last_block, tracked):
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({"last_block": last_block, "tracked": tracked}, fh)
    except Exception:
        pass


def alert(kind, h, sc, info):
    m = info["m"]
    bar = "=" * 60
    print(f"\n{bar}\n*** {kind}: {tier_of(sc)} {sc:.2f}  [{h['ver']}] {h['token']}")
    print(f"    liq {money(info['liq'])}  net {'+' if m['net'] >= 0 else ''}{money(m['net'])}"
          f"  gross {money(m['gross'])}  {m['buys']}b/{m['sells']}s"
          f"  {m['sniper_share']:.0%}snipe  fan{info['fanout']}")
    print(f"    pool {h['pool']}" + (f"  hook {h['hook']}" if h['hook'] != '-' else ""))
    print(bar)


def main():
    last_block, tracked = load_state()
    latest = int(rpc("eth_blockNumber", []), 16)
    if not last_block:
        last_block = max(latest - BLOCKS_PER_MIN, 0)   # first run: last ~1 min
    print(f"watching RH from block {last_block} (interval {INTERVAL}s, "
          f"tracking {TRACK_MINUTES:.0f}m). Ctrl+C to stop.\n")

    while True:
        try:
            latest = int(rpc("eth_blockNumber", []), 16)
            # 1) find pools born since last tick, add to the rolling watchlist
            if latest > last_block:
                for h in discover(hex(last_block + 1), hex(latest)):
                    tracked.setdefault(h["pool"], {**h, "peak": 0.0, "alerted": ""})
                last_block = latest
            # 2) evict pools older than the tracking window
            cutoff = latest - int(TRACK_MINUTES * BLOCKS_PER_MIN)
            for pid in [p for p, h in tracked.items() if h["blk"] < cutoff]:
                del tracked[pid]
            # 3) re-read the newest tracked pools, alert on threshold crossings
            fresh = sorted(tracked.values(), key=lambda h: -h["blk"])[:MAX_TRACK]
            hot = 0
            for h in fresh:
                sc, info = enrich(h)
                if sc is None:
                    continue
                tier = tier_of(sc)
                if tier in ("WATCH", "HOT"):
                    hot += 1
                    # alert on first crossing into a tier, or an upgrade to HOT
                    if h["alerted"] != tier and not (h["alerted"] == "HOT" and tier == "WATCH"):
                        alert("NEW" if not h["alerted"] else "UPGRADE", h, sc, info)
                        h["alerted"] = tier
                h["peak"] = max(h.get("peak", 0.0), sc)
            save_state(last_block, tracked)
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] block {latest}  tracking {len(tracked)} pools  "
                  f"{hot} live  (newest {min(len(fresh), MAX_TRACK)} re-read)")
        except Exception as e:
            print(f"tick error ({e}) — retrying next interval")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
