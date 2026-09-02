/**
 * Launches IPO on Pons and deploys the treasury + vault behind it.
 *
 * The treasury must be the creator-fee recipient from the very first trade, so its address is
 * predicted from the deployer's nonce and passed into the launch. Deployment then asserts the
 * treasury actually landed there. Nothing is broadcast without CONFIRM_LAUNCH=yes.
 *
 *   CONFIRM_LAUNCH=yes POOL_MANAGER=0x... KEEPER_ADDRESS=0x... \
 *     npx hardhat run script/deploy.ts --network robinhood
 */
import { ethers } from "hardhat";
import { PONS, IPO_LAUNCH } from "../config/addresses";

const CREATOR_TAX_BPS = IPO_LAUNCH.creatorTaxBps; // 400 = 4%

async function main() {
  const [deployer] = await ethers.getSigners();
  const poolManager = process.env.POOL_MANAGER;
  const keeper = process.env.KEEPER_ADDRESS ?? deployer.address;
  const launchConfigId = BigInt(process.env.LAUNCH_CONFIG_ID ?? 0);

  if (!poolManager || poolManager === ethers.ZeroAddress) {
    throw new Error("POOL_MANAGER is required (Uniswap v4 PoolManager on Robinhood Chain)");
  }
  if (process.env.CONFIRM_LAUNCH !== "yes") {
    throw new Error("refusing to launch: set CONFIRM_LAUNCH=yes to broadcast");
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

  // Nonce n: launchToken, n+1: vault, n+2: distributor, n+3: treasury.
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

  const distributor = await (
    await ethers.getContractFactory("IPODistributor")
  ).deploy(deployer.address);
  await distributor.waitForDeployment();
  console.log(`distributor ${await distributor.getAddress()}`);

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
  await (await distributor.setTreasury(treasuryAddr)).wait();
  await (await distributor.setPublisher(keeper)).wait();
  await (await treasury.setDistributor(await distributor.getAddress())).wait();
  await (await treasury.setVault(await vault.getAddress())).wait();
  await (await treasury.setKeeper(keeper, true)).wait();
  // Unsold supply sitting on the bonding curve must not dilute redeemers' claims.
  await (await vault.setExcluded(curve, true)).wait();

  const launchBlock = launchReceipt!.blockNumber;

  console.log("\n--- deployed ---");
  console.log(`IPO         ${ipo}`);
  console.log(`curve       ${curve}`);
  console.log(`vault       ${await vault.getAddress()}`);
  console.log(`distributor ${await distributor.getAddress()}`);
  console.log(`treasury    ${treasuryAddr}`);
  console.log(`keeper      ${keeper}`);
  console.log("\n--- run the hourly keeper ---");
  console.log(
    [
      `TREASURY_ADDRESS=${treasuryAddr}`,
      `DISTRIBUTOR_ADDRESS=${await distributor.getAddress()}`,
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
