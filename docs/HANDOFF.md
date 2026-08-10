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

Watching only some of these silently loses launches — the mistake this file was
originally written to prevent.

**FOUR deployments exist, not two** (an earlier version of this file said two). From the owner's roster, cross-checked live:

```
gen4  0x0000fffFbe8efe702c8703ae3477ff5de3d319c0   ACTIVE      99 logs / 12k blocks
gen3  0x7a6c474b4dcd35b72203d2b569eafe4c9b5c768e   dormant      0 logs / 72k blocks
gen2  0xe050309b2f42cd5f788ab6ee1a07467770c03bf7   dormant      0 logs / 72k blocks
gen1  0x00004c4ccc709ef590f7c81102c0689f0263d4e9   near-retired 2 logs / 12k blocks
```

The owner numbers these as generations of "Uniswap CCA"; Blockscout source-verifies
the live ones as `LiquidityLauncher`. Same thing — the CCA product family, whose
launcher contract carries that name. gen2 and gen3 are real contracts of the same
code family (4,064 and 4,128 bytes) that have simply never fired in any window
scanned. **They are watched anyway.** The original sin recorded in this file was
watching one launcher while another still emitted; a dormant deployment waking up
is exactly how that recurs, and two extra log filters per sweep costs nothing
against silently losing a pad. Iterate `UNI_LAUNCHER_ADDRESSES`, never one address.

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

### FIRST LIVE TRADE — the loop is proven

`orderId 44e96922-707a-4265-98cb-bb92c1404b30`, 2026-08-10. $8 of USDG into a
pools.trade launch at a $5,044 market cap, 0% tax, 1.13% lost on the leg
(0.39% price impact, no fee). Discover -> gate -> size -> quote -> execute, on
chain, with real money. Every exit rule in this repo had until now only ever
been tested against synthetic marks; there is finally a real position to manage.

What the path to that first fill actually cost, worth remembering because none
of it was trading logic:

1. Credentials could not be got onto a Windows box through a shell. Solved by a
   file plus prefix-salvage (see Environment notes).
2. A hand-copied bundle went stale and its output was misread as a code bug.
   Solved by stamping every build.
3. `walletAddress` was missing from the address endpoint — but the resulting
   `400 ZodError` is what PROVED the HMAC signature was correct all along.
4. `"8.00000000"` against six-decimal USDG returns `400 "Internal server error"`.
   `"8"` works. Two hours of the wrong suspicion lived in that message.
5. Definitive could not price the top-ranked candidate, and quoting only the top
   pick threw away the whole scan.

Four of those five produced a misleading error message. When a live call fails
here, distrust the message and get the raw body first — `quote --raw`,
`vault --raw`, `--debug` all exist for exactly that reason.

### Definitive Client API — endpoint shapes, verified against the real docs

**The signature implementation is CONFIRMED CORRECT.** A live call came back
`400 ZodError: walletAddress Required`, which only happens after auth passes and
the request reaches body validation. HMAC-SHA256, the prehash format, the
`dpks_` strip, JSON-quoted header values, compact body serialisation — all of it
is right. That was the last unverified piece of the execution path.

The *shapes* were wrong, and the docs are at `ddp.definitive.fi` (page list at
`/api/portfolio-info/positions`; `WebFetch` 404s on them, plain `curl` works).
Corrections now in `runners/definitive.py`:

- `GET /v2/portfolio/address/{chain}` needs **`?walletAddress=0x...`**, and it is
  YOUR wallet — the address that will send the deposit, not the vault's. Returns
  `{vaultId, address}`. Vaults are auto-created per chain.
- Execution is `POST /v2/portfolio/quicktrade` — **there is no `/submit`
  sibling**, and no `quoteId` is threaded in. QuickTrade re-quotes and executes
  atomically, so a quote is an operator preview, not an input.
- `slippageTolerance` defaults to **1%**, which a fresh launch will not fill
  inside. The desk passes it explicitly (5%) on every submit.
- `chain: "robinhood"` is on the published supported-networks list.
- `qty` denominates the **contra** asset (what you spend) — confirmed live: 8
  USDG in returned `fromNotional: "8"`. `trade-vault` still refuses if
  `fromNotional` exceeds the ticket cap, so a future units change costs a
  refused trade rather than an oversized one.
- **`qty` must respect the spend asset's decimals.** `"8.00000000"` (eight
  places) against six-decimal USDG returns `400 {"message":"Internal server
  error"}`; the identical trade as `"8"` quotes fine. Confirmed both directions
  on the same token. Quantities are truncated — never rounded up — to the contra
  asset's on-chain `decimals()`.
- **Definitive cannot price every fresh launch.** A token it has no rate for
  fails with `failed to price asset. err: missing notional rates for assets
  [<uuid>]`. This is per-token and unrelated to liquidity: one pons-v1 launch
  quoted cleanly while another, minutes old, did not. So `trade-vault` walks the
  ranked candidates until one quotes instead of dying on its top pick, and names
  what it skipped. **Unpriceable tokens are not untradeable** — they need the
  curve-direct/router path rather than Definitive, which is the strongest
  argument yet for keeping both execution paths alive.

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

### Settlement is USDG or ETH — and that costs hops

The desk holds **USDG or ETH** on Robinhood Chain (USDC or ETH on Base). Nothing
launches denominated in USDG, so almost every entry crosses an asset boundary.
`runners/router.py` plans the path.

⚠️ **USDG is 6 decimals**, not 18 — `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`,
"Global Dollar", verified on-chain. An 18-decimal assumption overstates an amount
by 10^12: it would size a position a trillion times too large, or revert. $25 is
`25_000_000`.

Three shapes:

| Case | Hops | |
| --- | --- | --- |
| Flash-routable, USDG as contra | 1 | Flash takes USDG directly — $25 into FRONG quoted 0.18% impact live |
| Curve whose quote IS the settlement asset | 1 | only native-ETH launches settled in ETH |
| Curve whose quote is NOT the settlement asset | **2** | the common case |

The two-hop case dominates: ~60% of Pons v2 is stock-paired, so buying one from a
USDG balance is `USDG -> AAPL -> curve`. **Slippage is paid twice, and the desk is
exposed to the intermediate between legs** — a stock can move between the swap and
the curve call, so the size that arrives is not the size quoted. `plan_route`
reports the extra hop rather than folding it into one number; a two-leg entry into
a thin curve can cost more in transit than the edge being chased.

**Exits are NOT the entry reversed.** A curve sale returns the CURVE's quote
asset, so a stock-paired position exits into a STOCK which then has to be sold for
USDG. Planning the exit backwards leaves the desk holding equity it never chose —
`exit_route()` is a separate function for exactly that reason.

### High-tax flap is fee farming, not launches

Owner's read, and the chain agrees. Over 4,500 blocks:

```
PAD       TOTAL  TRADEABLE  PASS   MEDIAN TAX
flap      53     2          4%     10.0%
pons-v2   7      3          43%     6.0%
pons-v1   11     11         —      none (plain ERC-20, no tax function)
```

**36 of 53 flap tokens sat at EXACTLY 10.0%** — the maximum. A real launch does
not choose the cap, because the cap makes the token unsellable at a profit. So the
3% gate is not throwing away 93% of an opportunity; it is throwing away fee farms
and keeping the launches. Pons is where the tradeable flow actually is.

`format_sweep` now lists every PASSING row but only a few blocked ones, with the
rest counted by reason plus a fee-farm ratio. Blocked rows still must not vanish —
a scanner showing only passes makes a broken gate look like a quiet market — but
listing 40 max-tax farms buries the handful worth acting on.

⚠️ **Pons v1 launches into a Uniswap V3 pool, not V2.** Resolving it with the V2
reader returned nothing, so all 11 v1 launches in a live window failed the gate as
"no price" — the gate was working, the resolver was not. `venues.from_v3_pool`
prices them from `slot0().sqrtPriceX96`, which is exact.

V3 venues deliberately have **no local round-trip quote**: constant-product math on
a concentrated pool misprices depth in both directions. They size off market cap
and let Flash enforce slippage at execution, where it does the tick math properly,
and `execution.prepare` re-checks the real quote before anything becomes signable.
`plan["slippage_priced_by"] == "flash-at-execution"` marks those rows honestly
rather than printing a number we did not compute.

### The live loop

```
RH_RPC_URL=... python -m cambrian watch --blocks 1200 --bankroll 1000
```

`runners/live.py`. **Two cadences, deliberately** — scan 20s, mark 2s. Discovery
is slow (every pad, venue resolution, quote-asset pricing); marking is fast and
matters more, because a stop that fires 30 seconds late on a memecoin is a stop
that did not fire. Marking never waits on a sweep. This is correction #4 held at
loop level, and a scan failure is caught so it cannot stall marking.

**Dry run by default; nothing signs.** Ticks return *intents*. `positions.apply`
is called only via `record_exit_fill` on a real fill — a book that updates on
intent disagrees with the chain, which is worse than not tracking at all.

Discovery is deduped by token with a bounded seen-set (a launch alerts once, and
an unbounded set is a slow leak in a process meant to run for days). A quiet tick
prints nothing, because a loop that logs every tick buries the ticks that matter.

Live over 75s: 4 scans, 22 marks, 21 tokens — the cadence split working.

⚠️ **Pons v1 carries `tax_bps = 0` as a KNOWN zero, not an unknown.** v1 has no tax
mechanism at all — plain ERC-20s — which is exactly why v2 was built. Recording it
as None would leave the gate trusting an absence rather than a certainty. Do not
"fix" this by failing closed on v1.

### pools.trade was never in the sweep

Found by reconciling the owner's address roster: the scanner discovered pons-v1,
pons-v2 and flap, and **nothing at all from pools.trade** — 36 launches in a
20-minute window, completely invisible. Uniswap's own pad, and the one pad that
mints plain ERC-20s with no per-token creator tax, so it is precisely the flow
that sails through the 3% gate.

Discovery is off the PoolManager's `Initialize`, filtered to **hookless** pools,
rather than off the launchers' `TokenCreated`. Two reasons: a v4 pool has no
address, so `TokenCreated` alone leaves nothing to price against, while
`Initialize` yields the PoolId in the same read; and hookless is exactly what
separates launcher launches from other pads sharing the singleton.

`venues.from_v4_pool` prices them from slot0's sqrtPriceX96 via `extsload`, exact
like the V3 path, with `tax_bps=0` as a known zero. `pool` carries the **PoolId**,
not an address — which is why nothing tries `getReserves` or `balanceOf` on it.

Live after wiring: **15 tradeable over 1,500 blocks against 5 before**, with
pools.trade rows at $4.7k–$12.5k market caps and zero tax.

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

## ⚠️ The spot fallback reopened correction #1 — and how it was closed

Worth reading before touching `positions.mark`. Fixing the fake -100% on V3
venues introduced a spot fallback, which quietly undid the rug detector for every
venue that uses it:

**Spot ALWAYS returns a number.** So `fails` never incremented, `RUG_FAILS` never
tripped, and a Uniswap V3 or v4 honeypot marked healthy indefinitely — because
spot reflects the POOL's price, not whether you personally can sell. That is
correction #1 arriving through a side door, and it landed on pons-v1 and
pools.trade, which are now most of the tradeable flow and take the biggest
tickets.

Closed with `exit_quoter`: on a spot-marked venue, ask **Flash** what selling the
position would really pay. Flash is the path those trades would actually execute
through, so its refusal to quote a sell IS the unsellable signal, and it is the
only one available for a pool with no local sell math.

- status `unsellable` -> rug path (mark 0, `fails` += 1)
- status `error` -> mark untouched. **A network blip is not a rug**, and closing
  good positions on a bad connection is its own kind of loss.
- status `ok` -> the mark is upgraded to exact, at the real exit value

Throttled to `EXIT_QUOTE_INTERVAL_S` (30s) per token — the aim is to catch an
unsellable position within a minute, not to pay for a network call every 2s.
Curve and V2 venues never pay for it at all, since their local quote is already
exact. Live: real sell quotes returned for pools.trade and pons-v1 tokens.

## Position tracking — mark at the EXIT, not the mid

`runners/positions.py`. Two rules, both corrections to how this desk used to work.

**Mark at the sell quote, never the mid price.** A position is worth what closing
it would actually pay — net of creator tax, protocol fee, and the slippage of
THAT size against THAT venue. Measured live on four fresh positions of ~$20:

```
venue  tax     spent    mid-mark   exit-mark   instant P&L
pons   4.0%    $20.00   $18.89     $17.84      -10.8%
pons   0.0%    $20.00   $19.68     $19.37       -3.2%
flap  10.0%    $20.00   $17.85     $15.94      -20.3%
flap  10.0%    $20.00   $17.85     $15.94      -20.3%
```

The mid overstates a 10%-tax position by ~12% at the moment of entry. That gap is
the number that makes a desk feel profitable while it bleeds.

⚠️ **A failed sell quote marks to ZERO, never to the last good value.** This is
correction #1 and it must not regress. `exit_value_usd` returns None only when the
venue cannot be priced; `mark()` turns that into a 0 mark plus a `fails`
increment, and `RUG_FAILS` consecutive failures force a close at any price. The
old `if val is None: continue` preserved a stale mark forever, so a honeypot
printed a beautiful P&L right up until you tried to leave.

Exit priority is unchanged and the rug check outranks everything — a honeypot can
print a great mark, and being unable to sell still wins:

```
unsellable xN -> stop -> trailing stop -> profit rungs -> liq collapse -> time stop
```

The rungs never sum to 1.0, so a moon bag always survives; the trailing stop
(arm 1.35x, give 22%) is what lets a winner run past them.

## Exit EXECUTION — wired, exit-only (`runners/vault_exec.py`)

The policy in `exit_decision` was complete for a long time and had nothing to
submit through. It does now.

**Marking a vault position needs no venue at all.** `positions.mark_from_exit_quote`
takes what a real Definitive sell quote would pay. That is strictly better than
`mark()` for anything held in the execution venue's own account: no pool, no
reserves, no resolution, and the number IS the exit price rather than a model of
one — route, tax and this exact size's slippage are already inside it. The thing
that decides and the thing that would execute are the same call, so they cannot
disagree. A refusal to quote is the unsellable signal the rug rule keys off.

**`adopt()` builds the book from what the vault actually holds.** A loop that
only knows about fills it personally saw will watch a hand-bought position go to
zero without ever firing a stop — the worst failure available here. Cost basis
comes from the venue's accounting when reported (directly, or `notional - pnl`);
otherwise the current exit value stands in and `basis_known` is False, which
`watch` prints in capitals, because every percentage rule then measures from
adoption rather than entry and that is a materially different thing.

**`watch --execute` is EXIT-ONLY and opt-in.** Selling what you already hold
cannot increase exposure, so an unattended exit is the safe half of automation.
Unattended *entry* is not, and remains a separate decision that has not been
made.

**The book updates on submit, not on fill** — deliberately breaking `live.py`'s
usual rule. An intent stays true until the position changes, so waiting for
confirmation re-sells every tick. `ExitGuard` (60s per token) is the backstop if
a submit silently fails, and a *refused* exit starts the cooldown too, so a venue
that cannot quote is not hammered forever.

Manual half: `cambrian sell --token 0x.. --pct 50 [--yes]`.

### How fast can the exit loop actually be

Asked directly, and worth pinning because the intuitive answer is wrong.

**100ms is the information floor.** Robinhood Chain produces a block every
100ms. Polling faster than that re-reads identical state — it is not a faster
desk, it is the same desk making more requests.

**~300ms is the practical floor**, because a mark is an HTTPS round trip.
Measured: Definitive ~290-340ms, the public RPC ~320-680ms. Unlimited request
quota does not help here; the constraint is latency, not throughput, and the two
are not the same resource.

**No WebSocket was found on the public RPC** (`wss://rpc.mainnet...` 400,
`/ws` 404), so `eth_subscribe` push — open-work item 6, and the only thing that
would actually beat polling — has no endpoint to attach to yet. Worth asking
Robinhood or the sniper contact whether a WS endpoint exists.

So the loop polls **continuously** (`--mark-interval 0`, the default) rather than
on a timer, and marks **concurrently**. Concurrency is the part that matters:
serial marking makes the loop's period the SUM of every position's round trip, so
with ten positions the tenth is three seconds stale — and the tenth is the one
whose stop misses. Measured 8 positions in **306ms** parallel against 2,400ms
serial. Exits themselves are still submitted serially, which is deliberate:
they are rarer, and a burst of concurrent submits is not a risk worth taking to
save a second.

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

## The live terminal — `cambrian terminal`

`examples/cambrian_paper.py` answers "would this have worked". `cambrian/terminal.py`
answers "what am I in right now, and how do I get out": every number on it is a
real Definitive sell quote against real vault holdings.

It lives **inside the package**, not in `examples/`, because the owner runs the
single-file bundle and `tools_build_bundle.py` only embeds `cambrian/**`. Anything
in `examples/` is unreachable on his machine. The design system moved to
`cambrian/ui_css.py` (fonts + tokens, lifted verbatim) for the same reason — and
so the paper and live terminals cannot drift apart. **Zero external resources**:
78 KB page, fonts embedded, only click-through links leave the box.

- `STOP` halts automation. It does **not** sell. Those are different intentions
  and conflating them turns a panic click into a market sell. Manual `Close`
  keeps working while halted — halting is about the automation, not your hands.
- `Close` / `Close all` sell at market, both behind a confirm, both bypassing the
  cooldown (which exists to stop a repeating *automatic* signal, not a person).
- The engine runs whether or not a browser is open. Closing the tab stops
  nothing; a desk that only manages positions while you are looking at it is not
  managing them.
- It re-adopts every 30s, so anything bought by hand or by `trade-vault` starts
  being managed without a restart.
- 127.0.0.1 only. Any local process can reach the controls — personal desk, not
  a shared one.

### ⚠️ `exit_decision` MUTATES — asking the question consumes the answer

Caught live: `exit_decision` appends to `pos.rungs_hit` when a rung fires, so
calling it merely to *render a signal* marks that rung taken. The terminal did
exactly that while halted — 2x and 3x were recorded as hit, nothing was sold, and
on resume they could never fire again. `watch` had the same bug from the other
direction: it checked the cooldown *after* the decision, so a rung arriving during
a cooldown was consumed and never sold.

**Rule: never call `exit_decision` unless the result can be acted on.** Check
halted and cooldown first. Three tests pin this — including one that runs the
real engine thread halted and asserts `rungs_hit == []`.

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

### Credentials reach the desk through a FILE, not the shell

Getting the Definitive key and secret onto the owner's machine cost more rounds
than every piece of trading math combined, and none of the failures were about
trading. `$env:X=value` baked quotes into the value; an interactive `Read-Host`
prompt collected the *next pasted command* as the secret — twice; and the value
that finally landed in the key line was 80 characters of copied terminal prompt.
The lesson is not "explain PowerShell better", it is that **a value typed at a
shell passes through the shell's grammar, and a value in a text file does not**.

So `cambrian/config.py` reads `cambrian.env` (working dir, then home) as plain
`KEY=VALUE`, tolerating quotes, whitespace, BOMs and comments, never overriding a
real environment variable, and never raising — a malformed file must not stop the
desk booting.

Then a **salvage pass**, which is the part worth keeping: `dpka_`/`dpks_` are
unambiguous prefixes, so any credential that is missing or carries the wrong
prefix is re-found by scanning the whole file's text for its prefix. This makes
the *position* of the value irrelevant. A pasted terminal line
(`DEFINITIVE_API_KEY=PS C:\Users\...> $env:DEFINITIVE_API_KEY=dpka_real`) and a
straight swap of the two values both self-correct, silently and correctly. Add
any future prefixed credential to `CREDENTIAL_PREFIXES` and it inherits this.

One more rule fell out of this, and it is the interesting one. "A real
environment variable beats the file" is right, but it was being applied to a
value that **was not a credential at all** — a shell variable holding a copied
terminal prompt silently outranked a perfectly good file, and the diagnostic then
blamed the file. A missing `dpka_`/`dpks_` prefix is *proof* the value cannot be
that credential, so the file now wins over shell text — and `KEY_SOURCES` records
the override so `keys` says it happened rather than quietly doing the right thing
for an unexplained reason. A correctly-prefixed environment value is still
authoritative; a stale file must never silently redirect a trade.

`cambrian.build` stamps every bundle with the commit it came from, because "run
the new file" and "the new file is what ran" are different claims and a
hand-copied bundle makes a stale copy look like a bug in new code. That cost a
round on its own.

`cambrian keys` reports where each value came from and flags a salvage, printing
prefixes only — a secret that must be echoed to be verified is a secret that ends
up in a screenshot, which is exactly how this one leaked.

`cambrian vault --debug` prints the exact string being signed with the key
redacted and the secret never touched. A wrong signature and a wrong key both
return a bare 401, and this is the only way to tell them apart **without ever
being handed real credentials** — the offer to paste them into chat was declined
and should stay declined.

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
