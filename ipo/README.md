# IPO — Initial Pons Offering

A token on [Robinhood Chain](https://docs.robinhood.com/chain/) with a **4/4 tax** that, every hour,
buys whatever is trending on Pons and **airdrops it straight to holders**.

```
  every IPO trade                  once an hour
  ──────────────                   ────────────
  4% buy / 4% sell ──► IPOTreasury ──► buy the top trending Pons coins
                       (holds ETH)          │
                                            ├──► IPOAirdropper ──► pushed to holders' wallets
                                            └──► IPOVault (optional) ──► permanent backing
```

Hold IPO. Coins show up in your wallet every hour. No claiming, no approvals, no gas.

---

## The 4/4

Pons levies it, not us.

A Pons token is fixed-supply with no mint, freeze, blacklist, or tax-raising after launch — so a
conventional ERC-20 transfer tax is not available on a Pons launch. What Pons provides is
`creatorTaxBps`, chosen freely at launch and immutable after. IPO launches with:

```
creatorTaxBps       = 400            // 4% in, 4% out
creatorFeeRecipient = IPOTreasury
buybackEnabled      = false
pairToken           = native ETH
```

Pons charges the creator tax on the **quote side of both directions** — deducted from quote input on
buys, from quote output on sells — so that single 400 is the 4/4. It applies on the bonding curve
*and* after graduation, where the Pons meme hook collects it. The protocol cap is 1000 (verified
on-chain), so 4/4 sits comfortably inside it.

The tax arrives as **ETH**, and that is the part that matters. A conventional tax token accumulates
its own token and must sell it to do anything useful, making the treasury a permanent sell-side flow
against its own holders. Here the treasury never touches IPO. It receives ETH and spends ETH.

`buybackEnabled` is off deliberately: Pons buybacks would route the creator's fee share into buying
IPO and lock it for five years. We want that ETH buying *other people's* coins.

## The hourly cycle

One `runEpoch` call per hour:

1. **Sweep** any tax accrued in the Pons escrow since the last epoch, so it is deployed this hour.
2. **Buy** the trending coins the keeper selected, splitting the epoch budget equally between them.
3. **Route** each purchase to the airdropper (and optionally the vault).

Then the keeper snapshots IPO holders and pushes the coins out in batches.

The entire basket is bought under a **single pool-manager unlock**: the swaps accumulate one net ETH
debt settled once. Cheaper than unlocking per coin, and it makes the hour atomic — either the whole
basket lands or none of it does.

The budget is `min((balance − minReserve) × epochSpendBps, maxEpochSpend)`, divided equally across
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
direction alongside it makes that meaningfully more expensive. Weights are a parameter — tune them
against real data.

v4 emits one `Swap` per *pool id*, not per token, so the bot rebuilds each Pons pool's id
(`keccak256(abi.encode(PoolKey))`, native ETH always `currency0`) and matches events against that set.

## The airdrop

Holders do nothing. The keeper snapshots IPO balances, splits each coin pro rata, and sends the
transfers in batches. Coins land in wallets.

Batches are idempotent by `(epochId, batchIndex, token)`. A keeper that dies mid-airdrop and
restarts re-sends the same batch id, which reverts rather than paying twice. Idempotency is at batch
rather than recipient granularity on purpose: the keeper is already trusted to compute the split, so
the risk worth engineering against is a crash-and-retry, not a keeper choosing to pay someone twice.

Each hour distributes `undistributed(token)` — everything funded but not yet sent — rather than just
that hour's purchase. Rounding dust, holders skipped by the dust floor, and any batch that failed all
roll forward automatically instead of being stranded.

Every coin bought is a Pons launch, and Pons tokens are plain ERC-20s with no blacklist or transfer
hook, so a recipient cannot make a transfer revert. One hostile address cannot brick a batch.

### Gas, honestly

Push airdrops cost real gas and the cost grows with holders × coins. Measured at **~27,500 gas per
recipient** (worst case: a fresh address, cold balance slot):

| Holders | Coins/hour | Transfers/hour | Gas/hour | Txs/hour at 250/batch |
| --- | --- | --- | --- | --- |
| 250 | 5 | 1,250 | ~34M | 5 |
| 1,000 | 10 | 10,000 | ~275M | 40 |
| 5,000 | 10 | 50,000 | ~1.4B | 200 |

Robinhood Chain is a cheap L2, but the right-hand column is the one to watch — at 5,000 holders you
are sending a transaction every 18 seconds, forever. Three levers, in order of bluntness:

- **`MIN_PAYOUT`** — skip shares below a floor. Token holder distributions have a long dust tail;
  this usually removes most recipients for a small fraction of the value. Skipped shares roll
  forward rather than being lost.
- **Fewer coins per epoch** (`maxTokensPerEpoch`) — the multiplier in the table.
- **Longer epochs** (`epochDuration`) — fewer, larger distributions.

If holder count ever outgrows all three, the fix is a pull-based claim; the airdropper's accounting
(`totalFunded` / `totalSent` per token) is already the right shape to sit under one.

### Optional permanent backing

`vaultBps` splits each purchase between the airdropper and `IPOVault`, which holds its share forever
and lets holders redeem a pro-rata slice by locking IPO. Defaults to **0** — everything is airdropped.
Raising it trades current yield for a hard NAV floor under IPO.

Redeemers choose which assets to take, since the basket grows without bound. Skipping an asset
forfeits that claim and *raises* per-token backing for everyone who stays:

> Supply `S`, balances `a` and `b`. A holder redeems `r` taking only asset A.
> Claim per token on A: `a/S` before, `a(S−r)/S ÷ (S−r) = a/S` after — unchanged.
> Claim per token on B: `b/S` before, `b/(S−r)` after — **increased**.

Both properties are asserted in the tests.

## Trust model

**The treasury and vault have no owner withdrawal path.** ETH that reaches the treasury can only
leave as a purchase; vault assets can only leave as a redemption. A test asserts neither ABI contains
`withdraw`, `sweep`, `rescue`, `emergency`, `recover`, or `skim`.

**Keepers cannot redirect the buying.** A keeper submits a list of tokens. The treasury reads each
one's launch record from the Pons factory, rebuilds the v4 pool key itself, and swaps through the
pool manager directly. No arbitrary-calldata path. It independently enforces:

| Check | Why |
| --- | --- |
| `exists` | A real Pons launch, not an arbitrary address. |
| `phase == PoolCreated` | Actually bonded. Rejects `NotGraduated`, `Swept`, `Rescued`. |
| `pairToken == address(0)` | ETH-paired, so treasury ETH can buy it directly. |
| `graduationThreshold >= minGraduationThreshold` | The launch had to raise a real amount. |
| `now >= lastBoughtAt[token] + tokenCooldown` | One coin cannot be bought hour after hour. |
| strictly ascending orders | Rules out duplicates taking several slices of the budget. |
| `orders.length <= maxTokensPerEpoch` | Bounds the gas of one epoch. |
| `epochBudget()` | Caps ETH at risk in any single hour. |

### What is actually trusted

**Coin selection.** "Trending" is off-chain by nature, so the keeper decides what the treasury buys.
The contract bounds *what* it accepts and *how much* it spends, but not which coin trends. A
compromised keeper can waste an hour's budget on coins that pass every filter — including ones it
launched and funded to graduation itself. `maxEpochSpend`, `tokenCooldown` and
`minGraduationThreshold` bound that per hour; they do not eliminate it.

**Who gets the airdrop.** The keeper picks recipients and amounts. A compromised keeper can send an
epoch's coins to itself. The contract's guarantee is narrower but real: a token can never pay out
more than the treasury funded for it, so a bad split can misallocate one coin but cannot overdraw one
or touch another coin's balance. This is the same exposure a merkle-claim design would have — an
attacker publishing a root allocating everything to themselves — so pushing rather than pulling costs
nothing in trust; it costs gas.

### Keys

A multisig is about single-key loss and single-key compromise, not headcount — but for a solo
operator the thing that matters more is that **the owner key is not the keeper key**.

The keeper is hot: it signs unattended, every hour, from a server. The owner is not, and should be a
hardware wallet that never touches that machine. `deploy.ts` refuses to wire the same address into
both unless you pass `ALLOW_SHARED_KEY=yes`.

| If this leaks | Blast radius |
| --- | --- |
| **Keeper** | One epoch's ETH budget, plus coins currently sitting undistributed. Owner can `pause()` both contracts and `setKeeper(false)`. Roughly an hour of flow. |
| **Owner** | Everything. `setKeeper` + `setPolicy` + `setAirdropper` drains the treasury and all undistributed coins. |
| **Owner, for vault assets** | Nothing. The vault has no withdrawal path, so backed assets survive even a fully compromised owner. |

Two more things worth knowing:

- **Losing the owner key is unrecoverable.** No pause, no keeper rotation, no policy change, ever —
  while the treasury keeps accruing tax with no way to spend or stop it. That is the real argument
  for 2-of-3 even when the multisig is only you: a hardware wallet, a phone, and a paper backup is
  still one person, but survives losing any one of them.
- All three contracts use `Ownable2Step`, so ownership only moves once the new owner calls
  `acceptOwnership()`. A mistyped address cannot strand them. Pass `OWNER_ADDRESS` at deploy to hand
  ownership to a cold key, and the script prints the accept steps.

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `epochDuration` | 1 hour | Minimum gap between epochs. |
| `epochSpendBps` | 10000 | Share of spendable ETH deployed per epoch. |
| `maxEpochSpend` | 5 ETH | Hard ceiling on one epoch's spend. |
| `maxTokensPerEpoch` | 10 | Coins bought per epoch. |
| `minReserve` | 0 | ETH never spent. |
| `tokenCooldown` | 12 hours | Gap before the same coin can be bought again. |
| `minGraduationThreshold` | 4.2 ETH | Curve raise needed to qualify. A no-op while Pons has one config. |
| `vaultBps` | 0 | Share retained as permanent backing instead of airdropped. |

Keeper-side: `AIRDROP_BATCH_SIZE` (250), `MIN_PAYOUT` (0), `TREND_WINDOW_BLOCKS` (14400),
`MAX_AGE_BLOCKS` (0, off), `SLIPPAGE_BPS` (500), `POLL_MS` (5 min).

Graduation recency is deliberately *not* a treasury parameter — see below.

## Layout

```
contracts/
  IPOTreasury.sol         the hourly epoch: claim tax, buy trending coins, route them
  IPOAirdropper.sol       batched pro-rata transfers to holders
  IPOVault.sol            optional permanent backing + pro-rata redemption
  interfaces/IPons.sol        factory, fee escrow, launch records, phases
  interfaces/IUniswapV4.sol   pool manager, pool key, balance delta
  mocks/Mocks.sol         test doubles for Pons and the v4 pool manager
bot/
  runner.ts               the hourly cycle
  trending.ts             scores coins from v4 swap activity
  snapshot.ts             IPO holder balances, rebuilt incrementally from Transfers
  airdrop.ts              pro-rata split and batching
  pons.ts                 launch registry and pool-id derivation
script/deploy.ts          launches IPO on Pons and wires everything up
script/verify.ts          re-checks every Pons assumption against the live chain
config/addresses.ts       chain + verified Pons v2 / Uniswap v4 addresses
test/ipo.test.ts          45 unit tests against a mock pool manager
test/fork.test.ts         buys a real coin on the real v4 pool (opt-in)
```

## Build

```bash
npm install
npm run build
npm test
npm run typecheck
npm run verify     # checks the integration against live Robinhood Chain
```

The unit suite runs offline against a mock pool manager. The **fork test** buys a real graduated
Pons coin on the real Uniswap v4 pool through the real Pons hook — the one thing a mock cannot prove:

```bash
FORK_BLOCK=52202800 npx hardhat test test/fork.test.ts
```

It confirms the pool key reconstructed from a launch record actually identifies the pool, that the
swap direction is right, and that settle/take balances against v4's flash accounting with a hook in
the path. Last run bought 169,369.96 tokens for 0.02 ETH with the treasury's ETH exactly reconciled.

## Deploy

The treasury must be the creator-fee recipient from the first trade, so `deploy.ts` predicts its
address from the deployer's nonce, passes that into the launch, and asserts the treasury landed
there. If it does not, the script prints the `transferCreatorFeeRecipient` call to repoint it.
Nothing is broadcast without `CONFIRM_LAUNCH=yes`.

```bash
CONFIRM_LAUNCH=yes \
KEEPER_ADDRESS=0x...   # hot key the bot signs with
OWNER_ADDRESS=0x...    # cold key that ends up owning the contracts
PRIVATE_KEY=0x... npm run deploy
```

`POOL_MANAGER` and `LAUNCH_CONFIG_ID` default to the verified mainnet values, and the script asserts
400bps clears `maxCreatorTaxBps()` before broadcasting. It prints the keeper command on success:

```bash
TREASURY_ADDRESS=0x... AIRDROPPER_ADDRESS=0x... VAULT_ADDRESS=0x... \
IPO_ADDRESS=0x... POOL_MANAGER=0x... IPO_DEPLOY_BLOCK=... PONS_FROM_BLOCK=... \
KEEPER_PRIVATE_KEY=0x... npm start
```

The runner polls `epochReady()` every 5 minutes rather than sleeping for exactly an hour, so a
restart or RPC outage costs one poll interval instead of skipping the hour.

## Verified against mainnet

Everything this repo assumes about Pons was checked against the live deployment. `npm run verify`
re-runs all of it — read-only, no keys — and exits non-zero on any mismatch.

| Assumption | Verified |
| --- | --- |
| Pons contract addresses | All hold code; the factory's own `feeEscrow()`, `memeHook()`, `buybackVault()` getters agree |
| Uniswap v4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` — from `PonsFactory.poolManager()` **and** `PonsMemeHook.poolManager()`, which agree |
| `maxCreatorTaxBps()` | **1000** (10%). IPO's 400 is inside it; live launches already carry 400 |
| `TokenLaunched` | `(address,address,address,address,uint256,uint256)` — topic `0x8d4aad49…` |
| `PoolGraduated` | `(address indexed token, uint256 positionId, uint256 tokenAmount, uint256 quoteAmount)` — topic `0x0a44ef75…` |
| v4 `Swap` | `(bytes32,address,int128,int128,uint160,uint128,int24,uint24)` — topic `0x40e9cecb…` |
| `LaunchedToken` field order | Decodes to itself on real graduated launches, all reporting `phase == 2` |
| Launch config | `launchConfigCount() == 1`; config 0 is supply 1e27, curve fee 1%, phantom quote 1.68 ETH, threshold 4.2 ETH, tickSpacing 200, poolFee 0 |
| The swap itself | Fork test buys a real coin on the real pool through the real hook |

Two findings changed the code:

**`PoolGraduated` is not what the docs imply.** It carries one indexed argument and three words of
data — locked position id, reserved token amount, quote deposited — not `(token, curve, pairToken)`.
A watcher built on the documented shape would have matched nothing and silently bought nothing,
forever.

**`sweptQuote`, `sweptTokens` and `sweptAt` are always zero**, including on fully graduated launches.
An earlier revision gated purchases on `sweptAt + maxGraduationAge`, which would have rejected every
coin on the chain the moment that parameter was switched on. There is no on-chain graduation
timestamp, so the check was removed rather than left as a trap; recency, if wanted, is a keeper-side
filter (`MAX_AGE_BLOCKS`) using the `PoolGraduated` block. A regression test pins eligibility against
a record with every `swept*` field zeroed, and `verify` flags it if Pons starts populating them.

One consequence worth knowing: with a single launch config, `graduationThreshold` is *always* 4.2 ETH
for ETH-paired launches, so `minGraduationThreshold` is a no-op today. It stays as a guard against
cheaper configs Pons may add later.

## Before mainnet

- [ ] `npm run verify` — should print **all checks passed**.
- [ ] Token metadata for the launch: logo URI, description, socials. Baked into the Pons record.
- [ ] A keeper address (hot) and an owner address (cold). They must differ — see **Keys** above.
- [ ] Set `MIN_PAYOUT` from the gas table above once you have a sense of holder count.
- [ ] Calibrate `TREND_WINDOW_BLOCKS` to a real hour of Robinhood Chain blocks, and tune the trending
      weights against real data.
- [ ] Tune `setPolicy(...)` against expected tax revenue — the defaults are a starting point.
- [ ] Fund the keeper with enough ETH for ~40 airdrop transactions an hour at target scale.
- [ ] Run the keeper on redundant infrastructure, and back up the owner key so losing it cannot
      strand the contracts.
- [ ] External audit. None of this has been audited.
