import { expect } from "chai";
import { ethers } from "hardhat";
import { loadFixture, time } from "@nomicfoundation/hardhat-network-helpers";
import { CumulativeMerkleTree, accrue, type Entitlement } from "../bot/merkle";
import type { Address } from "viem";

const ETH = (n: string) => ethers.parseEther(n);
const IPO_SUPPLY = ETH("1000000000"); // 1B, Pons fixed supply
const HOUR = 3600;
const RATE = 1000n; // mock pool: tokens out per wei in

/** Build a Pons `LaunchedToken` record as the factory would report it. */
async function record(token: string, over: Record<string, any> = {}) {
  const now = await time.latest();
  return {
    token,
    curve: ethers.ZeroAddress,
    deployer: ethers.ZeroAddress,
    creatorFeeRecipient: ethers.ZeroAddress,
    pairToken: ethers.ZeroAddress, // native ETH
    graduationThreshold: ETH("4.2"),
    poolFee: 0n,
    tickSpacing: 60n,
    creatorTaxBps: 0n,
    buybackEnabled: false,
    phase: 2n, // PoolCreated
    // The live factory leaves all three swept* fields at zero even on graduated launches,
    // so the mock does too — nothing on-chain may depend on them.
    sweptQuote: 0n,
    sweptTokens: 0n,
    sweptAt: 0n,
    exists: true,
    ...over,
  };
}

/** Orders must be strictly ascending by token address. */
const sortOrders = (orders: { token: string; minTokensOut: bigint }[]) =>
  [...orders].sort((a, b) => (a.token.toLowerCase() < b.token.toLowerCase() ? -1 : 1));

describe("IPO — Initial Pons Offering", () => {
  async function deployFixture() {
    const [owner, keeper, alice, bob, carol, curve] = await ethers.getSigners();

    const ERC20 = await ethers.getContractFactory("MockERC20");
    const ipo = await ERC20.deploy("Initial Pons Offering", "IPO", IPO_SUPPLY);

    const factory = await (await ethers.getContractFactory("MockPonsFactory")).deploy();
    const escrow = await (await ethers.getContractFactory("MockPonsFeeEscrow")).deploy();
    const pm = await (await ethers.getContractFactory("MockPoolManager")).deploy();
    const memeHook = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044";

    const ipoAddr = await ipo.getAddress();
    const vault = await (await ethers.getContractFactory("IPOVault")).deploy(ipoAddr, owner.address);
    const distributor = await (
      await ethers.getContractFactory("IPODistributor")
    ).deploy(owner.address);
    const treasury = await (
      await ethers.getContractFactory("IPOTreasury")
    ).deploy(
      await factory.getAddress(),
      await escrow.getAddress(),
      await pm.getAddress(),
      memeHook,
      ipoAddr,
      owner.address
    );

    const treasuryAddr = await treasury.getAddress();
    await vault.setTreasury(treasuryAddr);
    await distributor.setTreasury(treasuryAddr);
    await distributor.setPublisher(keeper.address);
    await treasury.setDistributor(await distributor.getAddress());
    await treasury.setVault(await vault.getAddress());
    await treasury.setKeeper(keeper.address, true);

    /** Register a graduated Pons coin with liquidity sitting in the pool manager. */
    async function addCoin(symbol: string, over: Record<string, any> = {}) {
      const coin = await ERC20.deploy(symbol, symbol, ETH("100000000"));
      await coin.transfer(await pm.getAddress(), ETH("100000000"));
      const addr = await coin.getAddress();
      await factory.setRecord(addr, await record(addr, over));
      return coin;
    }

    const coinA = await addCoin("AAA");
    const coinB = await addCoin("BBB");

    return {
      owner, keeper, alice, bob, carol, curve,
      ipo, factory, escrow, pm, vault, distributor, treasury,
      coinA, coinB, addCoin,
    };
  }

  /** Fund the treasury the way Pons does: tax accrues in the escrow, treasury claims it. */
  async function fundTreasury(f: any, amount: bigint) {
    await f.escrow.credit(await f.treasury.getAddress(), { value: amount });
    await f.treasury.claimTax();
  }

  /** Run one hourly epoch buying the given coins. */
  async function runEpoch(f: any, coins: any[], minOuts?: bigint[]) {
    const orders = sortOrders(
      await Promise.all(
        coins.map(async (c, i) => ({
          token: await c.getAddress(),
          minTokensOut: minOuts?.[i] ?? 0n,
        }))
      )
    );
    const deadline = (await time.latest()) + 600;
    return f.treasury.connect(f.keeper).runEpoch(orders, deadline);
  }

  // =====================================================================
  describe("tax collection", () => {
    it("claims the creator tax out of the Pons fee escrow as ETH", async () => {
      const f = await loadFixture(deployFixture);
      const treasuryAddr = await f.treasury.getAddress();

      await f.escrow.credit(treasuryAddr, { value: ETH("3") });
      await expect(f.treasury.claimTax()).to.emit(f.treasury, "TaxClaimed").withArgs(ETH("3"));

      expect(await ethers.provider.getBalance(treasuryAddr)).to.equal(ETH("3"));
      expect(await f.treasury.totalTaxClaimed()).to.equal(ETH("3"));
    });

    it("reverts when there is nothing accrued", async () => {
      const f = await loadFixture(deployFixture);
      await expect(f.treasury.claimTax()).to.be.revertedWithCustomError(f.treasury, "NothingToClaim");
    });

    it("sweeps tax accrued since the last epoch as part of running one", async () => {
      const f = await loadFixture(deployFixture);
      // Nothing claimed up front; the tax is sitting in the escrow.
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });

      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "TaxClaimed").withArgs(ETH("1"));
      expect(await f.treasury.totalEthSpent()).to.equal(ETH("1"));
    });
  });

  // =====================================================================
  describe("the hourly epoch", () => {
    it("buys the trending coins the keeper selected and splits the budget equally", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));

      await expect(runEpoch(f, [f.coinA, f.coinB]))
        .to.emit(f.treasury, "EpochRun")
        .withArgs(1, 2, ETH("1"));

      const perToken = ETH("0.5");
      const expectedOut = perToken * RATE;
      const distAddr = await f.distributor.getAddress();
      expect(await f.coinA.balanceOf(distAddr)).to.equal(expectedOut);
      expect(await f.coinB.balanceOf(distAddr)).to.equal(expectedOut);
      expect(await f.treasury.epoch()).to.equal(1n);
      expect(await f.treasury.totalPurchases()).to.equal(2n);
    });

    it("will not run again inside the hour", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("2"));
      await runEpoch(f, [f.coinA]);

      await time.increase(HOUR - 60);
      await expect(runEpoch(f, [f.coinB])).to.be.revertedWithCustomError(
        f.treasury,
        "EpochNotElapsed"
      );

      // The first epoch deployed the whole balance, so top up before the next one.
      await time.increase(120);
      await fundTreasury(f, ETH("1"));
      await expect(runEpoch(f, [f.coinB])).to.emit(f.treasury, "EpochRun");
    });

    it("buys the same coin again only after its own cooldown", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("3"));
      await runEpoch(f, [f.coinA]);

      await time.increase(HOUR + 1);
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(
        f.treasury,
        "TokenOnCooldown"
      );

      await time.increase(12 * HOUR);
      await fundTreasury(f, ETH("1"));
      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "EpochRun");
    });

    it("rejects duplicate or unsorted orders that would take several slices of the budget", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const a = await f.coinA.getAddress();
      const deadline = (await time.latest()) + 600;

      await expect(
        f.treasury.connect(f.keeper).runEpoch(
          [
            { token: a, minTokensOut: 0n },
            { token: a, minTokensOut: 0n },
          ],
          deadline
        )
      ).to.be.revertedWithCustomError(f.treasury, "DuplicateOrUnsorted");

      const b = await f.coinB.getAddress();
      const descending = sortOrders([
        { token: a, minTokensOut: 0n },
        { token: b, minTokensOut: 0n },
      ]).reverse();
      await expect(
        f.treasury.connect(f.keeper).runEpoch(descending, deadline)
      ).to.be.revertedWithCustomError(f.treasury, "DuplicateOrUnsorted");
    });

    it("caps how many coins one epoch may buy", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.treasury.setPolicy(HOUR, 10_000, ETH("5"), 1, 0, 12 * HOUR, ETH("4.2"));

      await expect(runEpoch(f, [f.coinA, f.coinB]))
        .to.be.revertedWithCustomError(f.treasury, "TooManyOrders")
        .withArgs(2, 1);
    });

    it("deploys only the configured share of the balance", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      // 25% per epoch.
      await f.treasury.setPolicy(HOUR, 2_500, ETH("5"), 10, 0, 12 * HOUR, ETH("4.2"));

      expect(await f.treasury.epochBudget()).to.equal(ETH("0.25"));
      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "EpochRun").withArgs(1, 1, ETH("0.25"));
    });

    it("never spends past the per-epoch ceiling", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("100"));
      expect(await f.treasury.epochBudget()).to.equal(ETH("5")); // maxEpochSpend
      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "EpochRun").withArgs(1, 1, ETH("5"));
    });

    it("never spends the reserve", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.treasury.setPolicy(HOUR, 10_000, ETH("5"), 10, ETH("1"), 12 * HOUR, ETH("4.2"));

      expect(await f.treasury.spendable()).to.equal(0n);
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(
        f.treasury,
        "NothingToSpend"
      );
    });

    it("enforces the slippage bound per coin", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const tooMuch = ETH("1") * RATE * 2n;
      await expect(runEpoch(f, [f.coinA], [tooMuch])).to.be.revertedWithCustomError(
        f.treasury,
        "SlippageExceeded"
      );
    });

    it("honours the deadline", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const past = (await time.latest()) - 1;
      await expect(
        f.treasury
          .connect(f.keeper)
          .runEpoch([{ token: await f.coinA.getAddress(), minTokensOut: 0n }], past)
      ).to.be.revertedWithCustomError(f.treasury, "DeadlinePassed");
    });

    it("only keepers can run an epoch", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      await expect(
        f.treasury
          .connect(f.alice)
          .runEpoch([{ token: await f.coinA.getAddress(), minTokensOut: 0n }], deadline)
      ).to.be.revertedWithCustomError(f.treasury, "NotKeeper");
    });

    it("stops when paused", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.treasury.pause();
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(f.treasury, "EnforcedPause");
    });

    it("rejects an unlockCallback from anyone but the pool manager", async () => {
      const f = await loadFixture(deployFixture);
      await expect(
        f.treasury.connect(f.alice).unlockCallback("0x")
      ).to.be.revertedWithCustomError(f.treasury, "OnlyPoolManager");
    });
  });

  // =====================================================================
  describe("what it will and will not buy", () => {
    it("refuses a coin that has not bonded yet", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.factory.setPhase(await f.coinA.getAddress(), 0);
      await expect(runEpoch(f, [f.coinA]))
        .to.be.revertedWithCustomError(f.treasury, "NotGraduated")
        .withArgs(await f.coinA.getAddress(), 0);
    });

    it("refuses a launch that used the rescue path", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.factory.setPhase(await f.coinA.getAddress(), 3);
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(f.treasury, "NotGraduated");
    });

    it("refuses a token that is not a Pons launch at all", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      await expect(
        f.treasury.connect(f.keeper).runEpoch([{ token: f.alice.address, minTokensOut: 0n }], deadline)
      ).to.be.revertedWithCustomError(f.treasury, "NotAPonsLaunch");
    });

    it("refuses a coin paired against something other than ETH", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const addr = await f.coinA.getAddress();
      await f.factory.setRecord(addr, await record(addr, { pairToken: f.alice.address }));
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(f.treasury, "NotEthPaired");
    });

    it("refuses a launch whose graduation threshold was below the floor", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const addr = await f.coinA.getAddress();
      await f.factory.setRecord(addr, await record(addr, { graduationThreshold: ETH("0.1") }));
      await expect(runEpoch(f, [f.coinA])).to.be.revertedWithCustomError(f.treasury, "ThresholdTooLow");
    });

    it("buys a coin that bonded long ago, since trending is not about recency", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await time.increase(30 * 24 * HOUR);
      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "EpochRun");
    });

    it("does not depend on sweptAt, which the live factory never populates", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const addr = await f.coinA.getAddress();

      // Exactly what mainnet reports for a graduated launch: phase 2, every swept* field zero.
      await f.factory.setRecord(
        addr,
        await record(addr, { phase: 2n, sweptQuote: 0n, sweptTokens: 0n, sweptAt: 0n })
      );

      const [ok] = await f.treasury.eligibility(addr);
      expect(ok).to.equal(true);
      await expect(runEpoch(f, [f.coinA])).to.emit(f.treasury, "EpochRun");
    });

    it("reports eligibility off-chain the same way it enforces it on-chain", async () => {
      const f = await loadFixture(deployFixture);
      const addr = await f.coinA.getAddress();

      let [ok, reason] = await f.treasury.eligibility(addr);
      expect(ok).to.equal(true);

      await f.factory.setPhase(addr, 0);
      [ok, reason] = await f.treasury.eligibility(addr);
      expect(ok).to.equal(false);
      expect(reason).to.equal("not graduated");
    });

    it("reports when an epoch is ready to run", async () => {
      const f = await loadFixture(deployFixture);

      let [ok, reason] = await f.treasury.epochReady();
      expect(ok).to.equal(false);
      expect(reason).to.equal("nothing to spend");

      await fundTreasury(f, ETH("1"));
      [ok] = await f.treasury.epochReady();
      expect(ok).to.equal(true);

      await runEpoch(f, [f.coinA]);
      [ok, reason] = await f.treasury.epochReady();
      expect(ok).to.equal(false);
      expect(reason).to.equal("epoch not elapsed");
    });

    it("counts tax still sitting in the escrow as spendable", async () => {
      const f = await loadFixture(deployFixture);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      const [ok] = await f.treasury.epochReady();
      expect(ok).to.equal(true);
    });
  });

  // =====================================================================
  describe("routing purchases", () => {
    it("sends everything to holders by default", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      expect(await f.treasury.vaultBps()).to.equal(0n);

      await runEpoch(f, [f.coinA]);
      const bought = ETH("1") * RATE;
      expect(await f.coinA.balanceOf(await f.distributor.getAddress())).to.equal(bought);
      expect(await f.coinA.balanceOf(await f.vault.getAddress())).to.equal(0n);
      expect(await f.distributor.totalFunded(await f.coinA.getAddress())).to.equal(bought);
    });

    it("splits between holders and permanent backing when configured", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.treasury.setVaultBps(2_500); // 25% retained

      const bought = ETH("1") * RATE;
      await expect(runEpoch(f, [f.coinA]))
        .to.emit(f.treasury, "Bought")
        .withArgs(await f.coinA.getAddress(), ETH("1"), bought, (bought * 75n) / 100n, (bought * 25n) / 100n);

      expect(await f.coinA.balanceOf(await f.vault.getAddress())).to.equal((bought * 25n) / 100n);
      expect(await f.coinA.balanceOf(await f.distributor.getAddress())).to.equal((bought * 75n) / 100n);
    });

    it("rejects a split above 100%", async () => {
      const f = await loadFixture(deployFixture);
      await expect(f.treasury.setVaultBps(10_001)).to.be.revertedWithCustomError(
        f.treasury,
        "InvalidBps"
      );
    });
  });

  // =====================================================================
  describe("distribution to holders", () => {
    /** Run an epoch, then publish a root paying out pro rata to the IPO holders. */
    async function distribute(f: any, prior: Entitlement[] = []): Promise<Entitlement[]> {
      const holders: [Address, bigint][] = [
        [f.alice.address as Address, await f.ipo.balanceOf(f.alice.address)],
        [f.bob.address as Address, await f.ipo.balanceOf(f.bob.address)],
        [f.carol.address as Address, await f.ipo.balanceOf(f.carol.address)],
      ];
      const balances = new Map(holders.filter(([, b]) => b > 0n));

      const coinAddr = (await f.coinA.getAddress()) as Address;
      const funded = await f.distributor.totalFunded(coinAddr);
      const alreadyOwed = prior
        .filter((e) => e.token.toLowerCase() === coinAddr.toLowerCase())
        .reduce((a, e) => a + e.cumulativeAmount, 0n);

      const purchases = new Map<Address, bigint>([[coinAddr, funded - alreadyOwed]]);
      const next = accrue(prior, balances, purchases);
      const tree = new CumulativeMerkleTree(next);
      await f.distributor.connect(f.keeper).publishRoot(tree.root);
      return next;
    }

    async function withHolders() {
      const f = await loadFixture(deployFixture);
      // 60/30/10 split of a tenth of supply; the rest stays with the deployer and is not entitled.
      await f.ipo.transfer(f.alice.address, ETH("60000000"));
      await f.ipo.transfer(f.bob.address, ETH("30000000"));
      await f.ipo.transfer(f.carol.address, ETH("10000000"));
      await fundTreasury(f, ETH("1"));
      await runEpoch(f, [f.coinA]);
      return f;
    }

    it("pays each holder their pro-rata share of the hour's buys", async () => {
      const f = await withHolders();
      const entitlements = await distribute(f);
      const coin = (await f.coinA.getAddress()) as Address;
      const tree = new CumulativeMerkleTree(entitlements);

      const aliceE = entitlements.find(
        (e) => e.account.toLowerCase() === f.alice.address.toLowerCase()
      )!;
      await f.distributor.claim(f.alice.address, coin, aliceE.cumulativeAmount, tree.proof(aliceE));

      const bought = ETH("1") * RATE;
      expect(await f.coinA.balanceOf(f.alice.address)).to.equal((bought * 60n) / 100n);
      expect(aliceE.cumulativeAmount).to.equal((bought * 60n) / 100n);
    });

    it("accumulates across hours so one proof collects everything owed", async () => {
      const f = await withHolders();
      let entitlements = await distribute(f);

      // A second hour, a second purchase of the same coin.
      await time.increase(13 * HOUR);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      await runEpoch(f, [f.coinA]);
      entitlements = await distribute(f, entitlements);

      const coin = (await f.coinA.getAddress()) as Address;
      const tree = new CumulativeMerkleTree(entitlements);
      const bobE = entitlements.find(
        (e) => e.account.toLowerCase() === f.bob.address.toLowerCase()
      )!;

      // Bob never claimed the first hour; one claim now settles both.
      await f.distributor.claim(f.bob.address, coin, bobE.cumulativeAmount, tree.proof(bobE));
      const boughtTwice = ETH("2") * RATE;
      expect(await f.coinA.balanceOf(f.bob.address)).to.equal((boughtTwice * 30n) / 100n);
    });

    it("pays only the difference when a holder claims twice", async () => {
      const f = await withHolders();
      let entitlements = await distribute(f);
      const coin = (await f.coinA.getAddress()) as Address;
      let tree = new CumulativeMerkleTree(entitlements);
      let aliceE = entitlements.find(
        (e) => e.account.toLowerCase() === f.alice.address.toLowerCase()
      )!;

      await f.distributor.claim(f.alice.address, coin, aliceE.cumulativeAmount, tree.proof(aliceE));
      const afterFirst = await f.coinA.balanceOf(f.alice.address);

      await expect(
        f.distributor.claim(f.alice.address, coin, aliceE.cumulativeAmount, tree.proof(aliceE))
      ).to.be.revertedWithCustomError(f.distributor, "NothingToClaim");

      await time.increase(13 * HOUR);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      await runEpoch(f, [f.coinA]);
      entitlements = await distribute(f, entitlements);
      tree = new CumulativeMerkleTree(entitlements);
      aliceE = entitlements.find(
        (e) => e.account.toLowerCase() === f.alice.address.toLowerCase()
      )!;

      await f.distributor.claim(f.alice.address, coin, aliceE.cumulativeAmount, tree.proof(aliceE));
      // The second claim paid only the new hour, not the whole cumulative total again.
      expect(await f.coinA.balanceOf(f.alice.address)).to.equal(afterFirst * 2n);
    });

    it("rejects a forged proof", async () => {
      const f = await withHolders();
      const entitlements = await distribute(f);
      const coin = (await f.coinA.getAddress()) as Address;
      const tree = new CumulativeMerkleTree(entitlements);
      const aliceE = entitlements.find(
        (e) => e.account.toLowerCase() === f.alice.address.toLowerCase()
      )!;

      // Alice's proof, inflated amount.
      await expect(
        f.distributor.claim(
          f.alice.address,
          coin,
          aliceE.cumulativeAmount * 2n,
          tree.proof(aliceE)
        )
      ).to.be.revertedWithCustomError(f.distributor, "InvalidProof");

      // Carol claiming against Alice's leaf.
      await expect(
        f.distributor.claim(f.carol.address, coin, aliceE.cumulativeAmount, tree.proof(aliceE))
      ).to.be.revertedWithCustomError(f.distributor, "InvalidProof");
    });

    it("cannot pay out more of a coin than the treasury funded", async () => {
      const f = await withHolders();
      const coin = (await f.coinA.getAddress()) as Address;
      const funded = await f.distributor.totalFunded(coin);

      // A root that over-allocates: every holder credited the entire balance.
      const bad: Entitlement[] = [
        { account: f.alice.address as Address, token: coin, cumulativeAmount: funded },
        { account: f.bob.address as Address, token: coin, cumulativeAmount: funded },
      ];
      const tree = new CumulativeMerkleTree(bad);
      await f.distributor.connect(f.keeper).publishRoot(tree.root);

      await f.distributor.claim(f.alice.address, coin, funded, tree.proof(bad[0]));
      await expect(
        f.distributor.claim(f.bob.address, coin, funded, tree.proof(bad[1]))
      ).to.be.revertedWithCustomError(f.distributor, "ExceedsFunded");
    });

    it("claims several coins in one transaction", async () => {
      const f = await withHolders();
      await time.increase(HOUR + 1);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      await runEpoch(f, [f.coinB]);

      const coinA = (await f.coinA.getAddress()) as Address;
      const coinB = (await f.coinB.getAddress()) as Address;
      const alice = f.alice.address as Address;

      const entitlements: Entitlement[] = [
        { account: alice, token: coinA, cumulativeAmount: ETH("1") },
        { account: alice, token: coinB, cumulativeAmount: ETH("2") },
        { account: f.bob.address as Address, token: coinA, cumulativeAmount: ETH("3") },
      ];
      const tree = new CumulativeMerkleTree(entitlements);
      await f.distributor.connect(f.keeper).publishRoot(tree.root);

      await f.distributor.claimMany(
        alice,
        [coinA, coinB],
        [ETH("1"), ETH("2")],
        [tree.proof(entitlements[0]), tree.proof(entitlements[1])]
      );

      expect(await f.coinA.balanceOf(alice)).to.equal(ETH("1"));
      expect(await f.coinB.balanceOf(alice)).to.equal(ETH("2"));
    });

    it("refuses claims before any root is published", async () => {
      const f = await withHolders();
      await expect(
        f.distributor.claim(f.alice.address, await f.coinA.getAddress(), 1n, [])
      ).to.be.revertedWithCustomError(f.distributor, "NoRoot");
    });

    it("only the publisher or owner may publish a root", async () => {
      const f = await withHolders();
      await expect(
        f.distributor.connect(f.alice).publishRoot(ethers.ZeroHash)
      ).to.be.revertedWithCustomError(f.distributor, "NotPublisher");

      await expect(f.distributor.connect(f.keeper).publishRoot(ethers.id("x"))).to.emit(
        f.distributor,
        "RootPublished"
      );
      await expect(f.distributor.connect(f.owner).publishRoot(ethers.id("y"))).to.emit(
        f.distributor,
        "RootPublished"
      );
    });

    it("only the treasury can fund a distribution", async () => {
      const f = await withHolders();
      await expect(
        f.distributor.connect(f.alice).fund(await f.coinA.getAddress(), 1n)
      ).to.be.revertedWithCustomError(f.distributor, "NotTreasury");
    });
  });

  // =====================================================================
  describe("the vault's permanent backing", () => {
    async function withBacking() {
      const f = await loadFixture(deployFixture);
      await f.treasury.setVaultBps(10_000); // everything retained
      await fundTreasury(f, ETH("1"));
      await runEpoch(f, [f.coinA]);
      await f.ipo.transfer(f.alice.address, ETH("100000000"));
      return f;
    }

    it("pays a redeemer their pro-rata slice of the basket", async () => {
      const f = await withBacking();
      const vaultAddr = await f.vault.getAddress();
      const coin = await f.coinA.getAddress();
      const basket = await f.coinA.balanceOf(vaultAddr);

      const amount = ETH("100000000"); // 10% of supply
      const expected = (basket * amount) / IPO_SUPPLY;

      await f.ipo.connect(f.alice).approve(vaultAddr, amount);
      await f.vault.connect(f.alice).redeem(amount, [coin], f.alice.address);
      expect(await f.coinA.balanceOf(f.alice.address)).to.equal(expected);
    });

    it("leaves per-token backing unchanged for holders who do not redeem", async () => {
      const f = await withBacking();
      const vaultAddr = await f.vault.getAddress();
      const before =
        ((await f.coinA.balanceOf(vaultAddr)) * ETH("1")) / (await f.vault.redeemableSupply());

      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(vaultAddr, amount);
      await f.vault.connect(f.alice).redeem(amount, [await f.coinA.getAddress()], f.alice.address);

      const after =
        ((await f.coinA.balanceOf(vaultAddr)) * ETH("1")) / (await f.vault.redeemableSupply());
      expect(after).to.equal(before);
    });

    it("rejects duplicate assets, which would otherwise pay twice", async () => {
      const f = await withBacking();
      const coin = await f.coinA.getAddress();
      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(await f.vault.getAddress(), amount);
      await expect(
        f.vault.connect(f.alice).redeem(amount, [coin, coin], f.alice.address)
      ).to.be.revertedWithCustomError(f.vault, "AssetsNotSorted");
    });

    it("exposes no way for the owner to take assets out", async () => {
      const f = await withBacking();
      for (const c of [f.vault, f.distributor, f.treasury]) {
        const fns = c.interface.fragments
          .filter((x: any) => x.type === "function")
          .map((x: any) => x.name.toLowerCase());
        for (const name of ["withdraw", "sweep", "rescue", "emergency", "recover", "skim"]) {
          expect(fns.some((fn: string) => fn.includes(name)), `must not expose ${name}`).to.equal(false);
        }
      }
    });
  });
});
