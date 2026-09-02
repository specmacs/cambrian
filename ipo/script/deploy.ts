/**
 * Launches IPO on Pons and deploys the treasury + vault behind it.
 *
 * The treasury must be the creator-fee recipient from the very first trade, so its address is
 * predicted from the deployer's nonce and passed into the launch. Deployment then asserts the
 * treasury actually landed there. Nothing is broadcast without CONFIRM_LAUNCH=yes.
 *
 *   CONFIRM_LAUNCH=yes KEEPER_ADDRESS=0x... OWNER_ADDRESS=0x... \
 *     npx hardhat run script/deploy.ts --network robinhood
 *
 * KEEPER_ADDRESS is the hot key the bot signs with; OWNER_ADDRESS is the cold key that ends up
 * owning the contracts. They must differ — see the check below for why.
 */
import { ethers } from "hardhat";
import { PONS, IPO_LAUNCH, UNISWAP_V4, LAUNCH_CONFIG_ID } from "../config/addresses";

const CREATOR_TAX_BPS = IPO_LAUNCH.creatorTaxBps; // 400 = 4%

async function main() {
  const [deployer] = await ethers.getSigners();
  const poolManager = process.env.POOL_MANAGER ?? UNISWAP_V4.poolManager;
  const keeper = process.env.KEEPER_ADDRESS ?? deployer.address;
  const finalOwner = process.env.OWNER_ADDRESS;
  const launchConfigId = BigInt(process.env.LAUNCH_CONFIG_ID ?? LAUNCH_CONFIG_ID);

  if (!poolManager || poolManager === ethers.ZeroAddress) {
    throw new Error("POOL_MANAGER is required (Uniswap v4 PoolManager on Robinhood Chain)");
  }
  console.log("run `npm run verify` first if you have not — it re-checks every Pons assumption");
  if (process.env.CONFIRM_LAUNCH !== "yes") {
    throw new Error("refusing to launch: set CONFIRM_LAUNCH=yes to broadcast");
  }

  // The keeper key is hot: it signs unattended every hour from a server. If it is also the owner,
  // that server can setKeeper/setPolicy/setAirdropper and drain the treasury. Owner should be a
  // cold key that never touches the machine running the bot.
  if (keeper.toLowerCase() === deployer.address.toLowerCase() && process.env.ALLOW_SHARED_KEY !== "yes") {
    throw new Error(
      "keeper must not be the owner key — a compromised keeper would then own every contract.\n" +
        "Set KEEPER_ADDRESS to a separate hot wallet (and OWNER_ADDRESS to a cold one),\n" +
        "or pass ALLOW_SHARED_KEY=yes if you really mean to share a single key."
    );
  }

  console.log(`deployer   ${deployer.address}`);
  console.log(`balance    ${ethers.formatEther(await ethers.provider.getBalance(deployer.address))} ETH`);

  const factory = await ethers.getContractAt("IPonsFactory", PONS.factory);

  // Pons caps the creator tax. Read the ceiling rather than assuming 4% clears it.
  const maxTax = await factory.maxCreatorTaxBps();
  console.log(`maxCreatorTaxBps ${maxTax}, requesting ${CREATOR_TAX_BPS}`);
  if (BigInt(CREATOR_TAX_BPS) > maxTax) {
    throw new Error(`creator tax ${CREATOR_TAX_BPS}bps exceeds the Pons cap of ${maxTax}bps`);
  }

  // Nonce n: launchToken, n+1: vault, n+2: airdropper, n+3: treasury.
  const nonce = await ethers.provider.getTransactionCount(deployer.address);
  const predictedTreasury = ethers.getCreateAddress({ from: deployer.address, nonce: nonce + 3 });
  console.log(`treasury will be ${predictedTreasury}`);

  const economics = await factory.previewLaunchEconomics(launchConfigId, IPO_LAUNCH.pairToken);

  const params = {
    name: IPO_LAUNCH.name,
    symbol: IPO_LAUNCH.symbol,
    logo: process.env.LOGO_URI ?? "",
    description:
      "A 4% tax that buys newly bonded Pons projects and hands them to IPO holders.",
    twitter: process.env.TWITTER ?? "",
    telegram: process.env.TELEGRAM ?? "",
    discord: "",
    website: process.env.WEBSITE ?? "",
    farcaster: "",
    creatorFeeRecipient: predictedTreasury,
    creatorTaxBps: CREATOR_TAX_BPS,
    buybackEnabled: IPO_LAUNCH.buybackEnabled,
    expectedEconomics: economics,
    salt: ethers.hexlify(ethers.randomBytes(32)),
  };

  console.log("launching IPO on Pons...");
  const launchTx = await factory.launchToken(params, launchConfigId, IPO_LAUNCH.pairToken, {
    value: ethers.parseEther(process.env.LAUNCH_FEE_ETH ?? "0.0005"),
  });
  const launchReceipt = await launchTx.wait();

  const launched = launchReceipt!.logs
    .map((l) => {
      try {
        return factory.interface.parseLog(l);
      } catch {
        return null;
      }
    })
    .find((l) => l?.name === "TokenLaunched");
  if (!launched) throw new Error("TokenLaunched not found in receipt");

  const ipo = launched.args.token as string;
  const curve = launched.args.curve as string;
  console.log(`IPO   ${ipo}`);
  console.log(`curve ${curve}`);

  const vault = await (await ethers.getContractFactory("IPOVault")).deploy(ipo, deployer.address);
  await vault.waitForDeployment();
  console.log(`vault ${await vault.getAddress()}`);

  const airdropper = await (
    await ethers.getContractFactory("IPOAirdropper")
  ).deploy(deployer.address);
  await airdropper.waitForDeployment();
  console.log(`airdropper ${await airdropper.getAddress()}`);

  const treasury = await (
    await ethers.getContractFactory("IPOTreasury")
  ).deploy(PONS.factory, PONS.feeEscrow, poolManager, PONS.memeHook, ipo, deployer.address);
  await treasury.waitForDeployment();
  const treasuryAddr = await treasury.getAddress();
  console.log(`treasury ${treasuryAddr}`);

  if (treasuryAddr.toLowerCase() !== predictedTreasury.toLowerCase()) {
    throw new Error(
      `treasury landed at ${treasuryAddr} but IPO pays tax to ${predictedTreasury}. ` +
        `Call transferCreatorFeeRecipient(${ipo}, ${treasuryAddr}) to repoint it.`
    );
  }

  console.log("wiring...");
  await (await vault.setTreasury(treasuryAddr)).wait();
  await (await airdropper.setTreasury(treasuryAddr)).wait();
  await (await airdropper.setKeeper(keeper, true)).wait();
  await (await treasury.setAirdropper(await airdropper.getAddress())).wait();
  await (await treasury.setVault(await vault.getAddress())).wait();
  await (await treasury.setKeeper(keeper, true)).wait();
  // Unsold supply sitting on the bonding curve must not dilute redeemers' claims.
  await (await vault.setExcluded(curve, true)).wait();

  const launchBlock = launchReceipt!.blockNumber;

  // Hand ownership to a cold key. Ownable2Step means it is not owner until it accepts, so a
  // mistyped address cannot strand the contracts.
  if (finalOwner) {
    console.log(`\ntransferring ownership to ${finalOwner} (pending until accepted)...`);
    await (await vault.transferOwnership(finalOwner)).wait();
    await (await airdropper.transferOwnership(finalOwner)).wait();
    await (await treasury.transferOwnership(finalOwner)).wait();
  }

  console.log("\n--- deployed ---");
  console.log(`IPO         ${ipo}`);
  console.log(`curve       ${curve}`);
  console.log(`vault       ${await vault.getAddress()}`);
  console.log(`airdropper  ${await airdropper.getAddress()}`);
  console.log(`treasury    ${treasuryAddr}`);
  console.log(`keeper      ${keeper}`);
  if (finalOwner) {
    console.log(
      `\nownership transfer PENDING. From ${finalOwner}, call acceptOwnership() on all three:\n` +
        `  vault      ${await vault.getAddress()}\n` +
        `  airdropper ${await airdropper.getAddress()}\n` +
        `  treasury   ${treasuryAddr}\n` +
        `Until then ${deployer.address} is still owner.`
    );
  } else {
    console.log(
      `\nWARNING: ${deployer.address} owns all three contracts and is the deploying key.\n` +
        `Move ownership to a cold wallet with OWNER_ADDRESS, or transferOwnership() manually.`
    );
  }

  console.log("\n--- run the hourly keeper ---");
  console.log(
    [
      `TREASURY_ADDRESS=${treasuryAddr}`,
      `AIRDROPPER_ADDRESS=${await airdropper.getAddress()}`,
      `VAULT_ADDRESS=${await vault.getAddress()}`,
      `IPO_ADDRESS=${ipo}`,
      `POOL_MANAGER=${poolManager}`,
      `IPO_DEPLOY_BLOCK=${launchBlock}`,
      `PONS_FROM_BLOCK=${launchBlock}`,
      "npm start",
    ].join(" \\\n  ")
  );
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
