# IPO — Initial Pons Offering

A token on [Robinhood Chain](https://docs.robinhood.com/chain/) that taxes itself 4%, and every
hour spends the proceeds buying whatever is trending on Pons and hands it to holders.

```
  every IPO trade                   once an hour
  ──────────────                    ────────────
  4% tax ──► IPOTreasury ──► buy the top trending Pons coins
             (holds ETH)           │
                                   ├──► IPODistributor ──► holders claim, pro rata
                                   └──► IPOVault (optional) ──► permanent backing
```

Hold IPO, get paid in whatever is hot on Pons this hour.

---

## The 4%

Pons levies it, not us.

A Pons token is fixed-supply with no mint, freeze, blacklist, or tax-raising after launch — so a
conventional ERC-20 transfer tax is not available on a Pons launch. What Pons does provide is
`creatorTaxBps`, chosen freely at launch (up to the protocol cap read from `maxCreatorTaxBps()`)
and immutable thereafter. IPO launches with:

```
creatorTaxBps       = 400            // 4%
creatorFeeRecipient = IPOTreasury
buybackEnabled      = false
pairToken           = native ETH
```

That gives a 4% tax that:

- is charged on **every buy and every sell**, on the bonding curve *and* after graduation, where
  the Pons meme hook collects it;
- is taken on the **quote side, in ETH** — deducted from quote input on buys, from quote output on
  sells, never in IPO itself;
- accrues to the treasury in the Pons fee escrow, withdrawn with `claim()`.

Being paid in ETH is the part that matters. A conventional tax token accumulates its own token and
must sell it to do anything useful, so the treasury becomes a permanent sell-side flow against its
own holders. Here the treasury never touches IPO. It receives ETH and spends ETH.

`buybackEnabled` is off deliberately: Pons buybacks would route the creator's fee share into buying
IPO and lock it for five years. We want that ETH buying *other people's* coins.

## The hourly cycle

One `runEpoch` call per hour does the whole thing:

1. **Sweep** any tax accrued in the Pons escrow since the last epoch, so it is deployed this hour
   rather than next.
2. **Buy** the trending coins the keeper selected, splitting the epoch budget equally between them.
3. **Route** each purchase to the distributor (and optionally the vault).

The entire basket is bought under a **single pool-manager unlock**: the swaps accumulate one net
ETH debt that is settled once. That is cheaper than unlocking per coin, and it makes the hour
atomic — either the whole basket lands or none of it does, so there is never a half-filled epoch
with an unresolved delta.

The budget is `min(balance − minReserve × epochSpendBps, maxEpochSpend)`, divided equally across
the orders. **Equal weighting is deliberate**: it removes the keeper's discretion over position
sizing, so the only judgement they exercise is *which* coins are trending.

## What "trending" means

Trending has no on-chain definition, so `bot/trending.ts` computes it from Uniswap v4 `Swap` events
over a trailing window. Four signals, each normalised against the best coin in the window:

| Signal | Weight | What it catches |
| --- | --- | --- |
| `volume` | 0.40 | ETH traded — the baseline "is anything happening here" |
| `traders` | 0.25 | Unique addresses — breadth, because one whale is not a trend |
| `buyRatio` | 0.20 | Share of swaps that are buys — accumulation vs distribution |
| `momentum` | 0.15 | Price change across the window, clamped to ±100% |

Volume alone is trivially wash-traded by one address cycling a balance. Requiring breadth and
direction alongside it makes that meaningfully more expensive. Weights are a parameter, not a
constant — tune them against real data.

v4 emits one `Swap` per *pool id*, not per token, so the bot rebuilds each Pons pool's id
(`keccak256(abi.encode(PoolKey))`, native ETH always `currency0`) and matches events against that
set.

## How holders get paid

`IPODistributor` uses a **cumulative** merkle tree. Each published root encodes, for every holder
and every coin, the total that holder has *ever* been owed; a claim pays the difference against
what they have already taken.

That matters at an hourly cadence. A per-epoch design would publish 24 roots a day and force a
holder to submit 24 proofs per coin to collect a day's worth. Here **one proof collects everything
owed since the last claim**, whenever the holder gets round to it, and someone who never claims
just keeps accruing.

```solidity
distributor.claim(account, token, cumulativeAmount, proof);
distributor.claimMany(account, tokens, cumulativeAmounts, proofs);
```

Leaves are `keccak256(keccak256(abi.encode(account, token, cumulativeAmount)))` — the inner hash is
doubled so a leaf can never be reinterpreted as an internal node.

### Optional permanent backing

`vaultBps` splits each purchase between the distributor and `IPOVault`, which holds its share
forever and lets holders redeem a pro-rata slice by locking IPO. It defaults to **0** — everything
goes to holders. Raising it trades current yield for a hard NAV floor under IPO.

Redeemers choose which assets to take, since the basket grows without bound and redeeming all of it
would eventually exceed the block gas limit. Skipping an asset forfeits that claim and *raises*
per-token backing for everyone who stays:

> Supply `S`, balances `a` and `b`. A holder redeems `r` taking only asset A.
> Claim per token on A: `a/S` before, `a(S−r)/S ÷ (S−r) = a/S` after — unchanged.
> Claim per token on B: `b/S` before, `b/(S−r)` after — **increased**.

Both properties are asserted in the tests.

## Trust model

**No contract has an owner withdrawal path.** Not the treasury, not the vault, not the distributor.
ETH that reaches the treasury can only leave as a purchase; coins can only leave as a claim or a
redemption. A test asserts none of the three ABIs contains `withdraw`, `sweep`, `rescue`,
`emergency`, `recover`, or `skim`.

**Keepers cannot redirect funds.** A keeper submits a list of tokens. The treasury reads each one's
launch record from the Pons factory, rebuilds the v4 pool key itself, and swaps through the pool
manager directly. There is no arbitrary-calldata path. The contract independently enforces:

| Check | Why |
| --- | --- |
| `exists` | A real Pons launch, not an arbitrary address. |
| `phase == PoolCreated` | Actually bonded. Rejects `NotGraduated`, `Swept`, and `Rescued`. |
| `pairToken == address(0)` | ETH-paired, so treasury ETH can buy it directly. |
| `graduationThreshold >= minGraduationThreshold` | The launch had to raise a real amount to bond. |
| `now >= lastBoughtAt[token] + tokenCooldown` | One coin cannot be bought hour after hour. |
| strictly ascending orders | Rules out duplicates taking several slices of the budget. |
| `orders.length <= maxTokensPerEpoch` | Bounds the gas of one epoch. |
| `epochBudget()` | Caps ETH at risk in any single hour. |

### What is actually trusted

Two things, and both are worth stating plainly.

**Coin selection.** "Trending" is off-chain by nature, so the keeper decides which coins the
treasury buys. The contract bounds *what* it will accept and *how much* it will spend, but not
which coin trends. A compromised keeper can waste an hour's budget on coins that pass every filter
— including ones it launched and funded to graduation itself. `maxEpochSpend`, `tokenCooldown`, and
`minGraduationThreshold` bound that loss per hour; they do not eliminate it. This is a strictly
larger trust surface than the newly-bonded-only rule it replaced, where the buy set was fully
determined on-chain.

**Root publication.** No on-chain check can verify a merkle root sums to what was deposited, so the
publisher is trusted to compute entitlements honestly. What the contract *does* enforce is that
claims for a coin can never exceed `totalFunded[coin]` — so a bad root can misallocate one coin, but
cannot drain a coin that was never bought, and cannot touch another coin's balance.

Both roles should be a hardened keyed service, and ownership should sit behind a multisig.

**Proofs live off-chain.** `data/proofs.json` is what holders need to claim, and it cannot be
derived from chain state. If it is lost, entitlements are unclaimable until the tree is rebuilt
from the entitlement history. Serve it publicly and back it up.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `epochDuration` | 1 hour | Minimum gap between epochs. |
| `epochSpendBps` | 10000 | Share of spendable ETH deployed per epoch. |
| `maxEpochSpend` | 5 ETH | Hard ceiling on one epoch's spend. |
| `maxTokensPerEpoch` | 10 | Coins bought per epoch. |
| `minReserve` | 0 | ETH never spent. |
| `tokenCooldown` | 12 hours | Gap before the same coin can be bought again. |
| `minGraduationThreshold` | 4.2 ETH | Curve raise a launch needed to qualify. A no-op while Pons has one config. |
| `vaultBps` | 0 | Share retained as permanent backing instead of distributed. |

Graduation recency is deliberately *not* a treasury parameter — see below. Set `MAX_AGE_BLOCKS` on
the keeper instead.

## Layout

```
contracts/
  IPOTreasury.sol         the hourly epoch: claim tax, buy trending coins, route them
  IPODistributor.sol      cumulative merkle claims
  IPOVault.sol            optional permanent backing + pro-rata redemption
  interfaces/IPons.sol        factory, fee escrow, launch records, phases
  interfaces/IUniswapV4.sol   pool manager, pool key, balance delta
  mocks/Mocks.sol         test doubles for Pons and the v4 pool manager
bot/
  runner.ts               the hourly cycle
  trending.ts             scores coins from v4 swap activity
  snapshot.ts             IPO holder balances, rebuilt incrementally from Transfers
  merkle.ts               cumulative tree + proofs
  pons.ts                 launch registry and pool-id derivation
script/deploy.ts          launches IPO on Pons and wires everything up
script/verify.ts          re-checks every Pons assumption against the live chain
config/addresses.ts       chain + Pons v2 deployment addresses
test/ipo.test.ts          42 tests
```

## Build

```bash
npm install
npm run build
npm test
npm run typecheck
npm run verify     # checks the integration against live Robinhood Chain
```

## Deploy

The treasury must be the creator-fee recipient from the first trade, so `deploy.ts` predicts its
address from the deployer's nonce, passes that into the launch, and asserts the treasury landed
there. If it does not, the script prints the `transferCreatorFeeRecipient` call to repoint it.
Nothing is broadcast without `CONFIRM_LAUNCH=yes`.

```bash
CONFIRM_LAUNCH=yes KEEPER_ADDRESS=0x... PRIVATE_KEY=0x... npm run deploy
```

`POOL_MANAGER` and `LAUNCH_CONFIG_ID` now default to the verified mainnet values, and the script
still asserts 400bps clears `maxCreatorTaxBps()` before broadcasting.

It prints the fully populated keeper command on success:

```bash
TREASURY_ADDRESS=0x... DISTRIBUTOR_ADDRESS=0x... VAULT_ADDRESS=0x... \
IPO_ADDRESS=0x... POOL_MANAGER=0x... IPO_DEPLOY_BLOCK=... PONS_FROM_BLOCK=... \
KEEPER_PRIVATE_KEY=0x... npm start
```

The runner polls `epochReady()` every 5 minutes rather than sleeping for exactly an hour, so a
restart or an RPC outage costs one poll interval instead of skipping the hour.

## Verified against mainnet

Everything this repo assumes about Pons was checked against the live deployment on Robinhood Chain.
`npm run verify` re-runs all of it — read-only, no keys — and exits non-zero on any mismatch. Run it
before deploying, and again after any Pons upgrade.

| Assumption | Verified |
| --- | --- |
| Pons contract addresses | All hold code; the factory's own `feeEscrow()`, `memeHook()`, `buybackVault()` getters agree |
| Uniswap v4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` — read from `PonsFactory.poolManager()` **and** `PonsMemeHook.poolManager()`, which agree |
| `maxCreatorTaxBps()` | **1000** (10%). IPO's 400 is well inside it; live launches already carry 400 |
| `TokenLaunched` | `(address,address,address,address,uint256,uint256)` — topic `0x8d4aad49…` |
| `PoolGraduated` | `(address indexed token, uint256 positionId, uint256 tokenAmount, uint256 quoteAmount)` — topic `0x0a44ef75…` |
| v4 `Swap` | `(bytes32,address,int128,int128,uint160,uint128,int24,uint24)` — topic `0x40e9cecb…`, ~1900 in 300 blocks |
| `LaunchedToken` field order | Decodes to itself on real graduated launches, all reporting `phase == 2` |
| Launch config | `launchConfigCount() == 1`; config 0 is supply 1e27, curve fee 1%, phantom quote 1.68 ETH, threshold 4.2 ETH, tickSpacing 200, poolFee 0 |

Two findings changed the code:

**`PoolGraduated` is not what the docs imply.** It carries one indexed argument and three words of
data — the locked position id, the reserved token amount, and the quote deposited — not
`(token, curve, pairToken)`. A watcher built on the documented shape would have matched nothing and
silently bought nothing, forever.

**`sweptQuote`, `sweptTokens` and `sweptAt` are always zero**, including on launches that have fully
graduated. An earlier revision gated purchases on `sweptAt + maxGraduationAge`, which would have
rejected every coin on the chain the moment that parameter was switched on. There is no on-chain
graduation timestamp, so the check was removed rather than left as a trap; recency, if wanted, is a
keeper-side filter (`MAX_AGE_BLOCKS`) using the `PoolGraduated` block. A regression test now pins
eligibility against a record with every `swept*` field zeroed, and `verify` flags it if Pons ever
starts populating them.

One consequence worth knowing: with a single launch config, `graduationThreshold` is *always* 4.2
ETH for ETH-paired launches, so `minGraduationThreshold` is a no-op today. It stays as a guard
against cheaper configs Pons may add later.

## Before mainnet

- [ ] `npm run verify` — should print **all checks passed**.
- [ ] Decide keeper and publisher key custody, and move ownership to a multisig.
- [ ] Calibrate `TREND_WINDOW_BLOCKS` to a real hour of Robinhood Chain blocks, and tune the
      trending weights against real graduation data.
- [ ] Tune `setPolicy(...)` against real volume — the defaults are a starting point, not a
      recommendation.
- [ ] Back up `data/` and serve `proofs.json` publicly. Holders cannot claim without it.
- [ ] Run the keeper on redundant infrastructure.
- [ ] External audit. None of this has been audited.
