/**
 * Fork test: buys a real graduated Pons coin, on a real Uniswap v4 pool, through the real Pons hook.
 *
 * The unit suite proves the treasury's logic against a mock pool manager. It cannot prove the part
 * most likely to be wrong — that the pool key reconstructed from a Pons launch record actually
 * identifies the pool, that the swap direction is right, and that the settle/take accounting
 * balances against v4's real flash accounting with a hook in the path.
 *
 * Opt-in, because it needs an archive-ish RPC and real network:
 *   FORK_BLOCK=52202800 npx hardhat test test/fork.test.ts
 */
import { expect } from "chai";
import { ethers, network } from "hardhat";
import { PONS, UNISWAP_V4 } from "../config/addresses";

/** A launch confirmed graduated (phase 2), ETH-paired, tickSpacing 200, creatorTax 400. */
const REAL_COIN = "0x4090486c416bb9e6fdb32d203ea305c4e7133190";

const forking = !!process.env.FORK_BLOCK;
(forking ? describe : describe.skip)("fork: buying a real Pons coin", () => {
  it("swaps ETH for a graduated coin through the real v4 pool and hook", async function () {
    this.timeout(300_000);
    const [owner, keeper] = await ethers.getSigners();

    // Sanity-check we are actually on the fork before asserting anything about it.
    expect(await ethers.provider.getNetwork().then((n) => Number(n.chainId))).to.be.oneOf([4663, 31337]);
    const factoryCode = await ethers.provider.getCode(PONS.factory);
    expect(factoryCode, "fork is not serving Pons state").to.not.equal("0x");

    const ipo = await (
      await ethers.getContractFactory("MockERC20")
    ).deploy("Initial Pons Offering", "IPO", ethers.parseEther("1000000000"));

    const vault = await (
      await ethers.getContractFactory("IPOVault")
    ).deploy(await ipo.getAddress(), owner.address);
    const airdropper = await (
      await ethers.getContractFactory("IPOAirdropper")
    ).deploy(owner.address);
    const treasury = await (
      await ethers.getContractFactory("IPOTreasury")
    ).deploy(
      PONS.factory,
      PONS.feeEscrow,
      UNISWAP_V4.poolManager,
      PONS.memeHook,
      await ipo.getAddress(),
      owner.address
    );

    const treasuryAddr = await treasury.getAddress();
    await vault.setTreasury(treasuryAddr);
    await airdropper.setTreasury(treasuryAddr);
    await airdropper.setKeeper(keeper.address, true);
    await treasury.setAirdropper(await airdropper.getAddress());
    await treasury.setVault(await vault.getAddress());
    await treasury.setKeeper(keeper.address, true);

    // Keep the trade small; these pools graduate with only ~4.2 ETH of liquidity.
    await treasury.setPolicy(
      3600,
      10_000,
      ethers.parseEther("0.02"), // maxEpochSpend
      10,
      0,
      12 * 3600,
      ethers.parseEther("4.2")
    );

    await network.provider.send("hardhat_setBalance", [
      treasuryAddr,
      "0x" + ethers.parseEther("1").toString(16),
    ]);

    const [eligible, reason] = await treasury.eligibility(REAL_COIN);
    expect(eligible, `real coin not eligible: ${reason}`).to.equal(true);

    const coin = await ethers.getContractAt("MockERC20", REAL_COIN);
    const dropAddr = await airdropper.getAddress();
    expect(await coin.balanceOf(dropAddr)).to.equal(0n);

    const deadline = (await ethers.provider.getBlock("latest"))!.timestamp + 600;
    const tx = await treasury
      .connect(keeper)
      .runEpoch([{ token: REAL_COIN, minTokensOut: 0n }], deadline);
    await tx.wait();

    // The real pool paid out, the accounting balanced, and the coins reached the airdropper.
    const received = await coin.balanceOf(dropAddr);
    console.log(`      received ${ethers.formatUnits(received, 18)} tokens for 0.02 ETH`);
    expect(received, "no tokens received from the real pool").to.be.greaterThan(0n);
    expect(await airdropper.totalFunded(REAL_COIN)).to.equal(received);

    // The treasury kept nothing back and spent exactly the budget.
    expect(await coin.balanceOf(treasuryAddr)).to.equal(0n);
    expect(await treasury.totalEthSpent()).to.equal(ethers.parseEther("0.02"));
    expect(await ethers.provider.getBalance(treasuryAddr)).to.equal(ethers.parseEther("0.98"));
  });
});
