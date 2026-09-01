/**
 * Graduation watcher.
 *
 * Watches the Pons factory for `PoolGraduated`, asks the treasury whether the launch is eligible,
 * simulates the purchase to get a quote, and submits the buy with a slippage bound derived from it.
 *
 * The bot cannot redirect funds. It only names a token; the treasury reads that token's launch
 * record from Pons, rebuilds the pool key itself, and swaps. A compromised keeper key can waste
 * `buySize` on a bad launch that still passes the on-chain filters, and nothing more.
 *
 *   RPC_URL=... KEEPER_PRIVATE_KEY=0x... TREASURY_ADDRESS=0x... npm run watch
 */
import {
  createPublicClient,
  createWalletClient,
  http,
  parseAbi,
  type Address,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { robinhoodChain, PONS } from "../config/addresses";

const RPC_URL = process.env.RPC_URL ?? robinhoodChain.rpcUrls.default.http[0];
const TREASURY = process.env.TREASURY_ADDRESS as Address;
const KEEPER_KEY = process.env.KEEPER_PRIVATE_KEY as Hex;
/** Haircut applied to the simulated output to produce `minTokensOut`. */
const SLIPPAGE_BPS = BigInt(process.env.SLIPPAGE_BPS ?? 300);
/** How far back to sweep for graduations missed while the bot was down. */
const BACKFILL_BLOCKS = BigInt(process.env.BACKFILL_BLOCKS ?? 5000);

if (!TREASURY) throw new Error("TREASURY_ADDRESS is required");
if (!KEEPER_KEY) throw new Error("KEEPER_PRIVATE_KEY is required");

const factoryAbi = parseAbi([
  "event PoolGraduated(address indexed token, address indexed curve, address pairToken)",
]);

const treasuryAbi = parseAbi([
  "function eligibility(address token) view returns (bool ok, string reason)",
  "function buy(address token, uint256 minTokensOut, uint256 deadline) returns (uint256)",
  "function claimTax() returns (uint256)",
  "function buySize() view returns (uint256)",
  "function minReserve() view returns (uint256)",
]);

const escrowAbi = parseAbi(["function balanceOf(address) view returns (uint256)"]);

const account = privateKeyToAccount(KEEPER_KEY);
const publicClient = createPublicClient({ chain: robinhoodChain, transport: http(RPC_URL) });
const walletClient = createWalletClient({ account, chain: robinhoodChain, transport: http(RPC_URL) });

const log = (...args: unknown[]) => console.log(new Date().toISOString(), ...args);

/** Pull accrued tax out of the Pons escrow when there is enough to matter. */
async function sweepTax() {
  const accrued = await publicClient.readContract({
    address: PONS.feeEscrow,
    abi: escrowAbi,
    functionName: "balanceOf",
    args: [TREASURY],
  });
  if (accrued === 0n) return;

  const buySize = await publicClient.readContract({
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "buySize",
  });
  // Claiming costs gas; wait until the tax is worth at least one purchase.
  if (accrued < buySize) return;

  log(`claiming ${accrued} wei of accrued tax`);
  const { request } = await publicClient.simulateContract({
    account,
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "claimTax",
  });
  const hash = await walletClient.writeContract(request);
  await publicClient.waitForTransactionReceipt({ hash });
  log(`tax claimed: ${hash}`);
}

async function tryBuy(token: Address) {
  const [ok, reason] = await publicClient.readContract({
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "eligibility",
    args: [token],
  });

  if (!ok) {
    // "insufficient funds" and "cooldown active" are transient; the next graduation retries.
    log(`skip ${token}: ${reason}`);
    return;
  }

  const deadline = BigInt(Math.floor(Date.now() / 1000) + 300);

  // Simulate with no slippage bound to obtain the quote, then re-simulate with the real bound so
  // the transaction we actually broadcast is the one that was checked.
  const quote = await publicClient.simulateContract({
    account,
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "buy",
    args: [token, 0n, deadline],
  });
  const expected = quote.result as bigint;
  const minOut = (expected * (10_000n - SLIPPAGE_BPS)) / 10_000n;

  const { request } = await publicClient.simulateContract({
    account,
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "buy",
    args: [token, minOut, deadline],
  });

  const hash = await walletClient.writeContract(request);
  log(`buying ${token} (expect ${expected}, min ${minOut}): ${hash}`);
  const receipt = await publicClient.waitForTransactionReceipt({ hash });
  log(`buy ${receipt.status} in block ${receipt.blockNumber}`);
}

/** Catch graduations that happened while the bot was offline. */
async function backfill() {
  const head = await publicClient.getBlockNumber();
  const from = head > BACKFILL_BLOCKS ? head - BACKFILL_BLOCKS : 0n;
  const logs = await publicClient.getLogs({
    address: PONS.factory,
    event: factoryAbi[0],
    fromBlock: from,
    toBlock: head,
  });
  log(`backfill: ${logs.length} graduations in blocks ${from}-${head}`);
  for (const entry of logs) {
    const token = entry.args.token as Address;
    try {
      await tryBuy(token);
    } catch (err) {
      log(`backfill buy failed for ${token}:`, (err as Error).message);
    }
  }
}

async function main() {
  log(`keeper ${account.address} watching Pons factory ${PONS.factory}`);
  log(`treasury ${TREASURY}`);

  await sweepTax().catch((e) => log("tax sweep failed:", e.message));
  await backfill().catch((e) => log("backfill failed:", e.message));

  publicClient.watchEvent({
    address: PONS.factory,
    event: factoryAbi[0],
    onLogs: async (logs) => {
      for (const entry of logs) {
        const token = entry.args.token as Address;
        log(`PoolGraduated: ${token}`);
        try {
          await sweepTax();
          await tryBuy(token);
        } catch (err) {
          log(`buy failed for ${token}:`, (err as Error).message);
        }
      }
    },
    onError: (err) => log("watch error:", err.message),
  });

  // A graduation may arrive while the treasury is in cooldown or short on funds. Re-check
  // periodically so a skipped launch still gets bought inside its eligibility window.
  setInterval(() => {
    sweepTax().catch((e) => log("tax sweep failed:", e.message));
  }, 10 * 60 * 1000);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
