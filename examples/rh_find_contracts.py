"""Standalone: find RH Chain's Uniswap V3 factory + v4 PoolManager. No repo needed.

Every V3 `PoolCreated` log is emitted by the factory and every v4 `Initialize`
log by the PoolManager, so we scan those event signatures and see who emitted
them. Save this file anywhere and run:  python rh_find_contracts.py
"""

import os
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = 8000  # how far back to scan; increase if nothing is found

POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise SystemExit(f"RPC error: {d['error']}")
    return d["result"]


latest = int(rpc("eth_blockNumber", []), 16)
frm = hex(max(latest - BLOCKS, 0))
print(f"connected — block {latest}, scanning back {BLOCKS} blocks\n")

for name, topic in (("V3 factory  (PoolCreated)", POOLCREATED),
                    ("v4 PoolManager (Initialize)", INITIALIZE)):
    try:
        logs = rpc("eth_getLogs", [{"fromBlock": frm, "toBlock": "latest",
                                    "topics": [topic]}])
    except Exception as e:
        print(f"{name}: getLogs failed ({e}) — lower BLOCKS at top of file\n")
        continue
    counts = {}
    for lg in logs:
        a = (lg.get("address") or "").lower()
        if a:
            counts[a] = counts.get(a, 0) + 1
    print(name + ":")
    if not counts:
        print("  none in this window — increase BLOCKS at top of file")
    for a, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {a}   ({n} events)")
    print()
