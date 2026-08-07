"""Standalone: census EVERY launchpad on RH Chain, so you can name them all.

We only know bankr's fingerprint out of the box. This enumerates the rest: it
scans recent launches and clusters them three ways —
  * by v4 HOOK        (bankr and any other hooked pad)
  * by DEPLOYER       (the launch tx's `to` — the pad's contract; covers hookless
                       pads and v3 pads that a hook can't distinguish)
  * by ADDRESS SUFFIX (vanity endings like bankr's 'ba3')
and prints the ranked clusters with counts and sample tokens. Each big cluster is
a launchpad. Name them by dropping the fingerprints into an RH_PADS_FILE:

  {"hooks": {"0x..": "pons"}, "deployers": {"0x..": "noxa"}, "suffixes": {"xyz": "flap"}}

and every tool (rh_discover / rh_watch / the auto-trader) will label those pads.

  python rh_pads.py            # scan + cluster + print
Only dependency is requests.
"""

import os
import requests

RPC = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
BLOCKS = int(os.getenv("RH_BLOCKS", "40000"))     # how far back to census (~1hr)
MAX_TX = int(os.getenv("RH_MAX_TX", "400"))       # cap launch-tx lookups (deployer)
V4_PM = os.getenv("RH_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951").lower()
V3_FACTORY = os.getenv("RH_V3_FACTORY", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa").lower()
WETH = os.getenv("RH_WETH", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73").lower()
NATIVE = "0x0000000000000000000000000000000000000000"

INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# What we already know (extend via RH_PADS_FILE for the ones you name).
KNOWN_HOOKS = {"0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544": "bankr"}
KNOWN_DEPLOYERS = {}
KNOWN_SUFFIXES = {"ba3": "bankr"}
_pf = os.getenv("RH_PADS_FILE", "")
if _pf:
    try:
        import json
        with open(_pf) as fh:
            _d = json.load(fh)
        KNOWN_HOOKS.update({k.lower(): v for k, v in _d.get("hooks", {}).items()})
        KNOWN_DEPLOYERS.update({k.lower(): v for k, v in _d.get("deployers", {}).items()})
        KNOWN_SUFFIXES.update({k.lower(): v for k, v in _d.get("suffixes", {}).items()})
    except Exception:
        pass


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}, timeout=40)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def get_logs(topic, frm, addr):
    return rpc("eth_getLogs", [{"address": addr, "fromBlock": frm,
                                "toBlock": "latest", "topics": [topic]}])


def _addr(word):
    return "0x" + word[-40:]


def _words(data, n):
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def tx_to(txhash):
    try:
        return (rpc("eth_getTransactionByHash", [txhash]) or {}).get("to")
    except Exception:
        return None


latest = int(rpc("eth_blockNumber", []), 16)
frm = hex(max(latest - BLOCKS, 0))
print(f"census — block {latest}, scanning back {BLOCKS} blocks (~{BLOCKS*0.1/60:.0f} min)\n")

# Collect fresh WETH/ETH launches: (ver, token, hook, txhash)
launches = []
for lg in get_logs(INITIALIZE, frm, V4_PM):
    t, w = lg["topics"], _words(lg["data"], 5)
    c0, c1 = _addr(t[2]).lower(), _addr(t[3]).lower()
    if not ({WETH, NATIVE} & {c0, c1}):
        continue
    tok = _addr(t[3]) if c0 in {WETH, NATIVE} else _addr(t[2])
    launches.append(("v4", tok, _addr(w[2]).lower(), lg.get("transactionHash")))
for lg in get_logs(POOLCREATED, frm, V3_FACTORY):
    t = lg["topics"]
    t0, t1 = _addr(t[1]).lower(), _addr(t[2]).lower()
    if WETH not in (t0, t1):
        continue
    tok = _addr(t[2]) if t0 == WETH else _addr(t[1])
    launches.append(("v3", tok, "", lg.get("transactionHash")))

print(f"{len(launches)} fresh launches "
      f"({sum(1 for l in launches if l[0]=='v4')} v4, {sum(1 for l in launches if l[0]=='v3')} v3)\n")

# Cluster by hook, deployer (capped tx lookups, newest first), and suffix.
by_hook, by_dep, by_suf, samples = {}, {}, {}, {}
tx_budget = MAX_TX
for ver, tok, hook, txh in launches:
    if hook and hook != NATIVE:
        by_hook.setdefault(hook, []).append(tok)
    suf = tok[-3:].lower()
    by_suf.setdefault(suf, []).append(tok)
    if txh and tx_budget > 0:
        dep = (tx_to(txh) or "").lower()
        tx_budget -= 1
        if dep and dep not in (NATIVE, ""):
            by_dep.setdefault(dep, []).append(tok)


def show(title, clusters, known, minc):
    print(f"== {title} ==  (>= {minc} launches)")
    rows = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
    shown = 0
    for key, toks in rows:
        if len(toks) < minc:
            continue
        name = known.get(key)
        tag = f"  = {name}" if name else "  (UNNAMED — name it)"
        print(f"  {key}   {len(toks):>4} launches{tag}")
        print(f"       e.g. {', '.join(toks[:3])}")
        shown += 1
    if not shown:
        print("  (none above threshold)")
    print()


show("by v4 HOOK", by_hook, KNOWN_HOOKS, 2)
show("by DEPLOYER (launch-tx target)", by_dep, KNOWN_DEPLOYERS, 3)
# suffixes: only vanity ones are interesting (a random suffix hits ~1/4096)
show("by ADDRESS SUFFIX (vanity)", by_suf, KNOWN_SUFFIXES, 4)

print("Name the UNNAMED clusters in RH_PADS_FILE and every tool will label them:")
print('  {"hooks": {"0x..": "pons"}, "deployers": {"0x..": "noxa"}, "suffixes": {"xyz": "flap"}}')
print(f"(deployer lookups capped at {MAX_TX}; raise RH_MAX_TX to cluster more.)")
