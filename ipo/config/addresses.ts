import { defineChain } from "viem";

/** Robinhood Chain — Arbitrum Orbit L2. */
export const robinhoodChain = defineChain({
  id: 4663,
  name: "Robinhood Chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: ["https://rpc.mainnet.chain.robinhood.com"] } },
});

/**
 * Pons v2 deployment on Robinhood Chain.
 *
 * Every address below was confirmed to hold code on mainnet, and the factory's own
 * `feeEscrow()`, `memeHook()`, `buybackVault()` and `poolManager()` getters agree with them.
 * Re-verify with `npx tsx script/probe.ts`.
 */
export const PONS = {
  factory: "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e",
  memeHook: "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044",
  feeEscrow: "0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e",
  buybackVault: "0x42df2a798f82289E177311362e8f5ccC45c1219c",
  launchLocker: "0x267444D099b10fB5Ed7c3Cc7B7c767AdcA574952",
  launchAndBuyRouter: "0xe33E9E479dF8802cb0866d5d05258bEc4cF62948",
} as const;

/**
 * Uniswap v4 on Robinhood Chain.
 *
 * Read directly off-chain from both `PonsFactory.poolManager()` and `PonsMemeHook.poolManager()`,
 * which agree. Its `Swap` topic0 is 0x40e9cecb…, matching the standard v4 signature
 * `Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)`.
 */
export const UNISWAP_V4 = {
  poolManager: process.env.POOL_MANAGER ?? "0x8366a39cc670b4001a1121b8f6a443a643e40951",
} as const;

/**
 * Protocol ceiling on `creatorTaxBps`, read from `PonsFactory.maxCreatorTaxBps()`.
 * IPO's 4% sits well inside it; live launches already carry 400.
 */
export const MAX_CREATOR_TAX_BPS = 1000; // 10%

/**
 * The only launch config Pons currently exposes (`launchConfigCount()` returns 1).
 *   supply 1e27 (1B x 1e18), curveFee 1%, phantomQuote 1.68 ETH,
 *   graduationThreshold 4.2 ETH, poolFee 0, tickSpacing 200.
 */
export const LAUNCH_CONFIG_ID = 0;

/** IPO launch parameters. */
export const IPO_LAUNCH = {
  name: "Initial Pons Offering",
  symbol: "IPO",
  /** 4% creator tax, charged by Pons on the quote side of every buy and sell. Cap is 1000. */
  creatorTaxBps: 400,
  /** Pons pays the creator tax to this address; it must be the IPOTreasury. */
  creatorFeeRecipientEnv: "TREASURY_ADDRESS",
  /** Buybacks route creator fees back into IPO; we want that ETH funding project purchases. */
  buybackEnabled: false,
  pairToken: "0x0000000000000000000000000000000000000000", // native ETH
} as const;
