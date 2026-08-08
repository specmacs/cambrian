"""Discover EVERY launchpad's official "token launched" event, from recent activity.

Only a pad's own contract can emit logs at its own address, so (address, topic0)
is an unforgeable pad identity — that is what real snipers key off, and it is
strictly stronger than our current address-suffix guessing, which anyone can spoof
by grinding a vanity address.

Method: take the newest pool creations, pull each one's FULL transaction receipt,
discard the log signatures we already recognise (ERC-20, Uniswap v3/v4), and tally
whatever remains by (emitter, topic0). A pad that launched many tokens shows up as
a large cluster — that is its launch event.

Uses only recent blocks, so it is fast and works on brand-new tokens rather than
hunting an old flagship's mint far back in history.

    python rh_padscan.py            # scan and cluster
Only dependency is requests.
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = int(os.getenv("RH_BLOCKS", "12000"))     # ~20 min of RH
MAX_TX = int(os.getenv("RH_MAX_TX", "60"))        # receipts to inspect

V4_PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951".lower()
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa".lower()
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
PC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# Everything we can already name. Whatever is left over is pad-specific.
KNOWN = {
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef": "ERC20 Transfer",
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925": "ERC20 Approval",
    "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438": "UniV4 Initialize",
    "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118": "UniV3 PoolCreated",
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f": "UniV4 Swap",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniV3 Swap",
    "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde": "UniV3 Mint",
    "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec": "UniV4 ModifyLiquidity",
    "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c": "Permit2 Approval",
    "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65": "WETH Withdrawal",
    "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c": "WETH Deposit",
}

POOL = ThreadPoolExecutor(max_workers=10)


def rpc(method, params):
    d = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=45).json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def receipt(h):
    try:
        return rpc("eth_getTransactionReceipt", [h]) or {}
    except Exception:
        return {}


def tx(h):
    try:
        return rpc("eth_getTransactionByHash", [h]) or {}
    except Exception:
        return {}


latest = int(rpc("eth_blockNumber", []), 16)
frm = hex(max(latest - BLOCKS, 0))
print("pad-event scan — block %d, back %d blocks\n" % (latest, BLOCKS))

txs = []
for addr, topic, label in ((V4_PM, INIT, "v4"), (V3_FACTORY, PC, "v3")):
    try:
        for lg in rpc("eth_getLogs", [{"address": addr, "fromBlock": frm,
                                       "toBlock": "latest", "topics": [topic]}]):
            txs.append((int(lg["blockNumber"], 16), lg["transactionHash"], label))
    except Exception as e:
        print("  (%s scan failed: %s)" % (label, e))

seen, uniq = set(), []
for b, h, lab in sorted(txs, key=lambda x: -x[0]):
    if h in seen:
        continue
    seen.add(h)
    uniq.append((b, h, lab))
uniq = uniq[:MAX_TX]
print("%d recent launch transactions; pulling receipts...\n" % len(uniq))

rcs = list(POOL.map(lambda u: (u, receipt(u[1])), uniq))
callees = {}
clusters = {}
for (b, h, lab), rc in rcs:
    for lg in (rc.get("logs") or []):
        t0 = (lg.get("topics") or ["?"])[0]
        if t0 in KNOWN:
            continue
        a = (lg.get("address") or "").lower()
        k = (a, t0)
        c = clusters.setdefault(k, {"n": 0, "topics": len(lg.get("topics") or []) - 1,
                                    "data": len((lg.get("data") or "0x")[2:]) // 2,
                                    "tx": h})
        c["n"] += 1

# what contract did the launcher actually call? (the pad's entrypoint)
for t in POOL.map(lambda u: tx(u[0][1]), rcs[:30]):
    to = (t.get("to") or "").lower()
    if to:
        callees[to] = callees.get(to, 0) + 1

# A genuine launch event fires once per token, so it should appear across the
# whole window at roughly the launch rate — an admin/one-off event will not.
def window_count(k):
    a, t0 = k
    try:
        return len(rpc("eth_getLogs", [{"address": a, "fromBlock": frm,
                                        "toBlock": "latest", "topics": [t0]}]))
    except Exception:
        return -1


top = sorted(clusters.items(), key=lambda kv: -kv[1]["n"])[:14]
totals = list(POOL.map(lambda kv: window_count(kv[0]), top))

print("=" * 78)
print("CANDIDATE PAD LAUNCH EVENTS  (emitter + topic0, unforgeable pair)")
print("=" * 78)
if not clusters:
    print("  none — every log was a known standard event. The pads may emit their")
    print("  launch event in a separate tx; use the tx.to table below instead.")
for ((a, t0), c), tot in zip(top, totals):
    print("  in %3d of the sampled launches   contract %s" % (c["n"], a))
    print("      topic0 %s" % t0)
    print("      %d indexed args, %d data bytes" % (c["topics"], c["data"]))
    print("      fired %s times in the whole %d-block window\n"
          % (("%d" % tot) if tot >= 0 else "?", BLOCKS))

print("=" * 78)
print("CONTRACTS THE LAUNCHER CALLED  (tx.to — the pad's entrypoint)")
print("=" * 78)
for a, n in sorted(callees.items(), key=lambda kv: -kv[1])[:10]:
    print("  %3dx  %s" % (n, a))
print("""
Read it like this: a pad's launch event shows up in MANY of the sampled launches
AND fires a similar number of times across the whole window — one event, one token.
A contract that appears once, or fires thousands of times, is not a launch event.

Just paste the output. Each confirmed (contract, topic0) pair flips discovery from
"watch Uniswap and guess which pad made this" to "watch the pad itself" — which is
both safer (an impostor cannot emit logs at the pad's address) and earlier (the pad
fires before the pool is tradable).
""")
