"""Verified RH launches, straight from Uniswap's Liquidity Launcher.

Robinhood Chain launches route through Uniswap's Liquidity Launcher, whose
addresses and events come from Uniswap's own repos (not guessed):

  Liquidity Launcher (core, RH v3.2.0)  0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0
    event TokenCreated(address indexed token)
    event TokenDistributed(address indexed token, address indexed strategy, uint256 amount)
  ContinuousClearingAuctionFactory
    event AuctionCreated(address indexed auction, address indexed token,
                         uint256 amount, bytes configData)

Why this beats everything we had: a token appears in TokenCreated only if the
launcher itself emitted it, and only the launcher can emit logs at the launcher's
address. That is an EVM invariant, so membership cannot be spoofed by grinding a
vanity address. TokenDistributed then names the *strategy* the launch used, and
each pad runs its own strategy/fee-splitter pair — so the strategy address is the
pad's real, on-chain identity.

Two probes run CONCURRENTLY (the "probe the gateway in parallel" advice): the
chain scan above, and a probe of Uniswap's hosted gateway for the same tokens.
The gateway probe is exploratory — it prints exactly what each endpoint returns
so we can see whether the gateway indexes RH at all, rather than assuming it.

    python rh_launcher.py
Only dependency is requests.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = int(os.getenv("RH_BLOCKS", "40000"))
CHAIN_ID = 4663

LAUNCHER = "0x0000fffFbe8efe702c8703ae3477ff5de3d319c0"

TOKEN_CREATED = "0x2e2b3f61b70d2d131b2a807371103cc98d51adcaa5e9a8f9c32658ad8426e74e"
TOKEN_DISTRIBUTED = "0x67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8"
AUCTION_CREATED = "0x7ede475fad18ccf0039f2b956c4d43a8b4ed0853de4daaa8ae25299f331ae3b9"

# Strategy + periphery addresses published for Robinhood Chain. The strategy in
# TokenDistributed is the pad fingerprint; two InstantLaunch deployments exist
# with different fee splitters, which is very likely two different pads.
STRATEGY = {
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1": "InstantLaunch #1 (fee 0xeFF166..)",
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2": "InstantLaunch #2 (fee 0x222D6d..)",
    "0x05d552391067389ee44fec3924157ed33f976000": "LBPStrategy",
    "0x1242c9439d589cae85e121b1f79f2af51e91dcee": "UniversalRouterStrategy",
    "0x4f5e3fbb9745358a92da5674305fab8d2b8a73ce": "TokenSplitter",
}

POOL = ThreadPoolExecutor(max_workers=10)


def rpc(method, params):
    d = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=45).json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def logs(flt):
    try:
        return rpc("eth_getLogs", [flt])
    except Exception as e:
        print("  (getLogs failed: %s)" % e)
        return []


def addr(word):
    return "0x" + word[-40:]


def symbol(t):
    try:
        raw = rpc("eth_call", [{"to": t, "data": "0x95d89b41"}, "latest"])
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:
            n = int.from_bytes(b[32:64], "big")
            s = b[64:64 + n].decode("utf-8", "ignore")
        else:
            s = b.rstrip(b"\x00").decode("utf-8", "ignore")
        return "".join(c for c in s if c.isprintable())[:12] or "?"
    except Exception:
        return "?"


# --------------------------------------------------------------------------
# Gateway probe. Runs alongside the chain scan. Nothing here is assumed to
# work — each attempt reports its real status so we learn the true shape.
# --------------------------------------------------------------------------
GQL = "https://interface.gateway.uniswap.org/v1/graphql"
TRADE = "https://trade-api.gateway.uniswap.org/v1"
HDRS = {"content-type": "application/json", "origin": "https://app.uniswap.org"}

TOKEN_Q = """query T($chain: Chain!, $address: String!) {
  token(chain: $chain, address: $address) {
    symbol name decimals
    project { name isSpam safetyLevel }
  }
}"""


def gateway_probe(tokens):
    """Ask Uniswap's gateway what it knows about these tokens. Returns a list of
    (label, status, body-snippet) so the output is evidence, not a claim."""
    out = []
    if not tokens:
        return out
    tok = tokens[0]
    for chain in ("ROBINHOOD", "ROBINHOOD_CHAIN", "ETHEREUM"):
        try:
            r = requests.post(GQL, headers=HDRS, timeout=20, json={
                "query": TOKEN_Q, "variables": {"chain": chain, "address": tok}})
            out.append(("graphql token chain=%s" % chain, r.status_code, r.text[:400]))
        except Exception as e:
            out.append(("graphql token chain=%s" % chain, "ERR", str(e)[:200]))
    try:
        r = requests.get("%s/check_approval" % TRADE, headers=HDRS, timeout=20,
                         params={"chainId": CHAIN_ID, "token": tok, "amount": "1",
                                 "walletAddress": "0x" + "0" * 39 + "1"})
        out.append(("trade-api check_approval", r.status_code, r.text[:300]))
    except Exception as e:
        out.append(("trade-api check_approval", "ERR", str(e)[:200]))
    return out


# --------------------------------------------------------------------------
latest = int(rpc("eth_blockNumber", []), 16)
frm = hex(max(latest - BLOCKS, 0))
print("launcher scan — block %d, back %d blocks\n" % (latest, BLOCKS))

created = logs({"address": LAUNCHER, "fromBlock": frm, "toBlock": "latest",
                "topics": [TOKEN_CREATED]})
dist = logs({"address": LAUNCHER, "fromBlock": frm, "toBlock": "latest",
             "topics": [TOKEN_DISTRIBUTED]})
# No address filter: whatever emits AuctionCreated on RH IS the CCA factory here.
auctions = logs({"fromBlock": frm, "toBlock": "latest", "topics": [AUCTION_CREATED]})

tokens = []
seen = set()
for lg in created:
    t = addr(lg["topics"][1]).lower()
    if t not in seen:
        seen.add(t)
        tokens.append((t, int(lg["blockNumber"], 16)))

# gateway probe launches NOW and resolves after the chain work below
gw = POOL.submit(gateway_probe, [t for t, _ in tokens][:1])

by_token = {}
for lg in dist:
    t = addr(lg["topics"][1]).lower()
    by_token.setdefault(t, []).append(addr(lg["topics"][2]).lower())

print("TokenCreated   %d  (verified launches in window)" % len(created))
print("TokenDistributed %d" % len(dist))
print("AuctionCreated %d\n" % len(auctions))

if auctions:
    facs = {}
    for lg in auctions:
        facs[lg["address"].lower()] = facs.get(lg["address"].lower(), 0) + 1
    print("CCA factory on RH (self-discovered — it emitted AuctionCreated):")
    for a, n in sorted(facs.items(), key=lambda kv: -kv[1]):
        print("   %s   %d auctions" % (a, n))
    print()

print("=" * 78)
print("PAD IDENTITY BY STRATEGY  (TokenDistributed.strategy — unspoofable)")
print("=" * 78)
tally = {}
for t, _ in tokens:
    for s in by_token.get(t, []) or ["(no TokenDistributed in window)"]:
        tally.setdefault(s, []).append(t)
for s, ts in sorted(tally.items(), key=lambda kv: -len(kv[1])):
    print("  %4d launches   %s" % (len(ts), STRATEGY.get(s, s)))
    if s not in STRATEGY and s.startswith("0x"):
        print("                 UNKNOWN strategy %s  <- name this, it is a pad" % s)

print("\n" + "=" * 78)
print("%-44s %-10s %s" % ("NEWEST VERIFIED TOKENS", "SYMBOL", "STRATEGY"))
print("=" * 78)
recent = sorted(tokens, key=lambda x: -x[1])[:20]
syms = list(POOL.map(lambda x: symbol(x[0]), recent))
for (t, b), sy in zip(recent, syms):
    ss = by_token.get(t, [])
    name = STRATEGY.get(ss[0], ss[0][:12] + "..") if ss else "-"
    print("%-44s %-10s %s" % (t, sy, name))

print("\n" + "=" * 78)
print("UNISWAP GATEWAY PROBE  (ran in parallel with the chain scan)")
print("=" * 78)
for label, status, body in gw.result():
    print("  %-32s %s" % (label, status))
    print("      %s" % body.replace("\n", " ")[:300])

print("""
What to read here:
  * TokenCreated count > 0 proves the launcher is the RH launch path, and every
    token in that list is verified BY CONSTRUCTION — no suffix guessing.
  * Each distinct strategy address is one pad. Name them and the desk can refuse
    anything that did not come from a named strategy.
  * The gateway lines show whether Uniswap's hosted API indexes RH; a 200 with
    real token data means we get safetyLevel/isSpam as a second, independent
    opinion, and a 4xx means the on-chain path is all we get (which is fine —
    it is the stronger of the two anyway).
""")
