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

### pools.trade IS this launcher stack

pools.trade is Uniswap Labs' own launchpad (live 2026-08-05) and runs entirely on
Uniswap's own contracts. Every name below is the contract's **source-verified name
on Blockscout**, not our guess — and the descriptive labels this project had
already chosen turned out to match Uniswap's real contract names exactly.

```
LiquidityLauncher                  0x0000fffFbe8efe702c8703ae3477ff5de3d319c0  v3.2.0
LiquidityLauncher                  0x00004c4ccc709ef590f7c81102c0689f0263d4e9  v3.0.0
ContinuousClearingAuctionFactory   0x000000001f26a0044baa66024e7b6599c61963f8
InstantLaunchStrategy              0x23f8209572b4a1c2ad88a42749e830791fb027f1  184/40k
InstantLaunchStrategy              0xad44d55e7f8337c3ce113fbb591486e85be104b2   11/40k
LBPStrategy                        0x05d552391067389ee44fec3924157ed33f976000   12/40k
UniversalRouterStrategy            0x1242c9439d589cae85e121b1f79f2af51e91dcee   96/40k
```

pools.trade offers exactly two formats and both are now pinned on-chain:

- **Instant Launch** → `InstantLaunchStrategy`, tradable immediately.
- **Crowd Launch** → `LBPStrategy`, four hours of bids through the auction
  factory, then a pool. **PROVEN, not inferred:** in 12 of 12 sampled
  `AuctionCreated` txs the token was distributed through `LBPStrategy`, and that
  factory is the sole emitter of `AuctionCreated` on the chain.

Launcher launches settle into **hookless** v4 pools (`hooks == address(0)`),
which is how they are told apart from hooked pads on the same PoolManager.

`UniversalRouterStrategy` is a launcher strategy, **not** Uniswap's Universal
Router (`0x8876789976decbfcbbbe364623c63652db8c0904`). Do not conflate them.

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

pons-v2 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e  ~31% of all Pons launches
      TokenLaunched(address,address,address,address,uint256,uint256)
        0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607
      meme hook 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044  (graduated v4 pools)

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

✅ **flap trades on Uniswap V2 — corrected.** An earlier pass here claimed flap
launches "create no pool and are not buyable at launch". That was wrong, and it
was wrong for an instructive reason: we only ever scanned v3 and v4. flap launches
into **Uniswap V2**, which this project had never looked at.

```
V2 Factory   0x0d1ebb179cdbca88d74c923c4255cb2b17474afd   640 pairs / 40k blocks
PairCreated  0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9
Swap         0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822
Sync         0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1
```

Verified live: **186 of 186** flap `TokenCreated` events created a V2 pair *in the
same transaction*. Every sampled pair was seeded with exactly **1.9190 WETH**
(the `seedWeth` field of `TokenCurveSetV2`), and **7 of 8** sampled pairs had real
swaps within minutes. So the chain's highest-volume launch source is tradable from
block zero — the desk simply could not see it.

The documented graduation event `LaunchedToDEX(address,address,uint256,uint256)`
(`0x6e4f4763..ff7d`) has **zero** occurrences anywhere on this chain across 200k
blocks. On Robinhood Chain flap does not run the bonding-curve-then-graduate path
its generic docs describe; the pair exists at launch.

**Uniswap V2 support is BUILT** — `cambrian/runners/uniswap_v2.py` (decoding +
constant-product math), `feed.discover_new_pools_v2` / `v2_pool_swap_metrics` /
`v2_pool_depth_usd`, and `enrich` now dispatches on `hit["venue"]`. V2 depth is
EXACT (reserves are the balance), unlike the v3/v4 upper-bound proxies, and
`amount_out` doubles as the sell-quote for honeypot checks.

Sign convention: `decode_v2_swap` folds V2's four unsigned legs into v3's signed
pool-perspective form (`in - out`), so `aggregate_swaps` works with invert=False
like v3. Only v4 inverts. A test pins this — get it wrong and every V2 buy reads
as a sell, which is exactly the bug that once hit v4.

### Pons v1 vs v2 — they are DIFFERENT protocols

Found by auditing `docs.ponsfamily.com/v2` against config. Everything this file
said about "pons" is **v1**. Pons **v2** is a separate deployment: different
factory, different `TokenLaunched` signature, different venue. We were watching
only v1, so ~31% of Pons launches were invisible (16 v2 vs 35 v1 over the same
12k blocks). Same bug class as watching one of the two Liquidity Launchers.

| | v1 | v2 |
| --- | --- | --- |
| factory | `0xA5aAb3F0..1feB` | `0x7eD598Bc..EC7e` |
| topic2 of TokenLaunched | deployer | **curve** |
| venue | Uniswap **v3** pool at launch | bonding curve → **v4** at graduation |
| pair asset | WETH | native ETH (`0x0`) **or approved ERC-20** |

Three traps in v2 specifically:

- **Do not reuse the v1 field layout.** v1's topic2 is the deployer; v2's is the
  per-token bonding-curve contract. Trade history lives on that curve, not a pool.
- **Pair asset is not always WETH.** Sampled live: two launches against
  `0x000..0` (native ETH) and one against a custom ERC-20. A WETH-only filter
  drops them silently, which looks exactly like "no launches found".
- **The docs' 99% snipe tax is not active.** They describe a snipe tax opening at
  99% and decaying to zero over 5 seconds. Measured across every sampled launch
  (`launchConfigId` 0), buys landing in the launch block itself paid **1.00% fee,
  0.00% tax**. It is a per-launch-config parameter, so read fee/tax off `CurveBuy`
  rather than trusting either the doc's 99% or the observed 1%.

Everything else in Pons's docs matched what we already had exactly: both factory
addresses, both start blocks, the locker, and the v1 `TokenLaunched` topic0.

### Pons v2 curve pricing — BUILT

`cambrian/runners/pons_v2.py` prices v2 tokens pre-graduation, plus
`feed.discover_new_curves_pons_v2` / `pons_v2_curve_metrics` /
`pons_v2_curve_depth_usd`, and `enrich` dispatches on `venue == "pons-v2-curve"`.
There is no pool to quote in that window, so price, depth and flow all come off
the curve contract and its own `CurveBuy`/`CurveSell` events.

Three things that will bite anyone touching this code:

- **Fees land on opposite sides.** A BUY takes fees off the INPUT before pricing;
  a SELL prices first and takes them off the OUTPUT. Treating them the same way
  overstates sell proceeds, and that error only ever shows up as a quietly
  optimistic P&L.
- **`getReserves()` is mostly phantom.** It returns PRICING reserves including a
  virtual quote reserve seeded at launch — a live curve read ~4.36 ETH against
  `realQuoteReserve() == 0`. Price with it, never report it as liquidity, or every
  brand-new launch looks deep. Depth uses `realQuoteReserve()`.
- **Quote assets are not all 18 decimals.** A live curve had a `graduationThreshold`
  of 8.09e9 — a 6-decimal asset. Assuming 1e18 misprices it by 10^12. `enrich`
  reads the decimals per curve.

Getters, all verified live and pinned by selector tests: `getReserves`,
`realQuoteReserve`, `tokenReserve`, `sellableTokens`, `feeBps`, `creatorTaxBps`,
`readyToGraduate`, `graduated`, `graduationThreshold`.

⚠️ **v2 creator taxes are high too.** Live curves carried `creatorTaxBps` of 500,
600, 700 and 1000 on top of the 1% protocol fee — 12–22% ROUND TRIP before any
price move, charged on BOTH legs. Use `round_trip_cost_bps()` when sizing.

**But do not read a round-trip loss as all tax.** Quoting an instant buy→sell on
three live curves and splitting the cost:

```
7% tax curve:   tax/fee 15.30%  +  slippage  0.94%  ->  16.10% lost
0% tax curve:   tax/fee  1.89%  +  slippage 10.64%  ->  12.32% lost
1% tax curve:   tax/fee  3.86%  +  slippage  5.17%  ->   8.83% lost
```

The zero-tax curve still lost 12.3%, nearly all of it slippage: 0.1 ETH against a
curve holding ~4.4 ETH of pricing reserve is a big trade relative to depth, and
constant product charges for it both ways. The two costs need different tools —
**tax is fixed** (creator-set, both legs, same at any size; that is what the 3%
gate is for), **slippage scales with size against depth** (controlled by position
sizing, and never a reason to reject a token). The gate does nothing about
slippage, so size the entry off curve depth separately.

(These are QUOTES, not fills. The desk has no signing path — nothing was traded.)

### Stock-paired launches — priced properly, and apeable

~60% of Pons v2 launches are paired against a **Robinhood Stock Token** (GME,
SPY, AAPL, TSLA, SPCX, COIN, NVDA, MU, CRCL, PLTR ...) rather than ETH. Two
consequences, both handled in `runners/stock_tokens.py`:

**The quote asset is not worth the ETH price.** A SPY-quoted launch is
denominated in a ~$773 asset, a GME-quoted one in ~$19. Since sizing is a
fraction of market cap, valuing either at the ETH price mis-sizes every ticket.
Prices come from Robinhood's public API, mid of bid/ask, **times the
corporate-action multiplier** — a live 4.0 on CRWD means one token represents
four post-split shares, so skipping it prices the token at a quarter of its
worth. An unknown quote asset returns None and BLOCKS, rather than silently
falling back to ETH.

```
GET https://api.robinhood.com/rhj/assets            96 tokens on chain 4663
GET https://api.robinhood.com/rhj/prices/{symbol}   60 req/s, 15s cache
```

**They run ~5% creator tax, and the owner wants them traded.** So stock-paired
launches get `STOCK_PAIRED_MAX_TAX_BPS` (500) instead of the 3% house rule.
Deliberately not a blanket raise — 5% everywhere would wave through half of flap,
which is exactly the flow the 3% rule exists to exclude.

⚠️ **Canonical membership only.** Robinhood's docs are explicit: a token with a
matching ticker but a different address is NOT a stock token. Anyone can deploy
an ERC-20 called AAPL and pair a launch against it; that launch would look
stock-backed while its quote asset is worthless — and would inherit the looser
tax ceiling. `is_stock_token()` checks the registry, never the name.

### Execution — Definitive Flash

`runners/execution.py`. Flow from Flash's OpenAPI spec (`/v1/openapi.json`, v2.0.0):

```
POST /quote  -> quoteId + evm.{wrap, approveTx, orderTypedData}
sign evm.orderTypedData (EIP-712) with the funder wallet   <- ELSEWHERE
POST /order  -> quoteId + userSignature + evmOrderTypedData echo
```

**Keys never enter this process.** Nothing here signs and nothing accepts a
private key. `prepare()` hands out typed data for the Flash MCP
(`@definitive-fi/flash-mcp`) to sign from the OS keychain; `submit()` takes a
signature made somewhere else and refuses unless `confirm=True`. A Flash API key
alone cannot move funds — every order also needs the wallet signature plus an
on-chain approval to the settlement contract `0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78`.

Three things learned against the live API, each of which costs an hour if you
rediscover it:

- ⚠️ **A browser User-Agent is mandatory.** Without one Cloudflare answers
  `403 error code: 1010`, which reads exactly like a bad API key and sends you
  hunting in the wrong place.
- ⚠️ **Flash cannot price a fresh Pons v2 curve.** It aggregates 200+ DEXes but
  only assets it has notional rates for; a bonding curve is not an AMM it indexes
  and returns `FailedPrecondition ... missing notional rates for assets`. Verified
  live. `route_for()` sends pre-graduation v2 to the direct curve path and
  everything else — v3/v4 pools, flap's pair, stock tokens — to Flash.
- **Native ETH gets wrapped.** Spending the `0xEeee...EEeE` sentinel returns a
  `wrap.evmTx` to send first; the order itself then spends WETH.

⚠️ **`RH_WETH_USD` defaulted to 3000. ETH measured $1,917 against Flash — a 56%
overstatement.** Market cap is priced in the quote asset and size is a fraction
of market cap, so that stale constant inflated every ticket by 56%. `sweep` now
takes a LIVE price from `live_eth_usd()`, derived from a real quote's
notional/amount. Note it anchors on a stock token, not WETH: spending native ETH
for WETH is just a wrap and Flash declines to quote it.

### Curve-direct execution — the path Flash cannot route

`runners/curve_exec.py`. Pre-graduation Pons v2 tokens have no AMM, so Flash
returns `missing notional rates`; the only way to trade them is to call the curve.
That window is also where a sniping desk lives, so this is not an edge case.

ABI from docs.ponsfamily.com/v2, both selectors confirmed present in live curve
bytecode:

```
buy(uint256 quoteIn, uint256 minTokensOut, address recipient) payable  0x59a87bc1
sell(uint256 tokensIn, uint256 minQuoteOut, address recipient)         0xd04c6983
```

**VALIDATED AGAINST THE CHAIN.** Simulating the built calldata with `eth_call`
plus a balance state-override on two live curves, the contract's own `buy()`
return matched our `quote_buy()` to **0.000%** — including on a 10%-tax curve. So
the curve math here reproduces the contract exactly; a quote and a fill agree.

Three mechanics that silently cost money if assumed:

- **Native vs ERC-20 quote.** On a native launch `quoteIn` must EQUAL `msg.value`.
  On a stock-paired launch (~60% of v2) you approve the curve first and send
  ZERO value. Sending value on an ERC-20 launch strands ETH; approving on a
  native launch wastes a transaction.
- **minOut is mandatory.** It bounds the price, and passing 0 invites a sandwich
  — a fresh curve with virtual reserves is the cheapest place to run one. Every
  builder derives minOut from our own quote and REFUSES to emit a zero, so a
  curve we cannot price produces no trade rather than an unbounded one.
- **Selling always needs the token approved**, whatever the quote asset is: the
  curve pulls the tokens from you.

Same safety model as Flash: these build **unsigned** transactions, nothing signs,
nothing accepts a key. A plan the gate blocked cannot reach calldata — otherwise
the tax gate would be advisory rather than binding.

### The sweep — one command that does the whole job

```
RH_RPC_URL=... python -m cambrian sweep --blocks 1200 --bankroll 1000
```

`runners/scanner.py` discovers across every pad, resolves each token to a venue,
prices its quote asset, gates it, sizes it, and ranks it. Blocked rows are shown
WITH their reason: a scanner that prints only what passed makes a broken gate
look like a quiet market, which is how pons and flap sat unwatched for weeks.
Exit code is non-zero when any log chunk failed, so a partial scan cannot be
mistaken for a quiet one.

### The tax gate — owner's hard rule: never above 3%

Enforced in `runners/flap_tax.py` and wired into `agent_safety`, where it outranks
pad verification: a verified, sellable flap token can still hand back 10% on exit,
and that is a certain loss rather than a risk.

Read the rate straight off the token — `buyTaxRate()` / `sellTaxRate()`, uint16
basis points. Do NOT parse the Tax Token Helper's `getTaxTokenInfoV2`: it returns
a 20-word struct whose field order is not published, and guessing offsets for a
safety gate is the wrong trade.

**The public docs' 1/3/5/10% menu is wrong.** Live rates are arbitrary. Across 120
consecutive launches: 10.0% x74, 7.3% x11, 6.3% x6, 4.3% x5, 1.3% x5, 9.3%/8.3%/
5.3%/3.3% x4 each, 3.0% x1, 2.3% x1, 1.0% x1. So gate on the number, never on a
tier set.

⚠️ **This removes ~93% of flap flow.** 62% of launches carry the maximum 10%. Live
run: of 32 V2 pools in a 1,500-block window, **30 blocked, 2 passed**. flap's
headline launch rate is NOT its tradable rate — size the opportunity off the
post-gate number.

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
scan, never as a blocking dependency. It does index launcher tokens and returns
`isSpam` / `safetyLevel`, which is real triage signal. GraphQL **introspection is
FORBIDDEN** (`errorCode: FORBIDDEN`), so query only known fields — you cannot
discover the schema from the endpoint. `trade-api.gateway.uniswap.org` 404s on
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

**7b. "Shared infrastructure every pad routes through" was false.** Correction #2
removed the LiquidityLauncher from the pad maps — correct — but justified it by
claiming every pad routes through it. Sampled live, **0 of 24** Pons and flap
launches touched the launcher at all; those pads run their own factories end to
end. The launcher is pools.trade's stack specifically. Keep it out of the pad
maps because it is a launcher, not because everyone uses it. This makes
"arrived via the launcher" a much stronger signal than we had credited.

**7c. `0x58daec..4fa7` is v4's PositionManager.** Correction #3 removed it from
`PAD_BY_DEPLOYER` (right) on the grounds that it was "a token in the launch"
(wrong). Uniswap's published v4 deployment table for chain 4663 lists it as the
**PositionManager** — core infrastructure, which is exactly why it turns up
inside launch txs.

**7d. Two Pons `TokenLaunched` fields were mislabelled here.** topic2 is the
**deployer**, not the pool; the pool is the second data word. Confirmed against
Pons's official docs, which publish the same topic0 we derived by keccak. The
trailing words are `positionId` and `restrictionsEndBlock`, not anonymous
counters — `restrictionsEndBlock` says when launch restrictions lift.

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

**The RPC caps `eth_getLogs` to ~2,048 blocks per request** ("requested logs from
N blocks ... only allowed to ..."). Exceeding it returns an error, not a truncated
result, so a helper that swallows errors silently reports ZERO launches rather
than failing loudly. Chunk every scan, and never pass a wide range with
`toBlock:"latest"` — the range grows as blocks arrive and trips the cap mid-run.

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
   pools.trade is now mapped too (see above) — its stack is Uniswap's launcher,
   all source-verified. Owner's call: Noxa / bankr / ArrowPad / hood.fun and the
   unnamed hook are **not worth chasing**; do not spend time on them.
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
