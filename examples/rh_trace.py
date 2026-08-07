"""Standalone: fingerprint a launchpad from ONE of its tokens on RH Chain.

Give it a token you know a pad launched (e.g. a bankr token) and it finds that
token's Uniswap pool(s), the v4 hook, and the launch transaction's `to` (the pad's
contract) and `from` (the launcher EOA). Those are the pad's on-chain fingerprint
— add the hook / deployer to the tracker's pad-label map and every future launch
from that pad gets tagged.

Run:  python rh_trace.py 0x<token>      (or set RH_TOKEN)
Only dependency is `requests`.
"""

import os
import sys
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
V4_PM = os.getenv("RH_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951").lower()
V3_FACTORY = os.getenv("RH_V3_FACTORY", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa").lower()
CHUNK = int(os.getenv("RH_CHUNK", "200000"))       # blocks per getLogs query
MAX_LOOKBACK = int(os.getenv("RH_LOOKBACK", "6000000"))  # how far back to hunt

INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

TOKEN = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RH_TOKEN", "")).lower()
if not TOKEN.startswith("0x") or len(TOKEN) != 42:
    raise SystemExit("usage: python rh_trace.py 0x<token-address>")
TTOPIC = "0x" + "0" * 24 + TOKEN[2:]


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=40)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def _addr(word):
    return "0x" + word[-40:]


def _words(data, n):
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def scan(addr, topic_sets):
    """Backward chunked getLogs across topic filter variants; return first hits."""
    latest = int(rpc("eth_blockNumber", []), 16)
    hi = latest
    found = []
    while hi > max(latest - MAX_LOOKBACK, 0) and not found:
        lo = max(hi - CHUNK, 0)
        for topics in topic_sets:
            try:
                logs = rpc("eth_getLogs", [{"address": addr, "fromBlock": hex(lo),
                                            "toBlock": hex(hi), "topics": topics}])
            except Exception:
                logs = []
            found.extend(logs)
        hi = lo - 1
    return found, latest


def tx_parties(log):
    try:
        tx = rpc("eth_getTransactionByHash", [log["transactionHash"]])
        return (tx.get("from"), tx.get("to"))
    except Exception:
        return (None, None)


print(f"tracing token {TOKEN}\nRPC {RPC}\n")

# v4: token as currency0 (topics[2]) or currency1 (topics[3])
v4, _ = scan(V4_PM, [[INITIALIZE, None, TTOPIC], [INITIALIZE, None, None, TTOPIC]])
for lg in v4:
    t, w = lg["topics"], _words(lg["data"], 5)
    frm, to = tx_parties(lg)
    print("[v4] Initialize")
    print(f"  pool_id   {t[1]}")
    print(f"  hook      {_addr(w[2])}   <-- pad fingerprint (v4)")
    print(f"  paired    {_addr(t[2])} / {_addr(t[3])}")
    print(f"  fee       {int(w[0], 16)}   block {int(lg['blockNumber'], 16)}")
    print(f"  launch tx from {frm}")
    print(f"           to   {to}   <-- pad contract (launcher/periphery)\n")

# v3: token as token0 (topics[1]) or token1 (topics[2])
v3, _ = scan(V3_FACTORY, [[POOLCREATED, TTOPIC], [POOLCREATED, None, TTOPIC]])
for lg in v3:
    t, data = lg["topics"], lg["data"][2:]
    frm, to = tx_parties(lg)
    print("[v3] PoolCreated")
    print(f"  pool      {_addr(data[64:128])}")
    print(f"  paired    {_addr(t[1])} / {_addr(t[2])}")
    print(f"  fee       {int(t[3], 16)}   block {int(lg['blockNumber'], 16)}")
    print(f"  launch tx from {frm}")
    print(f"           to   {to}   <-- pad contract (launcher/periphery)\n")

# mint: first Transfer from 0x0 -> who deployed / received the initial supply
mint, _ = scan(TOKEN, [[TRANSFER, "0x" + "0" * 64]])
if mint:
    mint.sort(key=lambda l: int(l["blockNumber"], 16))
    m0 = mint[0]
    frm, to = tx_parties(m0)
    print("[mint] first Transfer from 0x0")
    print(f"  to        {_addr(m0['topics'][2])}   <-- initial holder")
    print(f"  mint tx from {frm}   to {to}   block {int(m0['blockNumber'], 16)}")

if not (v4 or v3 or mint):
    print("nothing found — raise RH_LOOKBACK (default 6,000,000 blocks) and retry")
