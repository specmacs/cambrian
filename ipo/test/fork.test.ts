/**
 * Fork tests: the whole pipeline against real Robinhood Chain state.
 *
 * The unit suite proves the logic against a mock pool manager. It cannot prove the parts most
 * likely to be wrong — that the pool key reconstructed from a Pons launch record actually
 * identifies the pool, that the swap direction is right, and that settle/take balances against
 * v4's real flash accounting with the Pons hook in the path taking its cut.
 *
 * Opt-in, because it needs real network:
 *   FORK=1 npx hardhat test test/fork.test.ts
 *
 * No block is pinned. The public RPC prunes state, so a hard-coded block stops being serveable
 * within days; each test forks the chain head instead. The coins below graduated long ago and stay
 * graduated, so they remain valid targets at any later head.
 */
import { expect } from "chai";
import { ethers, network } from "hardhat";
import { PONS, UNISWAP_V4 } from "../config/addresses";
import { planAirdrop, toBatches } from "../bot/airdrop";
import type { Address } from "viem";

/** Launches confirmed graduated (phase 2) and ETH-paired. Graduation is permanent. */
const REAL_COIN = "0x4090486c416bb9e6fdb32d203ea305c4e7133190";
/** Three more, already in the strictly-ascending order runEpoch requires. */
const REAL_COINS = [
  "0x22433543c50b0f932d3ddf8661624ba5b0014ee0",
  "0x33453a6be2c31497af3928353d5fd3e2aabc3b3d",
  "0x5896e1f9a93a6a2482a5c20463b73536687b9828",
];

const IPO_SUPPLY = ethers.parseEther("1000000000");

const forking = !!process.env.FORK;
(forking ? describe : describe.skip)("fork: the real pipeline", () => {
  // Each test mines blocks past the fork point, and the RPC is not an archive node, so a later
  // test asking for state at those blocks gets "metadata is not found". Re-fork the current head
  // before each test so every one starts from fresh, serveable state.
  beforeEach(async () => {
    await network.provider.request({
      method: "hardhat_reset",
      params: [
        {
          forking: {
            jsonRpcUrl: process.env.RPC_URL ?? "https://rpc.mainnet.chain.robinhood.com",
          },
        },
      ],
    });
  });

  async function deployAgainstMainnet() {
    const [owner, keeper, alice, bob, carol] = await ethers.getSigners();

    const factoryCode = await ethers.provider.getCode(PONS.factory);
    expect(factoryCode, "fork is not serving Pons state").to.not.equal("0x");

    const ipo = await (
      await ethers.getContractFactory("MockERC20")
    ).deploy("Initial Pons Offering", "IPO", IPO_SUPPLY);

    const vault = await (
      await ethers.getContractFactory("IPOVault")
    ).deploy(await ipo.getAddress(), owner.address);
    const airdropper = await (await ethers.getContractFactory("IPOAirdropper")).deploy(owner.address);
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

    await network.provider.send("hardhat_setBalance", [
      treasuryAddr,
      "0x" + ethers.parseEther("1").toString(16),
    ]);

    return { owner, keeper, alice, bob, carol, ipo, vault, airdropper, treasury, treasuryAddr };
  }

  it("swaps ETH for a graduated coin through the real v4 pool and hook", async function () {
    this.timeout(300_000);
    const f = await deployAgainstMainnet();

    // Keep the trade small; these pools graduate with only ~4.2 ETH of liquidity.
    await f.treasury.setPolicy(
      3600,
      10_000,
      ethers.parseEther("0.02"), // maxEpochSpend
      10,
      0,
      12 * 3600,
      ethers.parseEther("4.2")
    );

    const [eligible, reason] = await f.treasury.eligibility(REAL_COIN);
    expect(eligible, `real coin not eligible: ${reason}`).to.equal(true);

    const coin = await ethers.getContractAt("MockERC20", REAL_COIN);
    const dropAddr = await f.airdropper.getAddress();
    expect(await coin.balanceOf(dropAddr)).to.equal(0n);

    const deadline = (await ethers.provider.getBlock("latest"))!.timestamp + 600;
    await (
      await f.treasury.connect(f.keeper).runEpoch([{ token: REAL_COIN, minTokensOut: 0n }], deadline)
    ).wait();

    const received = await coin.balanceOf(dropAddr);
    console.log(`      received ${ethers.formatUnits(received, 18)} tokens for 0.02 ETH`);
    expect(received, "no tokens received from the real pool").to.be.greaterThan(0n);
    expect(await f.airdropper.totalFunded(REAL_COIN)).to.equal(received);

    // The treasury kept nothing back and spent exactly the budget.
    expect(await coin.balanceOf(f.treasuryAddr)).to.equal(0n);
    expect(await f.treasury.totalEthSpent()).to.equal(ethers.parseEther("0.02"));
    expect(await ethers.provider.getBalance(f.treasuryAddr)).to.equal(ethers.parseEther("0.98"));
  });

  it("buys a whole basket under one unlock and airdrops it to real wallets", async function () {
    this.timeout(600_000);
    const f = await deployAgainstMainnet();

    await f.treasury.setPolicy(
      3600,
      10_000,
      ethers.parseEther("0.03"), // 0.01 per coin across three
      10,
      0,
      12 * 3600,
      ethers.parseEther("4.2")
    );

    for (const c of REAL_COINS) {
      const [ok, why] = await f.treasury.eligibility(c);
      expect(ok, `${c} not eligible: ${why}`).to.equal(true);
    }

    // Three real pools, three swaps, one unlock, one net ETH settlement.
    const deadline = (await ethers.provider.getBlock("latest"))!.timestamp + 600;
    const receipt = await (
      await f.treasury
        .connect(f.keeper)
        .runEpoch(
          REAL_COINS.map((token) => ({ token, minTokensOut: 0n })),
          deadline
        )
    ).wait();
    console.log(`      basket of 3 bought in one tx, gas ${receipt!.gasUsed}`);

    const dropAddr = await f.airdropper.getAddress();
    for (const c of REAL_COINS) {
      const coin = await ethers.getContractAt("MockERC20", c);
      const bal = await coin.balanceOf(dropAddr);
      expect(bal, `${c} paid out nothing`).to.be.greaterThan(0n);
      expect(await f.airdropper.totalFunded(c)).to.equal(bal);
      console.log(`      ${c} -> ${ethers.formatUnits(bal, 18)}`);
    }
    expect(await f.treasury.totalEthSpent()).to.equal(ethers.parseEther("0.03"));
    expect(await ethers.provider.getBalance(f.treasuryAddr)).to.equal(ethers.parseEther("0.97"));

    // --- now actually airdrop it, exactly as the keeper would --------------
    await f.ipo.transfer(f.alice.address, ethers.parseEther("60000000"));
    await f.ipo.transfer(f.bob.address, ethers.parseEther("30000000"));
    await f.ipo.transfer(f.carol.address, ethers.parseEther("10000000"));

    const holders: [Address, bigint][] = [];
    for (const who of [f.owner, f.alice, f.bob, f.carol]) {
      holders.push([who.address as Address, await f.ipo.balanceOf(who.address)]);
    }
    const balances = new Map(holders);
    const totalIpo = [...balances.values()].reduce((a, b) => a + b, 0n);

    const available = new Map<Address, bigint>();
    for (const c of REAL_COINS) {
      available.set(c as Address, await f.airdropper.undistributed(c as Address));
    }

    const plans = planAirdrop(available, balances);
    expect(plans.length).to.equal(3);

    for (const plan of plans) {
      for (const batch of toBatches(plan, 2)) {
        await f.airdropper
          .connect(f.keeper)
          .airdrop(1, batch.batchIndex, batch.token, batch.recipients, batch.amounts);
      }
    }

    // Real coins, bought on real pools, now sitting in holders' wallets.
    for (const c of REAL_COINS) {
      const coin = await ethers.getContractAt("MockERC20", c);
      const funded = await f.airdropper.totalFunded(c);
      for (const who of [f.alice, f.bob, f.carol]) {
        const expected = (funded * (await f.ipo.balanceOf(who.address))) / totalIpo;
        expect(await coin.balanceOf(who.address)).to.equal(expected);
      }
      // Only rounding dust is left behind.
      expect(await f.airdropper.undistributed(c)).to.be.lessThan(4n);
    }
    console.log(`      airdropped 3 coins to ${balances.size} holders, dust only remaining`);
  });
});
