/**
 * The hourly cycle.
 *
 *   claim the tax  ->  pick the trending coins  ->  buy them  ->  airdrop them to holders
 *
 * Polls rather than sleeping for exactly an hour: the treasury is the authority on when an epoch
 * may run, so the runner asks it (`epochReady`) every few minutes and acts when the answer is yes.
 * A missed tick, a restart, or an RPC outage therefore costs at most one poll interval rather than
 * skipping the hour entirely.
 *
 *   RPC_URL=... KEEPER_PRIVATE_KEY=0x... TREASURY_ADDRESS=0x... \
 *   AIRDROPPER_ADDRESS=0x... IPO_ADDRESS=0x... POOL_MANAGER=0x... npm start
 */
import {
  createPublicClient,
  createWalletClient,
  http,
  parseAbi,
  parseEventLogs,
  type Address,
  type Hex,
  type PublicClient,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { join } from "node:path";

import { robinhoodChain, PONS } from "../config/addresses";
import { topTrending } from "./trending";
import { snapshotHolders } from "./snapshot";
import { planAirdrop, toBatches } from "./airdrop";
import { factoryAbi } from "./pons";

// --- configuration -----------------------------------------------------

const env = (k: string, fallback?: string) => {
  const v = process.env[k] ?? fallback;
  if (v === undefined) throw new Error(`${k} is required`);
  return v;
};

const RPC_URL = process.env.RPC_URL ?? robinhoodChain.rpcUrls.default.http[0];
const TREASURY = env("TREASURY_ADDRESS") as Address;
const AIRDROPPER = env("AIRDROPPER_ADDRESS") as Address;
const IPO = env("IPO_ADDRESS") as Address;
const POOL_MANAGER = env("POOL_MANAGER") as Address;
const KEEPER_KEY = env("KEEPER_PRIVATE_KEY") as Hex;

/** Blocks of swap history that define "trending". Robinhood Chain targets ~0.25s blocks. */
const TREND_WINDOW_BLOCKS = BigInt(process.env.TREND_WINDOW_BLOCKS ?? 14_400);
/** Optional recency filter on graduations, in blocks. Zero (default) buys coins of any age. */
const MAX_AGE_BLOCKS = BigInt(process.env.MAX_AGE_BLOCKS ?? 0);
/** Where to start scanning for graduations and IPO transfers. */
const PONS_FROM_BLOCK = BigInt(process.env.PONS_FROM_BLOCK ?? 0);
const IPO_DEPLOY_BLOCK = BigInt(process.env.IPO_DEPLOY_BLOCK ?? PONS_FROM_BLOCK);
/** Haircut applied to the simulated output to produce each `minTokensOut`. */
const SLIPPAGE_BPS = BigInt(process.env.SLIPPAGE_BPS ?? 500);
const POLL_MS = Number(process.env.POLL_MS ?? 5 * 60 * 1000);
const DATA_DIR = process.env.DATA_DIR ?? "data";
/** Transfers per airdrop transaction. Sized to stay well inside a block. */
const AIRDROP_BATCH_SIZE = Number(process.env.AIRDROP_BATCH_SIZE ?? 250);
/** Shares below this are skipped and roll into the next hour rather than burning gas. */
const MIN_PAYOUT = BigInt(process.env.MIN_PAYOUT ?? 0);

const treasuryAbi = parseAbi([
  "struct BuyOrder { address token; uint256 minTokensOut; }",
  "function runEpoch((address token, uint256 minTokensOut)[] orders, uint256 deadline) returns (uint256[])",
  "function epochReady() view returns (bool ok, string reason)",
  "function eligibility(address token) view returns (bool ok, string reason)",
  "function maxTokensPerEpoch() view returns (uint256)",
  "function epoch() view returns (uint256)",
  "event Bought(address indexed token, uint256 ethIn, uint256 tokensOut, uint256 toHolders, uint256 toVault)",
  "event EpochRun(uint256 indexed epoch, uint256 tokenCount, uint256 ethSpent)",
]);

const airdropperAbi = parseAbi([
  "function airdrop(uint256 epochId, uint256 batchIndex, address token, address[] recipients, uint256[] amounts) returns (uint256 sent, uint256 paidCount)",
  "function tokens() view returns (address[])",
  "function undistributed(address token) view returns (uint256)",
  "function isBatchSent(uint256 epochId, uint256 batchIndex, address token) view returns (bool)",
]);

const account = privateKeyToAccount(KEEPER_KEY);
const publicClient = createPublicClient({
  chain: robinhoodChain,
  transport: http(RPC_URL),
}) as PublicClient;
const walletClient = createWalletClient({ account, chain: robinhoodChain, transport: http(RPC_URL) });

const log = (...args: unknown[]) => console.log(new Date().toISOString(), ...args);

// --- the cycle ---------------------------------------------------------

/** Rank trending coins, then keep only the ones the treasury will actually accept. */
async function selectCoins(limit: number): Promise<Address[]> {
  const scored = await topTrending(
    publicClient,
    POOL_MANAGER,
    TREND_WINDOW_BLOCKS,
    PONS_FROM_BLOCK,
    limit * 4, // over-fetch: some will fail eligibility
    MAX_AGE_BLOCKS
  );
  log(`trending candidates: ${scored.length}`);

  const picked: Address[] = [];
  for (const s of scored) {
    if (picked.length >= limit) break;
    const [ok, reason] = await publicClient.readContract({
      address: TREASURY,
      abi: treasuryAbi,
      functionName: "eligibility",
      args: [s.token],
    });
    if (!ok) {
      log(`  skip ${s.token} (score ${s.score.toFixed(3)}): ${reason}`);
      continue;
    }
    log(
      `  pick ${s.token} score=${s.score.toFixed(3)} vol=${s.volumeEth} ` +
        `swaps=${s.swaps} traders=${s.traders} move=${s.priceChangePct.toFixed(1)}%`
    );
    picked.push(s.token);
  }
  // The treasury requires strictly ascending token addresses.
  return picked.sort((a, b) => (a.toLowerCase() < b.toLowerCase() ? -1 : 1));
}

/** Buy the selected coins under one epoch, returning what each purchase handed to holders. */
async function buy(tokens: Address[]): Promise<{ purchases: Map<Address, bigint>; epochId: bigint }> {
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 600);

  // Simulate with no bound to obtain a quote, then re-simulate with the real bound so the
  // transaction that gets broadcast is the one that was checked.
  const quote = await publicClient.simulateContract({
    account,
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "runEpoch",
    args: [tokens.map((token) => ({ token, minTokensOut: 0n })), deadline],
  });
  const expected = quote.result as readonly bigint[];

  const orders = tokens.map((token, i) => ({
    token,
    minTokensOut: (expected[i] * (10_000n - SLIPPAGE_BPS)) / 10_000n,
  }));

  const { request } = await publicClient.simulateContract({
    account,
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "runEpoch",
    args: [orders, deadline],
  });

  const hash = await walletClient.writeContract(request);
  log(`epoch tx ${hash}`);
  const receipt = await publicClient.waitForTransactionReceipt({ hash });
  if (receipt.status !== "success") throw new Error(`epoch reverted: ${hash}`);

  const events = parseEventLogs({ abi: treasuryAbi, eventName: "Bought", logs: receipt.logs });
  const purchases = new Map<Address, bigint>();
  for (const e of events) {
    const { token, toHolders } = e.args as { token: Address; toHolders: bigint };
    purchases.set(token, (purchases.get(token) ?? 0n) + toHolders);
  }
  const [epochRun] = parseEventLogs({ abi: treasuryAbi, eventName: "EpochRun", logs: receipt.logs });
  const epochId = (epochRun?.args as { epoch: bigint } | undefined)?.epoch ?? 0n;
  log(`epoch ${epochId}: bought ${purchases.size} coins in block ${receipt.blockNumber}`);
  return { purchases, epochId };
}

/** IPO balances of everyone entitled to a share, excluding protocol-owned and curve supply. */
async function entitledHolders(): Promise<Map<Address, bigint>> {
  const head = await publicClient.getBlockNumber();
  const launch = await publicClient.readContract({
    address: PONS.factory as Address,
    abi: factoryAbi,
    functionName: "getLaunchedToken",
    args: [IPO],
  });
  const excluded: Address[] = [launch.curve, TREASURY, AIRDROPPER];
  if (process.env.VAULT_ADDRESS) excluded.push(process.env.VAULT_ADDRESS as Address);

  const balances = await snapshotHolders(
    publicClient,
    IPO,
    IPO_DEPLOY_BLOCK,
    head,
    excluded,
    join(DATA_DIR, "ipo-holders.json")
  );
  log(`snapshot: ${balances.size} entitled holders at block ${head}`);
  return balances;
}

/**
 * Push every coin the airdropper is holding out to IPO holders.
 *
 * Works from `undistributed(token)` rather than this epoch's purchase alone, so rounding dust and
 * anything a previous run failed to deliver is picked up here instead of being stranded.
 */
async function airdropAll(epochId: bigint) {
  const tokens = await publicClient.readContract({
    address: AIRDROPPER,
    abi: airdropperAbi,
    functionName: "tokens",
  });
  if (tokens.length === 0) return;

  const available = new Map<Address, bigint>();
  for (const token of tokens) {
    const amount = await publicClient.readContract({
      address: AIRDROPPER,
      abi: airdropperAbi,
      functionName: "undistributed",
      args: [token],
    });
    if (amount > 0n) available.set(token, amount);
  }
  if (available.size === 0) {
    log("nothing undistributed");
    return;
  }

  const balances = await entitledHolders();
  if (balances.size === 0) {
    log("no entitled holders; coins roll into the next hour");
    return;
  }

  const plans = planAirdrop(available, balances, MIN_PAYOUT);
  log(`airdropping ${plans.length} coins to ${balances.size} holders`);

  for (const plan of plans) {
    const batches = toBatches(plan, AIRDROP_BATCH_SIZE);
    log(
      `  ${plan.token}: ${plan.recipients.length} recipients in ${batches.length} batch(es), ` +
        `${plan.dusted} dusted, ${plan.remainder} rolling forward`
    );

    for (const batch of batches) {
      try {
        // Defensive: a retry inside the same epoch would revert on-chain anyway.
        const already = await publicClient.readContract({
          address: AIRDROPPER,
          abi: airdropperAbi,
          functionName: "isBatchSent",
          args: [epochId, BigInt(batch.batchIndex), batch.token],
        });
        if (already) {
          log(`    batch ${batch.batchIndex} already sent, skipping`);
          continue;
        }

        const { request } = await publicClient.simulateContract({
          account,
          address: AIRDROPPER,
          abi: airdropperAbi,
          functionName: "airdrop",
          args: [epochId, BigInt(batch.batchIndex), batch.token, batch.recipients, batch.amounts],
        });
        const hash = await walletClient.writeContract(request);
        const receipt = await publicClient.waitForTransactionReceipt({ hash });
        log(
          `    batch ${batch.batchIndex}: ${batch.recipients.length} transfers, ` +
            `${receipt.status}, gas ${receipt.gasUsed}`
        );
      } catch (err) {
        // One failed batch must not abandon the rest; the remainder rolls into the next hour.
        log(`    batch ${batch.batchIndex} FAILED: ${(err as Error).message.split("\n")[0]}`);
      }
    }
  }
}

async function tick() {
  const [ready, reason] = await publicClient.readContract({
    address: TREASURY,
    abi: treasuryAbi,
    functionName: "epochReady",
  });
  if (!ready) {
    log(`not ready: ${reason}`);
    return;
  }

  const limit = Number(
    await publicClient.readContract({
      address: TREASURY,
      abi: treasuryAbi,
      functionName: "maxTokensPerEpoch",
    })
  );

  const tokens = await selectCoins(limit);
  if (tokens.length === 0) {
    log("no eligible trending coins this hour");
    return;
  }

  const { epochId } = await buy(tokens);
  await airdropAll(epochId);
}

async function main() {
  log(`keeper     ${account.address}`);
  log(`treasury   ${TREASURY}`);
  log(`airdropper ${AIRDROPPER}`);
  log(`polling every ${POLL_MS / 1000}s`);

  const run = () => tick().catch((err) => log("cycle failed:", (err as Error).message));
  await run();
  setInterval(run, POLL_MS);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
