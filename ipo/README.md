# IPO — Initial Pons Offering

A token on [Robinhood Chain](https://docs.robinhood.com/chain/) whose 4% tax buys **newly bonded
Pons projects** and hands them to IPO holders.

Every trade in IPO pays 4%. That 4% arrives as ETH. The treasury spends it buying projects the
moment they graduate off the Pons bonding curve, and every project it buys lands in a vault that
IPO holders can redeem against, pro rata, forever.

Hold IPO, own a slice of everything that bonds on Pons.

---

## How the 4% works

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

`buybackEnabled` is off deliberately: Pons buybacks would route the creator's fee share back into
buying IPO and lock it for five years. We want that ETH buying *other people's* newly bonded
projects, which is the entire thesis.

## What it buys

The Pons factory emits `PoolGraduated` when a launch completes its curve and its liquidity is
migrated into a permanently locked Uniswap v4 position. That event is the buy signal.

Before spending anything, `IPOTreasury.buy` re-reads the launch record from the Pons factory and
requires all of:

| Check | Why |
| --- | --- |
| `exists` | It is a real Pons launch, not an arbitrary address. |
| `phase == PoolCreated` | It has actually bonded. Rejects `NotGraduated`, `Swept`, and `Rescued`. |
| `pairToken == address(0)` | ETH-paired, so treasury ETH can buy it directly. |
| `!purchased[token]` | One buy per project, ever. |
| `graduationThreshold >= minGraduationThreshold` | The launch had to raise a real amount to bond. |
| `sweptAt + maxGraduationAge > now` | *Newly* bonded. Default 24h. |
| `now >= lastBuyAt + buyCooldown` | Rate-limits the treasury. Default 5 min. |
| `balance >= buySize + minReserve` | Never spends the reserve. |

`eligibility(token)` exposes the same checks as a view returning `(bool, string reason)`, so the
keeper filters off-chain without burning gas and cannot disagree with the contract.

## How holders get paid

`IPOVault` holds every project the treasury has bought. Any holder can redeem IPO for a pro-rata
slice of it at any time:

```solidity
vault.redeem(ipoAmount, [assetA, assetB, ...], recipient);
```

Distribution is **pull-based and continuous** rather than a periodic push. Each purchase raises the
per-token backing of IPO immediately, for every holder, at zero gas cost to the protocol — no
snapshots, no merkle roots, no unclaimed dust, and no airdrop loop that gets more expensive with
every holder and every asset.

Redeemed IPO is never released again. IPO is a Pons token, so it has no `burn` and cannot be sent
to `address(0)`; locking it in the vault is the equivalent, which is why the vault's own balance is
excluded from `redeemableSupply()`.

**Redeemers pick their assets.** The basket grows without bound, and redeeming all of it in one
transaction would eventually exceed the block gas limit. Taking a subset is strictly favourable to
the holders who remain — the redeemer forfeits their claim on everything they skipped:

> Supply `S`, asset balances `a` and `b`. A holder redeems `r` taking only asset A.
> Claim per token on A: `a/S` before, `a(S−r)/S ÷ (S−r) = a/S` after — unchanged.
> Claim per token on B: `b/S` before, `b/(S−r)` after — **increased**.

Both properties are asserted in the test suite. The asset array must be strictly ascending, which
cheaply rules out the duplicate entries that would otherwise pay a redeemer twice.

## Trust model

**The vault has no withdrawal path for the owner.** Assets enter through the treasury and leave
only through `redeem`. There is no `sweep`, `rescue`, or `emergencyWithdraw` — a test asserts the
ABI contains no such function. The cost is that a token sent here by mistake is stuck unless the
owner registers it as a basket asset, which can only ever hand it to holders.

**The treasury has no withdrawal path either.** ETH that lands there can only leave as a purchase
destined for the vault.

**Keepers cannot redirect funds.** A keeper names a token; the treasury reads that token's launch
record from Pons, rebuilds the pool key itself, and swaps through the pool manager directly. There
is no arbitrary-calldata path. A compromised keeper key can waste `buySize` on a bad launch that
still passes every on-chain filter, and nothing more.

**Known residual risk.** Keepers are allowlisted rather than permissionless, because a newly
graduated pool has no price history to bound slippage against, so `minTokensOut` has to be quoted
off-chain. Opening `buy` to anyone would let a caller pass `minTokensOut = 0` and sandwich the
treasury. Related: anyone willing to fund a launch to graduation could get the treasury to buy
their own project. `buySize`, `buyCooldown`, one-buy-per-token, and `minGraduationThreshold` bound
that loss to a single `buySize`; they do not eliminate it. A permissionless keeper set needs a
TWAP or an oracle first.

## Layout

```
contracts/
  IPOTreasury.sol         claims the tax, buys graduated projects, funds the vault
  IPOVault.sol            basket custody + pro-rata redemption
  interfaces/IPons.sol        factory, fee escrow, launch records, phases
  interfaces/IUniswapV4.sol   pool manager, pool key, balance delta
  mocks/Mocks.sol         test doubles for Pons and the v4 pool manager
bot/watcher.ts            watches PoolGraduated, quotes, submits buys
script/deploy.ts          launches IPO on Pons and wires everything up
config/addresses.ts       chain + Pons v2 deployment addresses
test/ipo.test.ts          28 tests
```

## Build

```bash
npm install
npm run build
npm test
```

## Deploy

The treasury must be the creator-fee recipient from the first trade, so `deploy.ts` predicts its
address from the deployer's nonce, passes that into the launch, and asserts the treasury landed
there. If it does not, the script prints the `transferCreatorFeeRecipient` call to repoint it.

```bash
CONFIRM_LAUNCH=yes \
POOL_MANAGER=0x...        \
KEEPER_ADDRESS=0x...      \
PRIVATE_KEY=0x...         \
npx hardhat run script/deploy.ts --network robinhood
```

Then run the keeper:

```bash
TREASURY_ADDRESS=0x... KEEPER_PRIVATE_KEY=0x... npm run watch
```

## Before mainnet

- [ ] **Fill in `UNISWAP_V4.poolManager`** in `config/addresses.ts`. It is the one address I could
      not verify for Robinhood Chain; `deploy.ts` refuses to run without it.
- [ ] **Confirm the `PoolGraduated` signature.** The Pons docs name the event but not its
      parameters. `bot/watcher.ts` and `interfaces/IPons.sol` assume
      `(address indexed token, address indexed curve, address pairToken)`. Verify against the
      deployed factory ABI — a mismatch means the bot silently sees nothing.
- [ ] **Confirm the `LaunchedToken` field order** in `interfaces/IPons.sol` against the deployed
      factory. It is decoded from the docs, and a reordered struct would misread `phase`.
- [ ] Check `maxCreatorTaxBps()` on-chain. `deploy.ts` asserts 400 clears it, but knowing the
      number in advance is worth a single `eth_call`.
- [ ] Decide the launch config id (`LAUNCH_CONFIG_ID`), which fixes supply, curve fee, phantom
      quote, graduation threshold, and tick spacing.
- [ ] Tune `setPolicy(buySize, minReserve, buyCooldown, maxGraduationAge, minGraduationThreshold)`
      against real graduation volume. Defaults are 0.05 ETH / 0 / 5 min / 24 h / 4.2 ETH.
- [ ] Move ownership to a multisig and run a keeper on redundant infrastructure.
- [ ] External audit. None of this has been audited.

## Assumptions I made

You dismissed the design questionnaire, so I picked defaults. Each of these is a real fork and
cheap to change now:

1. **Pons-native creator tax over a custom tax token.** IPO is a Pons launch, which fits "Initial
   Pons Offering" literally and gets the tax in ETH. The alternative — deploying IPO ourselves with
   a transfer tax and our own v4 hook — gives more control over the fee split but means bootstrapping
   liquidity and selling IPO to fund every purchase.
2. **Redeemable vault over merkle-epoch airdrops.** Reasoning above. If you specifically want
   visible periodic dividends, a merkle distributor is roughly 100 lines and can sit alongside this;
   the treasury would split purchases between the two.
3. **Filtered auto-buy over buy-everything.** ~250k tokens have launched on Pons; spreading the
   treasury across every graduation buys a lot of dead projects. The filters are all parameters —
   set `minGraduationThreshold` to 0 and `maxGraduationAge` high to approach buy-everything.
4. **Allowlisted keepers.** See the trust model. This is the assumption I'd revisit first.
5. **ETH-paired launches only.** Pons supports pairing against approved ERC-20s including tokenized
   stocks. Buying those needs the treasury to hold the quote asset; `claimTaxToken` handles tax that
   arrives in an ERC-20 by passing it straight to the vault, but the buy path is ETH-only.
