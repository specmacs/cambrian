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
 * Source: https://docs.ponsfamily.com/v2 — verify on-chain before mainnet use.
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
 * Uniswap v4 PoolManager on Robinhood Chain.
 * NOT yet verified — populate from https://docs.uniswap.org/contracts/v4/deployments
 * (or the deployments.json feed) before deploying. Deployment will refuse a zero address.
 */
export const UNISWAP_V4 = {
  poolManager: process.env.POOL_MANAGER ?? "",
} as const;

/** IPO launch parameters. */
export const IPO_LAUNCH = {
  name: "Initial Pons Offering",
  symbol: "IPO",
  /** 4% creator tax, charged by Pons on the quote side of every buy and sell. */
  creatorTaxBps: 400,
  /** Pons pays the creator tax to this address; it must be the IPOTreasury. */
  creatorFeeRecipientEnv: "TREASURY_ADDRESS",
  /** Buybacks route creator fees back into IPO; we want that ETH funding project purchases. */
  buybackEnabled: false,
  pairToken: "0x0000000000000000000000000000000000000000", // native ETH
} as const;
