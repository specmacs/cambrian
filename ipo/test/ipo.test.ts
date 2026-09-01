import { expect } from "chai";
import { ethers } from "hardhat";
import { loadFixture, time } from "@nomicfoundation/hardhat-network-helpers";

const ETH = (n: string) => ethers.parseEther(n);
const IPO_SUPPLY = ETH("1000000000"); // 1B, Pons fixed supply

/** Build a Pons `LaunchedToken` record as the factory would report it. */
async function record(token: string, over: Partial<Record<string, any>> = {}) {
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
    sweptQuote: ETH("4.2"),
    sweptTokens: 0n,
    sweptAt: BigInt(now),
    exists: true,
    ...over,
  };
}

describe("IPO — Initial Pons Offering", () => {
  async function deployFixture() {
    const [owner, keeper, alice, bob, curve] = await ethers.getSigners();

    const ERC20 = await ethers.getContractFactory("MockERC20");
    const ipo = await ERC20.deploy("Initial Pons Offering", "IPO", IPO_SUPPLY);

    const factory = await (await ethers.getContractFactory("MockPonsFactory")).deploy();
    const escrow = await (await ethers.getContractFactory("MockPonsFeeEscrow")).deploy();
    const pm = await (await ethers.getContractFactory("MockPoolManager")).deploy();
    const memeHook = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044";

    const vault = await (await ethers.getContractFactory("IPOVault")).deploy(
      await ipo.getAddress(),
      owner.address
    );
    const treasury = await (await ethers.getContractFactory("IPOTreasury")).deploy(
      await factory.getAddress(),
      await escrow.getAddress(),
      await pm.getAddress(),
      memeHook,
      await ipo.getAddress(),
      owner.address
    );

    await vault.setTreasury(await treasury.getAddress());
    await treasury.setVault(await vault.getAddress());
    await treasury.setKeeper(keeper.address, true);

    // A graduated Pons project the treasury can buy, with liquidity sitting in the pool manager.
    const proj = await ERC20.deploy("Bonded Project", "BOND", ETH("1000000"));
    await proj.transfer(await pm.getAddress(), ETH("1000000"));
    await factory.setRecord(await proj.getAddress(), await record(await proj.getAddress()));

    return { owner, keeper, alice, bob, curve, ipo, factory, escrow, pm, vault, treasury, proj };
  }

  /** Fund the treasury the way Pons does: tax accrues in the escrow, treasury claims it. */
  async function fundTreasury(f: any, amount: bigint) {
    await f.escrow.credit(await f.treasury.getAddress(), { value: amount });
    await f.treasury.claimTax();
  }

  describe("tax collection", () => {
    it("claims the creator tax out of the Pons fee escrow as ETH", async () => {
      const f = await loadFixture(deployFixture);
      const treasuryAddr = await f.treasury.getAddress();

      await f.escrow.credit(treasuryAddr, { value: ETH("3") });
      expect(await f.escrow.balanceOf(treasuryAddr)).to.equal(ETH("3"));

      await expect(f.treasury.claimTax())
        .to.emit(f.treasury, "TaxClaimed")
        .withArgs(ETH("3"));

      expect(await ethers.provider.getBalance(treasuryAddr)).to.equal(ETH("3"));
      expect(await f.treasury.totalTaxClaimed()).to.equal(ETH("3"));
    });

    it("reverts when there is nothing accrued", async () => {
      const f = await loadFixture(deployFixture);
      await expect(f.treasury.claimTax()).to.be.revertedWithCustomError(f.treasury, "NothingToClaim");
    });
  });

  describe("buying newly bonded projects", () => {
    it("buys a graduated project and deposits it into the vault", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      const deadline = (await time.latest()) + 600;

      const expectedOut = ETH("0.05") * 1000n; // buySize * mock rate

      await expect(f.treasury.connect(f.keeper).buy(proj, expectedOut, deadline))
        .to.emit(f.treasury, "Purchased")
        .withArgs(proj, ETH("0.05"), expectedOut);

      expect(await f.proj.balanceOf(await f.vault.getAddress())).to.equal(expectedOut);
      expect(await f.vault.isAsset(proj)).to.equal(true);
      expect(await f.vault.assetCount()).to.equal(1n);
      expect(await f.treasury.totalPurchases()).to.equal(1n);
    });

    it("only keepers can buy", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      await expect(
        f.treasury.connect(f.alice).buy(await f.proj.getAddress(), 0, deadline)
      ).to.be.revertedWithCustomError(f.treasury, "NotKeeper");
    });

    it("refuses a project that has not bonded yet", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      await f.factory.setPhase(proj, 0); // NotGraduated
      const deadline = (await time.latest()) + 600;

      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "NotGraduated")
        .withArgs(proj, 0);
    });

    it("refuses a launch that used the rescue path", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      await f.factory.setPhase(proj, 3); // Rescued
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "NotGraduated")
        .withArgs(proj, 3);
    });

    it("refuses a token that is not a Pons launch at all", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(f.alice.address, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "NotAPonsLaunch");
    });

    it("refuses a project paired against something other than ETH", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      await f.factory.setRecord(proj, await record(proj, { pairToken: f.alice.address }));
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "NotEthPaired");
    });

    it("refuses a project that bonded too long ago", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      await time.increase(25 * 60 * 60); // past the 24h window
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "TooOld");
    });

    it("refuses a launch whose graduation threshold was below the floor", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      await f.factory.setRecord(proj, await record(proj, { graduationThreshold: ETH("0.1") }));
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "ThresholdTooLow");
    });

    it("never buys the same project twice", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const proj = await f.proj.getAddress();
      let deadline = (await time.latest()) + 600;
      await f.treasury.connect(f.keeper).buy(proj, 0, deadline);

      await time.increase(600); // clear the cooldown
      deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(proj, 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "AlreadyPurchased");
    });

    it("enforces the cooldown between purchases", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const ERC20 = await ethers.getContractFactory("MockERC20");
      const second = await ERC20.deploy("Second", "TWO", ETH("1000000"));
      await second.transfer(await f.pm.getAddress(), ETH("1000000"));
      await f.factory.setRecord(await second.getAddress(), await record(await second.getAddress()));

      let deadline = (await time.latest()) + 600;
      await f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), 0, deadline);

      deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(await second.getAddress(), 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "CooldownActive");

      await time.increase(301);
      deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(await second.getAddress(), 0, deadline)).to.emit(
        f.treasury,
        "Purchased"
      );
    });

    it("respects the reserve floor", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("0.06"));
      await f.treasury.setPolicy(ETH("0.05"), ETH("0.05"), 0, 24 * 3600, ETH("4.2"));
      const deadline = (await time.latest()) + 600;
      await expect(f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), 0, deadline))
        .to.be.revertedWithCustomError(f.treasury, "InsufficientFunds");
    });

    it("enforces the slippage bound", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      const tooMuch = ETH("0.05") * 2000n;
      await expect(f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), tooMuch, deadline))
        .to.be.revertedWithCustomError(f.treasury, "SlippageExceeded");
    });

    it("honours the deadline", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const past = (await time.latest()) - 1;
      await expect(f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), 0, past))
        .to.be.revertedWithCustomError(f.treasury, "DeadlinePassed");
    });

    it("stops buying when paused", async () => {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      await f.treasury.pause();
      const deadline = (await time.latest()) + 600;
      await expect(
        f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), 0, deadline)
      ).to.be.revertedWithCustomError(f.treasury, "EnforcedPause");
    });

    it("reports eligibility off-chain the same way it enforces it on-chain", async () => {
      const f = await loadFixture(deployFixture);
      const proj = await f.proj.getAddress();

      let [ok, reason] = await f.treasury.eligibility(proj);
      expect(ok).to.equal(false);
      expect(reason).to.equal("insufficient funds");

      await fundTreasury(f, ETH("1"));
      [ok, reason] = await f.treasury.eligibility(proj);
      expect(ok).to.equal(true);
      expect(reason).to.equal("");

      await f.factory.setPhase(proj, 0);
      [ok, reason] = await f.treasury.eligibility(proj);
      expect(ok).to.equal(false);
      expect(reason).to.equal("not graduated");
    });

    it("rejects an unlockCallback from anyone but the pool manager", async () => {
      const f = await loadFixture(deployFixture);
      await expect(
        f.treasury.connect(f.alice).unlockCallback("0x")
      ).to.be.revertedWithCustomError(f.treasury, "OnlyPoolManager");
    });
  });

  describe("redemption", () => {
    /** Buy one project and hand IPO to alice and bob so they hold a claim on it. */
    async function withBasket() {
      const f = await loadFixture(deployFixture);
      await fundTreasury(f, ETH("1"));
      const deadline = (await time.latest()) + 600;
      await f.treasury.connect(f.keeper).buy(await f.proj.getAddress(), 0, deadline);

      // Owner keeps 800M; alice and bob hold 100M each.
      await f.ipo.transfer(f.alice.address, ETH("100000000"));
      await f.ipo.transfer(f.bob.address, ETH("100000000"));
      return f;
    }

    it("excludes the vault's own locked IPO from the redeemable supply", async () => {
      const f = await withBasket();
      expect(await f.vault.redeemableSupply()).to.equal(IPO_SUPPLY);

      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(await f.vault.getAddress(), amount);
      await f.vault.connect(f.alice).redeem(amount, [await f.proj.getAddress()], f.alice.address);

      // Alice's IPO is now locked in the vault and no longer backs a claim.
      expect(await f.vault.redeemableSupply()).to.equal(IPO_SUPPLY - amount);
    });

    it("pays a redeemer their pro-rata slice of the basket", async () => {
      const f = await withBasket();
      const proj = await f.proj.getAddress();
      const basket = await f.proj.balanceOf(await f.vault.getAddress());

      const amount = ETH("100000000"); // 10% of supply
      const expected = (basket * amount) / IPO_SUPPLY;

      const preview = await f.vault.previewRedeem(amount, [proj]);
      expect(preview[0]).to.equal(expected);

      await f.ipo.connect(f.alice).approve(await f.vault.getAddress(), amount);
      await expect(f.vault.connect(f.alice).redeem(amount, [proj], f.alice.address))
        .to.emit(f.vault, "Redeemed")
        .withArgs(f.alice.address, f.alice.address, amount, 1);

      expect(await f.proj.balanceOf(f.alice.address)).to.equal(expected);
      expect(await f.proj.balanceOf(await f.vault.getAddress())).to.equal(basket - expected);
    });

    it("leaves per-token backing unchanged for holders who do not redeem", async () => {
      const f = await withBasket();
      const proj = await f.proj.getAddress();
      const vaultAddr = await f.vault.getAddress();

      const backingBefore =
        (await f.proj.balanceOf(vaultAddr)) * ETH("1") / (await f.vault.redeemableSupply());

      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(vaultAddr, amount);
      await f.vault.connect(f.alice).redeem(amount, [proj], f.alice.address);

      const backingAfter =
        (await f.proj.balanceOf(vaultAddr)) * ETH("1") / (await f.vault.redeemableSupply());

      // Redeeming is neutral for everyone else: it removes exactly its own claim.
      expect(backingAfter).to.equal(backingBefore);
    });

    it("raises backing for remaining holders when a redeemer skips an asset", async () => {
      const f = await withBasket();
      const vaultAddr = await f.vault.getAddress();

      // Add a second asset to the basket via a second purchase.
      const ERC20 = await ethers.getContractFactory("MockERC20");
      const second = await ERC20.deploy("Second", "TWO", ETH("1000000"));
      await second.transfer(await f.pm.getAddress(), ETH("1000000"));
      await f.factory.setRecord(await second.getAddress(), await record(await second.getAddress()));
      await time.increase(301);
      await f.treasury
        .connect(f.keeper)
        .buy(await second.getAddress(), 0, (await time.latest()) + 600);

      const secondBal = await second.balanceOf(vaultAddr);
      const backingBefore = (secondBal * ETH("1")) / (await f.vault.redeemableSupply());

      // Alice redeems taking only the first asset, forfeiting her claim on the second.
      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(vaultAddr, amount);
      await f.vault.connect(f.alice).redeem(amount, [await f.proj.getAddress()], f.alice.address);

      expect(await second.balanceOf(f.alice.address)).to.equal(0n);
      const backingAfter =
        ((await second.balanceOf(vaultAddr)) * ETH("1")) / (await f.vault.redeemableSupply());
      expect(backingAfter).to.be.greaterThan(backingBefore);
    });

    it("redeems several assets at once", async () => {
      const f = await withBasket();
      const vaultAddr = await f.vault.getAddress();

      const ERC20 = await ethers.getContractFactory("MockERC20");
      const second = await ERC20.deploy("Second", "TWO", ETH("1000000"));
      await second.transfer(await f.pm.getAddress(), ETH("1000000"));
      await f.factory.setRecord(await second.getAddress(), await record(await second.getAddress()));
      await time.increase(301);
      await f.treasury
        .connect(f.keeper)
        .buy(await second.getAddress(), 0, (await time.latest()) + 600);

      const sorted = [await f.proj.getAddress(), await second.getAddress()].sort((a, b) =>
        a.toLowerCase() < b.toLowerCase() ? -1 : 1
      );

      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(vaultAddr, amount);
      await f.vault.connect(f.alice).redeem(amount, sorted, f.alice.address);

      expect(await f.proj.balanceOf(f.alice.address)).to.be.greaterThan(0n);
      expect(await second.balanceOf(f.alice.address)).to.be.greaterThan(0n);
    });

    it("rejects duplicate or unsorted assets, which would otherwise pay twice", async () => {
      const f = await withBasket();
      const proj = await f.proj.getAddress();
      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(await f.vault.getAddress(), amount);

      await expect(
        f.vault.connect(f.alice).redeem(amount, [proj, proj], f.alice.address)
      ).to.be.revertedWithCustomError(f.vault, "AssetsNotSorted");
    });

    it("rejects an asset that is not in the basket", async () => {
      const f = await withBasket();
      const amount = ETH("100000000");
      await f.ipo.connect(f.alice).approve(await f.vault.getAddress(), amount);
      await expect(
        f.vault.connect(f.alice).redeem(amount, [await f.ipo.getAddress()], f.alice.address)
      ).to.be.revertedWithCustomError(f.vault, "NotRegistered");
    });

    it("lets the owner exclude the bonding curve from the redeemable supply", async () => {
      const f = await withBasket();
      await f.ipo.transfer(f.curve.address, ETH("500000000"));

      expect(await f.vault.redeemableSupply()).to.equal(IPO_SUPPLY);
      await f.vault.setExcluded(f.curve.address, true);
      expect(await f.vault.redeemableSupply()).to.equal(IPO_SUPPLY - ETH("500000000"));

      await f.vault.setExcluded(f.curve.address, false);
      expect(await f.vault.redeemableSupply()).to.equal(IPO_SUPPLY);
    });

    it("only the treasury can deposit into the basket", async () => {
      const f = await withBasket();
      await expect(
        f.vault.connect(f.alice).deposit(await f.proj.getAddress(), 1)
      ).to.be.revertedWithCustomError(f.vault, "NotTreasury");
    });

    it("exposes no way for the owner to take assets out of the vault", async () => {
      const f = await withBasket();
      const fns = f.vault.interface.fragments
        .filter((x: any) => x.type === "function")
        .map((x: any) => x.name.toLowerCase());
      for (const name of ["withdraw", "sweep", "rescue", "emergency", "recover", "skim"]) {
        expect(fns.some((fn: string) => fn.includes(name)), `vault must not expose ${name}`).to.equal(
          false
        );
      }
    });
  });
});
