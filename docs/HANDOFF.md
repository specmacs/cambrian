# CAMBRIAN — handoff

Everything a fresh session needs to pick this up without re-deriving it. Written
because chat context does not survive a new session, but this file does.

Read this, then `cambrian/runners/config.py` (the comments there carry the
chain-verified facts) and `examples/cambrian_desk.py` (the desk itself).

---

## What this is

An autonomous multi-agent memecoin trading desk for **Robinhood Chain**
(chainId **4663**, an Arbitrum Orbit L2, ~100ms blocks).

RPC   `https://rpc.mainnet.chain.robinhood.com`
Explorer `https://robinhoodchain.blockscout.com`

The desk scans fresh launches, scores them, paper-trades them, and renders a
live web terminal. The owner watches; the agents decide.

**Speed is the stated edge.** The owner's words: "Literally EVERYTHING should
tick live... this is a game of edge, and our edge is speed." Do not add
latency to the hot path without saying so.

### The agent fleet (owner's design, partially built)

| Agent | Job | State |
| --- | --- | --- |
| A | Snipe fresh launches | built (`cambrian/runners/agents.py`) |
| B | Hunt volume across all of RH, buy into it | **not built** |
| C | Track ~500 wallets, buy on confluence | partial (WATCHED_WALLETS empty) |
| D | Information — X/Twitter scraping | not built |

---

## Chain facts — verified, do not re-derive

All confirmed by live scan at block 31,247,303. Addresses lowercased.

### Uniswap infrastructure on RH

```
v4 PoolManager   0x8366a39cc670b4001a1121b8f6a443a643e40951
v3 Factory       0x1f7d7550b1b028f7571e69a784071f0205fd2efa   (NOT the canonical one)
WETH             0x0bd7d308f8e1639fab988df18a8011f41eacad73
CCA Factory      0x000000001f26a0044baa66024e7b6599c61963f8
```

The CCA factory was **self-discovered**: filter `AuctionCreated` with no address
filter, and whatever emitted it is the factory. Reuse that trick.

### Launch path — the important part

**TWO** Liquidity Launcher deployments are live and both still emit. Watching
only one silently loses launches.

```
v3.2.0   0x0000fffFbe8efe702c8703ae3477ff5de3d319c0    330 dists / 40k blocks
v3.0.0   0x00004c4ccc709ef590f7c81102c0689f0263d4e9      7 dists / 40k blocks
```

Event topic0s (keccak of the signatures declared in Uniswap's repos):

```
TokenCreated(address)                              0x2e2b3f61b70d2d131b2a807371103cc98d51adcaa5e9a8f9c32658ad8426e74e
TokenDistributed(address,address,uint256)          0x67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8
AuctionCreated(address,address,uint256,bytes)      0x7ede475fad18ccf0039f2b956c4d43a8b4ed0853de4daaa8ae25299f331ae3b9
DistributionInitialized(address,address,uint256)   0x0afd26d7f0833a451173acef122d058906aa7708ceb6f67ea7471a649d88b44b
```

**The Uniswap launcher has no `TokenLaunched` event.** The full event list was
read from a clone of `Uniswap/liquidity-launcher`, and 111,110 candidate
signatures were brute-forced against the real topic0s in FRONG's launch tx with
no match. On the *official pad* the launch is `TokenCreated` + `TokenDistributed`.

⚠️ **But a literal `TokenLaunched` does exist — on Pons.** An earlier version of
this file generalised the finding above into "do not go hunting for a literal
`TokenLaunched` again", which was wrong and nearly buried a real pad. Pons's own
factory emits one; see *Independent pads* below. The sniper contact's phrase was
accurate, it just named an event on a pad we had not identified yet. The lesson
is narrow-scope your negative results: "not in Uniswap's launcher" is not "not on
this chain".

### Why this beats what we had

Only the launcher can emit logs at the launcher's own address. That is an EVM
invariant, so membership in `TokenCreated` cannot be forged. The old heuristic
was an address suffix (`ba3`, `777`), which anyone can spoof with a vanity
grind. **The suffix map is kept only as a last-resort hint and must never gate
a buy.**

### Pad identity = `TokenDistributed.strategy`

Each pad runs its own strategy + fee-splitter pair.

```
0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491   pools-trade   CONFIRMED via FRONG
0x23f8209572b4a1c2ad88a42749e830791fb027f1   instant-launch-1 (fee 0xeFF166..)  212/window
0x1242c9439d589cae85e121b1f79f2af51e91dcee   universal-router                   100/window
0xad44d55e7f8337c3ce113fbb591486e85be104b2   instant-launch-2 (fee 0x222D6d..)   10/window
0x05d552391067389ee44fec3924157ed33f976000   lbp                                  8/window
0x9f67b864b565966dfcc2e0c6ba2483b2d5ff4b00   unnamed, v3.0.0 only
0x544ef36801e90ee56bcd699ed51a63cfceac8ec9   unnamed, v3.0.0 only
```

`pad_of()` ranks: **strategy > hook > deployer > suffix**.

### Independent pads — SOLVED, both are now watched

**Pons** and **flap** appear in neither launcher; they run their own factories.
Their launch events used to be the main open hole in verified-only trading. Both
are now recovered, wired into `LAUNCHPADS`, and pinned by tests.

Method that worked (faster than clustering — reuse it for the next pad): take a
token the pad launched, ask Blockscout `/api/v2/addresses/<token>` for its
`creation_transaction_hash`, pull that receipt, and read the logs emitted *at the
creator's own address*. Then brute-force the signature name against the topic0 to
prove it.

```
pons  0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB   ~105 launches / 40k blocks
      Blockscout: `PonsLaunchFactory`, source-verified
      TokenDeployed(address,address,address,address,uint256,uint256)
        0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a
      TokenLaunched(address,address,address,address,address,
                    uint256,uint256,uint256,uint256,uint256)
        0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a

flap  0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09   ~403 launches / 40k blocks
      TransparentUpgradeableProxy -> impl 0x7bc20c2c..fa06, named `Portal`
      launch    0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603
      liquidity 0x71a10912a55f73d3cced0d1515c2b33c396c80342522bad0e295ccbede556f37
```

Both Pons events fire once per token, in the same tx: topic1 = the new token,
topic2 = its v3 pool, topic3 = the v3 factory (constant), data word 0 = WETH. On
`TokenLaunched` the **last data word is the creator's initial buy in wei** —
observed 0, 0.2e18 and 3.5e18. That is a dev-buy size available at t=0, which is
real entry signal we did not have before.

flap's event is **unindexed** but carries its metadata inline, so name, symbol and
the IPFS CID arrive with the launch and need no extra `eth_call`:
`(uint256 timestamp, address creator, uint256 launchId, address token, …offsets…,
string name, string symbol, string ipfsCid)`. Matching on emitter+topic0 is still
unforgeable — only the pad can emit at the pad's address. Its signature *text* is
not recovered yet, so that topic0 is chain-observed rather than keccak-proven.

Live check after wiring: `discover_fresh` returned **18 launches in 1,500 blocks
(~2.5 min) — 14 flap, 4 pons**. Before, it returned zero for both, because a
blank `created_topic0` makes it skip the pad entirely (fail closed).

Known tokens: FRONG (pools.trade flagship)
`0x6245e67affA44a23077f0Ea7f981a8DC743a0c47`.
`0x391e96EE8C17ca09Ee795331C1D10A70b3a1432B` is a Pons launch — contract
`PonsLauncherToken`, but its ticker is **`Higher`**, not "Pons".
⚠️ `0x20024E485c0B22b42855589700721b28320A7777` was recorded here as "Flap". On
chain `name()`/`symbol()` return **`Prism Assets`/`PRISM`**. It is a genuine
flap-factory launch, but it is not a token called Flap — the `7777` suffix was
doing the labelling, which is precisely what a spoofable suffix should never do.

### Uniswap gateway — works, indexes 4663

```
POST https://interface.gateway.uniswap.org/v1/graphql
     header  origin: https://app.uniswap.org
     chain enum: ROBINHOOD          (ROBINHOOD_CHAIN is rejected)
     returns  symbol, name, decimals, project { name, isSpam, safetyLevel }
```

Use it as an independent second opinion, probed **in parallel** with the chain
scan, never as a blocking dependency. `trade-api.gateway.uniswap.org` 404s on
`/check_approval` for this chain.

---

## Corrections already made — do not regress these

**1. Rugs rendered as permanent winners.** `mark_and_exit()` had
`if val is None: continue`, which preserved the last good mark forever when
sell quotes failed. A honeypot therefore showed a beautiful P&L. Fixed: a
failed sell quote now marks the position to zero and increments `p["fails"]`;
`RUG_FAILS` consecutive failures force a close at 0. Also `LIQ_COLLAPSE` closes
when pool liquidity falls below 35% of entry. **Honeypot checks now run on
every token, including pad-verified ones.**

**2. `0x0000ffff..19c0` was labelled "pons".** It is Uniswap's Liquidity
Launcher core — shared infrastructure every pad routes through. That label
stamped a trusted pad name onto launches from *any* pad, impostors included,
which is how unverified contracts got bought. Removed; a test pins it.

**3. `0x58daec..4fa7` was labelled "pools-trade".** Reading FRONG's launch tx,
its only log there is an ERC-20 Transfer at its own address — it is a token in
the launch, not the pad. Removed.

**4. Valuation used cached quotes.** `quickTrade: true` is correct for entry
(reuses a cached quote, faster) but wrong for marking. Valuation now calls
`fquote(..., quick=False)`, and marking runs on its own 2s thread instead of at
the tail of the slow scan.

**5. The PM model was never called.** It was gated behind `tier == "STRONG"`
(score ≥ 0.6 AND agree ≥ 3). Replayed against a real batch: 0/7 setups reached
it, including a 0.72-confluence setup with +$9,387 net inflow. Momentum needs
75%+ buys to clear 0.5, so agree ≥ 3 demanded perfection. Calibrated to
`CONF_MIN=0.60`, `AGREE_MIN=2` → fires 3/7.

**6. v4 Swap sign convention.** v4 emits the **swapper's** BalanceDelta, the
opposite sign from v3's pool-perspective amounts. Verified in v4-core
`PoolManager._swap`, which emits `delta.amount0()`. A previous claim that
uniform sell-skew proved a sign bug was **wrong reasoning** — the owner
correctly pointed out one $10 buy can be followed by ten $1 sells.

**7. "There is no `TokenLaunched` event" was over-generalised.** True of Uniswap's
launcher, false of the chain: Pons emits a literal `TokenLaunched`. The old
wording told the next session not to look, so the claim protected itself. Fixed
above, with the real signature pinned by a keccak test.

**8. The desk regressed to Google-Fonts `<link>` tags.** `examples/cambrian_desk.py`
in the repo was an older copy that fetched Geist from `fonts.googleapis.com` —
the exact fallback-to-Segoe-UI bug the owner rejected twice. Restored from the
owner's current file: fonts embedded as woff2 data URIs, zero external resources,
and `--font-mono` now prefers the font that is actually loaded. **The repo copy
was stale relative to the owner's local file — check that before trusting it.**

---

## Exit policy

Priority order in `exit_decision()`: stop → trailing stop → profit rungs →
flow reversal → liquidity collapse → time stop.

```
RUNGS        2x sell 50%, 3x sell 25%, 5x sell 15%   (moon bag survives)
TRAIL_ARM    1.35    arms the trailing stop
TRAIL_GIVE   0.22    give-back off peak once armed
RUG_FAILS    3       consecutive failed sell quotes -> close at 0
LIQ_COLLAPSE 0.35    close if liquidity < 35% of entry
MAX_HOLD_MIN         time stop, only if no rung has hit
```

Owner's brief: "optimized to TP on the way up. Maybe be able to recognize when
something has enough volume that we might actually wanna let it run vs TP all."
The trailing stop is that recognition — it lets winners run past the rungs.

`BRAIN` env var selects `math` (default) | `llm` | `hybrid`. The owner chose
math: "Math probably executes quicker."

---

## External services

**Definitive Flash** — `https://flash.definitive.fi/v1`, chain `robinhood`,
header `x-definitive-api-key`, 5 req/s per endpoint per key. Settlement
`0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78`. Key from `RH_FLASH_KEY`.

**Surplus Intelligence** (OpenAI-compatible) — base URL
`https://api.surplusintelligence.ai/min30/v1`, `Authorization: Bearer inf_...`,
model `claude-opus-4.7`. Key from `RH_LLM_KEY`. The `/min30/` path segment is
required; plain `/v1` is wrong.

### Security rules — non-negotiable

- **Never accept a wallet private key.** Signing belongs in the Flash MCP
  (`@definitive-fi/flash-mcp`) with keys in the OS keychain. The owner offered
  a PK; it was declined and must stay declined.
- A Flash API key alone cannot move funds — every trade needs a wallet
  signature plus an onchain approval.
- All keys come from env vars. Never hardcode, never commit.
- **A Surplus key was pasted into chat earlier in this project and should be
  rotated.** Do not echo key values into files, commits, or logs.

---

## UI / design

**The entire UI lives inside `examples/cambrian_desk.py`** as the `PAGE`
raw-string — tokens, layout, six palettes, the logo, and the fonts. That one
file is the whole desk: chain scanning, agents, exit policy, paper engine, web
server and interface. Copy it anywhere and it renders identically.
`docs/DESIGN.md` + `docs/tokens.css` are the reference spec; nothing reads them
at runtime.

**Fonts are embedded, not linked.** Geist and Geist Mono (latin subset) are
inlined as woff2 data URIs, ~69 KB, two `@font-face` rules because both are
variable faces spanning weights 400–600. Do NOT replace them with a Google
Fonts `<link>` — an earlier version did that and fell back to Segoe UI offline,
which the owner rejected twice. The page now loads **zero** external resources;
the only outbound URLs are click-through links (DexScreener, Blockscout, the
pad sites).

**Palettes**: `instrument` is the default (no `data-theme` attribute), plus
`graphite`, `void`, `ember`, `nocturne`, `daylight`. The owner wanted to *click
through* them, not pick from a dropdown. He tried `void` and disliked it — do
not make it the default.

**The logo is inline SVG** in the page (not `assets/logo.svg`, which is just a
copy), stroked with `var(--text)` and `var(--agent)` so it recolors per theme.

Hard rules:

- **Violet means agent. Nothing else is ever violet.** Each palette defines its
  own `--agent` hue and `--fill-agent` is its only consumer. A 2px violet
  provenance gutter marks agent-originated rows.
- Teal `#2ED3A7` = profit, coral `#FF6B7A` = loss.
- No floating cards. 28px dense rows. 2px/4px radii (`--r-sm`/`--r-md`).
- `font-variant-numeric: tabular-nums` on every numeric column, so digits do not
  jitter as prices tick.
- Age format is `42s` / `5m12s` / `1h05m`, ticking client-side every second.
  Never decimal minutes — `5.2` was rejected explicitly.
- `examples/cambrian_desk_v3.py` is the preserved pre-redesign snapshot. Keep
  it; the owner asked for a way back.

Contract addresses in the UI link to both the launchpad token page and
DexScreener, and the ticker must be shown.

### Taste notes from review rounds

The owner is watching agents work, not reading a consumer app — reference
points were Padre-style pro terminals. "Think PROFESSIONAL AGENT TRADING DESK."
He is blunt about visual misses and will say so; when he does, fix the actual
cause rather than adjusting around it. The Segoe UI fallback was diagnosed
twice before the real cause (fonts declared but never loaded) was found, which
is exactly why they are embedded now.

---

## Environment notes

Sessions run in an Anthropic-managed VM behind a policy-enforcing egress proxy.
The environment's network level is now **Full**, so `curl` reaches RH's RPC,
pools.trade, Uniswap's gateway and GitHub. `WebFetch` uses a separate
Anthropic-side path that may still refuse hosts; **use `curl` when WebFetch is
blocked** — it is a complete substitute and is how every scan above was run.

Cloning public repos works (`git clone --depth 1 https://github.com/...`), which
is the fastest way to settle an ABI or event question definitively. Prefer it
over trusting a search-result summary.

Pure-Python keccak256 lives in `examples/rh_codehash.py`; no dependency provides
it. Import it by slicing the source between `_M = (1 << 64)` and `def rpc(`.

---

## Open work

1. ~~Find Pons's and Flap's launch events.~~ **DONE** — both recovered, wired
   into `LAUNCHPADS`, verified live (18 launches / 2.5 min). Remaining tail:
   recover flap's launch-event *signature text* (topic0 is chain-observed, not
   yet keccak-proven), and decode Pons's two unlabelled `uint256`s — both are
   monotonic counters, one stepping ~3-4 and the other ~5 per launch.
   Next pads to run the same recipe on: Noxa, bankr, ArrowPad, hood.fun.
2. **Make verified-only trading the default** — refuse any token not traceable
   to a named strategy or a confirmed pad event. Proposed, not yet confirmed by
   the owner.
3. Name the unknown strategies `0x9f67b8..` and `0x544ef3..`.
4. **Agent B** — the volume hunter. Not started.
5. Deployer reputation scoring; conviction-based position sizing.
6. `eth_subscribe` push discovery instead of polling (speed).
7. Ask the sniper contact: does pools.trade emit at bonding-curve creation or at
   graduation? What is his entry-latency breakdown?

---

## Working agreements

- Branch: `claude/building-with-docs-pj6oon`. Commit and push; the VM is
  ephemeral.
- Tests: `python -m pytest -q` from the repo root. 189 passing.
- The owner runs the desk on **Windows PowerShell**. Multi-line paste blocks and
  here-strings fail there — **send actual files** via file delivery instead.
  Never wrap a key in quotes in an example command; he has pasted the quotes.
- No synthetic or mock data anywhere. The LLM's output surface is exactly
  `{decision, confidence, reason}` so it cannot invent numbers.
- When challenged on a claim, verify from source rather than defending it. Two
  of the corrections above came from the owner being right.
