"""Find a launchpad's official "token launched" event from one known token.

This is the primitive real snipers key off. A pad's launch event is emitted BY the
pad's own contract, and only that contract can emit logs at its own address — so
filtering eth_getLogs on (address = pad, topic0 = event) returns tokens that came
from that pad BY DEFINITION. No bytecode diffing, no address-suffix guessing, and
no way for an impostor to fake membership.

Our current discovery is inverted: we watch Uniswap for new pools and then infer
the pad. This finds the data needed to flip it — watch the pad, then find the pool.

Usage:
    python rh_padevent.py 0x<known_token_from_that_pad>

e.g. FRONG (pools.trade):  python rh_padevent.py 0x6245e67affA44a23077f0Ea7f981a8DC743a0c47

It locates the token's mint transaction, dumps every log in it, and highlights the
non-standard ones — the pad's own launch event will be among them. Only needs
requests.
"""

import os
import sys
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
LOOKBACK = int(os.getenv("RH_LOOKBACK", "4000000"))
CHUNK = int(os.getenv("RH_CHUNK", "400000"))

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO32 = "0x" + "0" * 64
# Events we already know, so the pad's own event stands out from the noise.
KNOWN = {
    TRANSFER: "ERC20 Transfer",
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925": "ERC20 Approval",
    "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438": "UniV4 Initialize",
    "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118": "UniV3 PoolCreated",
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f": "UniV4 Swap",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniV3 Swap",
    "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde": "UniV3 Mint",
    "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec": "UniV4 ModifyLiquidity",
}

tok = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RH_TOKEN", "")).strip()
if not tok.startswith("0x") or len(tok) != 42:
    raise SystemExit("usage: python rh_padevent.py 0x<known_pad_token>")


def rpc(method, params):
    d = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=45).json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


print("tracing %s\n" % tok)
latest = int(rpc("eth_blockNumber", []), 16)

# The mint (Transfer from 0x0) is emitted in the transaction that created the
# token, which is the same transaction the pad's launch event fires in.
mint = None
hi = latest
while hi > max(latest - LOOKBACK, 0) and not mint:
    lo = max(hi - CHUNK, 0)
    try:
        lg = rpc("eth_getLogs", [{"address": tok, "fromBlock": hex(lo), "toBlock": hex(hi),
                                  "topics": [TRANSFER, ZERO32]}])
    except Exception:
        lg = []
    if lg:
        lg.sort(key=lambda x: int(x["blockNumber"], 16))
        mint = lg[0]
    hi = lo - 1

if not mint:
    raise SystemExit("no mint found in the last %d blocks — raise RH_LOOKBACK" % LOOKBACK)

txh = mint["transactionHash"]
blk = int(mint["blockNumber"], 16)
print("mint tx   %s\nblock     %d" % (txh, blk))

tx = rpc("eth_getTransactionByHash", [txh]) or {}
print("tx.from   %s\ntx.to     %s   <- the contract the launcher CALLED\n"
      % (tx.get("from"), tx.get("to")))

rc = rpc("eth_getTransactionReceipt", [txh]) or {}
logs = rc.get("logs") or []
print("=" * 76)
print("%-44s %s" % ("EMITTED BY", "EVENT topic0"))
print("=" * 76)
unknown = []
for lg in logs:
    a = (lg.get("address") or "").lower()
    t0 = (lg.get("topics") or ["?"])[0]
    name = KNOWN.get(t0)
    tag = name or "*** UNKNOWN — candidate pad event ***"
    print("%-44s %s\n    %s" % (a, t0, tag))
    if not name:
        unknown.append((a, t0, lg))
print("=" * 76)

if unknown:
    print("\nCANDIDATE PAD LAUNCH EVENTS (%d):\n" % len(unknown))
    for a, t0, lg in unknown:
        print("  pad contract : %s" % a)
        print("  topic0       : %s" % t0)
        print("  indexed args : %d   data bytes: %d"
              % (len(lg.get("topics") or []) - 1, len(((lg.get("data") or "0x")[2:])) // 2))
        print("  -> verify it is the pad by counting how many OTHER tokens it launched:")
        print("     eth_getLogs address=%s topics=[%s]\n" % (a, t0))
else:
    print("\nOnly standard events here. The pad may emit its launch event in a "
          "separate transaction, or route through a factory that emits nothing — "
          "in that case fall back to tx.to above as the pad contract.\n")

print("Once we have (pad contract, topic0), discovery flips: we watch the PAD and\n"
      "get definitionally-verified tokens, instead of watching Uniswap and guessing.")
