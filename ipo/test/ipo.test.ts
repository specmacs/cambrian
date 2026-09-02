import { expect } from "chai";
import { ethers } from "hardhat";
import { loadFixture, time } from "@nomicfoundation/hardhat-network-helpers";
import { planAirdrop, toBatches } from "../bot/airdrop";
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
    const airdropper = await (
      await ethers.getContractFactory("IPOAirdropper")
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
    await airdropper.setTreasury(treasuryAddr);
    await airdropper.setKeeper(keeper.address, true);
    await treasury.setAirdropper(await airdropper.getAddress());
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
      ipo, factory, escrow, pm, vault, airdropper, treasury,
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
      const distAddr = await f.airdropper.getAddress();
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
      expect(await f.coinA.balanceOf(await f.airdropper.getAddress())).to.equal(bought);
      expect(await f.coinA.balanceOf(await f.vault.getAddress())).to.equal(0n);
      expect(await f.airdropper.totalFunded(await f.coinA.getAddress())).to.equal(bought);
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
      expect(await f.coinA.balanceOf(await f.airdropper.getAddress())).to.equal((bought * 75n) / 100n);
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
  describe("airdropping to holders", () => {
    async function withHolders() {
      const f = await loadFixture(deployFixture);
      await f.ipo.transfer(f.alice.address, ETH("60000000"));
      await f.ipo.transfer(f.bob.address, ETH("30000000"));
      await f.ipo.transfer(f.carol.address, ETH("10000000"));
      await fundTreasury(f, ETH("1"));
      await runEpoch(f, [f.coinA]);
      return f;
    }

    /** The split the keeper computes: everything undistributed, over every IPO holder. */
    async function plan(f: any, coin: any, minPayout = 0n) {
      const token = (await coin.getAddress()) as Address;
      const available = new Map<Address, bigint>([[token, await f.airdropper.undistributed(token)]]);
      const holders: [Address, bigint][] = [];
      for (const who of [f.owner, f.alice, f.bob, f.carol]) {
        holders.push([who.address as Address, await f.ipo.balanceOf(who.address)]);
      }
      const balances = new Map(holders.filter(([, b]) => b > 0n));
      const [p] = planAirdrop(available, balances, minPayout);
      return { p, token, balances };
    }

    async function send(f: any, p: any, epochId = 1n, batchIndex = 0n) {
      return f.airdropper
        .connect(f.keeper)
        .airdrop(epochId, batchIndex, p.token, p.recipients, p.amounts);
    }

    it("sends each holder their pro-rata share straight to their wallet", async () => {
      const f = await withHolders();
      const { p, token, balances } = await plan(f, f.coinA);
      const funded = await f.airdropper.totalFunded(token);
      const totalIpo = [...balances.values()].reduce((a, b) => a + b, 0n);

      expect(await f.coinA.balanceOf(f.alice.address)).to.equal(0n);
      await send(f, p);

      // Holders did nothing: no claim, no approval, no gas.
      for (const who of [f.alice, f.bob, f.carol]) {
        const expected = (funded * (await f.ipo.balanceOf(who.address))) / totalIpo;
        expect(await f.coinA.balanceOf(who.address)).to.equal(expected);
      }
      expect(await f.airdropper.totalSent(token)).to.equal(p.total);
      expect(await f.airdropper.totalTransfers()).to.equal(BigInt(p.recipients.length));
    });

    it("rolls rounding dust into the next hour instead of stranding it", async () => {
      const f = await withHolders();
      const { p, token } = await plan(f, f.coinA);
      await send(f, p);

      const left = await f.airdropper.undistributed(token);
      expect(left).to.equal(p.remainder);
      expect(left).to.be.lessThan(4n); // at most one wei per holder

      // Next hour's purchase adds to the leftover rather than replacing it.
      await time.increase(13 * HOUR);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      await runEpoch(f, [f.coinA]);
      expect(await f.airdropper.undistributed(token)).to.equal(left + ETH("1") * RATE);
    });

    it("leaves out holders whose share rounds below the dust floor", async () => {
      const f = await withHolders();
      // Carol holds 10M of 1B, so a 1 ETH buy pays her ~1% of the coins.
      const funded = await f.airdropper.totalFunded(await f.coinA.getAddress());
      const carolShare = (funded * ETH("10000000")) / IPO_SUPPLY;

      const { p } = await plan(f, f.coinA, carolShare + 1n);
      expect(p.recipients.map((r: string) => r.toLowerCase())).to.not.include(
        f.carol.address.toLowerCase()
      );
      expect(p.dusted).to.be.greaterThan(0);

      await send(f, p);
      expect(await f.coinA.balanceOf(f.carol.address)).to.equal(0n);
      // Her share is not lost, it is still sitting undistributed.
      expect(await f.airdropper.undistributed(await f.coinA.getAddress())).to.be.gte(carolShare);
    });

    it("refuses to send the same batch twice", async () => {
      const f = await withHolders();
      const { p } = await plan(f, f.coinA);
      await send(f, p);
      await expect(send(f, p)).to.be.revertedWithCustomError(f.airdropper, "BatchAlreadySent");
    });

    it("treats the same batch index under a different epoch as new work", async () => {
      const f = await withHolders();
      const { p } = await plan(f, f.coinA);
      await send(f, p, 1n, 0n);

      await time.increase(13 * HOUR);
      await f.escrow.credit(await f.treasury.getAddress(), { value: ETH("1") });
      await runEpoch(f, [f.coinA]);

      const next = await plan(f, f.coinA);
      await expect(send(f, next.p, 2n, 0n)).to.emit(f.airdropper, "AirdropSent");
    });

    it("cannot send more of a coin than the treasury funded", async () => {
      const f = await withHolders();
      const token = await f.coinA.getAddress();
      const funded = await f.airdropper.totalFunded(token);

      await expect(
        f.airdropper
          .connect(f.keeper)
          .airdrop(1, 0, token, [f.alice.address, f.bob.address], [funded, funded])
      ).to.be.revertedWithCustomError(f.airdropper, "ExceedsFunded");
    });

    it("splits into batches and delivers them all", async () => {
      const f = await withHolders();
      const { p } = await plan(f, f.coinA);
      const batches = toBatches(p, 2);
      expect(batches.length).to.equal(2); // 4 holders, 2 per batch

      for (const b of batches) {
        await f.airdropper
          .connect(f.keeper)
          .airdrop(1, b.batchIndex, b.token, b.recipients, b.amounts);
      }
      expect(await f.airdropper.totalSent(await f.coinA.getAddress())).to.equal(p.total);
      expect(await f.coinA.balanceOf(f.alice.address)).to.be.greaterThan(0n);
      expect(await f.coinA.balanceOf(f.carol.address)).to.be.greaterThan(0n);
    });

    it("skips zero amounts rather than reverting on them", async () => {
      const f = await withHolders();
      const token = await f.coinA.getAddress();
      await f.airdropper
        .connect(f.keeper)
        .airdrop(1, 0, token, [f.alice.address, f.bob.address], [0n, 100n]);
      expect(await f.coinA.balanceOf(f.alice.address)).to.equal(0n);
      expect(await f.coinA.balanceOf(f.bob.address)).to.equal(100n);
      expect(await f.airdropper.totalTransfers()).to.equal(1n);
    });

    it("rejects malformed batches", async () => {
      const f = await withHolders();
      const token = await f.coinA.getAddress();
      await expect(
        f.airdropper.connect(f.keeper).airdrop(1, 0, token, [f.alice.address], [1n, 2n])
      ).to.be.revertedWithCustomError(f.airdropper, "LengthMismatch");
      await expect(
        f.airdropper.connect(f.keeper).airdrop(1, 0, token, [], [])
      ).to.be.revertedWithCustomError(f.airdropper, "EmptyBatch");
    });

    it("only keepers can airdrop, and only the treasury can fund", async () => {
      const f = await withHolders();
      const { p } = await plan(f, f.coinA);
      await expect(
        f.airdropper.connect(f.alice).airdrop(1, 0, p.token, p.recipients, p.amounts)
      ).to.be.revertedWithCustomError(f.airdropper, "NotKeeper");
      await expect(
        f.airdropper.connect(f.alice).fund(p.token, 1n)
      ).to.be.revertedWithCustomError(f.airdropper, "NotTreasury");
    });

    it("costs a predictable amount of gas per recipient", async () => {
      const f = await withHolders();
      const token = await f.coinA.getAddress();
      const funded = await f.airdropper.totalFunded(token);

      // 100 fresh addresses, which is the expensive case: every transfer writes a new balance slot.
      const n = 100;
      const recipients = Array.from(
        { length: n },
        (_, i) => ethers.getAddress("0x" + (i + 1).toString(16).padStart(40, "0"))
      );
      const each = funded / BigInt(n * 2);
      const amounts = Array(n).fill(each);

      const tx = await f.airdropper.connect(f.keeper).airdrop(1, 0, token, recipients, amounts);
      const receipt = await tx.wait();
      const perRecipient = Number(receipt!.gasUsed) / n;
      console.log(
        `      ${n} fresh recipients: ${receipt!.gasUsed} gas total, ` +
          `~${Math.round(perRecipient)} per recipient`
      );
      // Guards against a refactor that quietly makes the hot loop much more expensive.
      expect(perRecipient).to.be.lessThan(40_000);
    });

    it("stops airdropping when paused", async () => {
      const f = await withHolders();
      const { p } = await plan(f, f.coinA);
      await f.airdropper.pause();
      await expect(send(f, p)).to.be.revertedWithCustomError(f.airdropper, "EnforcedPause");
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
      // The airdropper is excluded on purpose: sending coins out is its entire job. Its bound is
      // the totalFunded cap, tested separately, not the absence of a transfer path.
      for (const c of [f.vault, f.treasury]) {
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
