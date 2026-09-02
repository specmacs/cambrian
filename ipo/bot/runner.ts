/**
 * The hourly cycle.
 *
 *   claim the tax  ->  pick the trending coins  ->  buy them  ->  publish who is owed what
 *
 * Polls rather than sleeping for exactly an hour: the treasury is the authority on when an epoch
 * may run, so the runner asks it (`epochReady`) every few minutes and acts when the answer is yes.
 * A missed tick, a restart, or an RPC outage therefore costs at most one poll interval rather than
 * skipping the hour entirely.
 *
 *   RPC_URL=... KEEPER_PRIVATE_KEY=0x... TREASURY_ADDRESS=0x... \
 *   DISTRIBUTOR_ADDRESS=0x... IPO_ADDRESS=0x... POOL_MANAGER=0x... npm run start
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
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { robinhoodChain, PONS } from "../config/addresses";
import { topTrending } from "./trending";
import { snapshotHolders } from "./snapshot";
import { CumulativeMerkleTree, accrue, type Entitlement } from "./merkle";
import { factoryAbi } from "./pons";

// --- configuration -----------------------------------------------------

const env = (k: string, fallback?: string) => {
  const v = process.env[k] ?? fallback;
  if (v === undefined) throw new Error(`${k} is required`);
  return v;
};

const RPC_URL = process.env.RPC_URL ?? robinhoodChain.rpcUrls.default.http[0];
const TREASURY = env("TREASURY_ADDRESS") as Address;
const DISTRIBUTOR = env("DISTRIBUTOR_ADDRESS") as Address;
const IPO = env("IPO_ADDRESS") as Address;
const POOL_MANAGER = env("POOL_MANAGER") as Address;
const KEEPER_KEY = env("KEEPER_PRIVATE_KEY") as Hex;

/** Blocks of swap history that define "trending". Robinhood Chain targets ~0.25s blocks. */
const TREND_WINDOW_BLOCKS = BigInt(process.env.TREND_WINDOW_BLOCKS ?? 14_400);
/** Where to start scanning for graduations and IPO transfers. */
const PONS_FROM_BLOCK = BigInt(process.env.PONS_FROM_BLOCK ?? 0);
const IPO_DEPLOY_BLOCK = BigInt(process.env.IPO_DEPLOY_BLOCK ?? PONS_FROM_BLOCK);
/** Haircut applied to the simulated output to produce each `minTokensOut`. */
const SLIPPAGE_BPS = BigInt(process.env.SLIPPAGE_BPS ?? 500);
const POLL_MS = Number(process.env.POLL_MS ?? 5 * 60 * 1000);
const DATA_DIR = process.env.DATA_DIR ?? "data";

const treasuryAbi = parseAbi([
  "struct BuyOrder { address token; uint256 minTokensOut; }",
  "function runEpoch((address token, uint256 minTokensOut)[] orders, uint256 deadline) returns (uint256[])",
  "function epochReady() view returns (bool ok, string reason)",
  "function eligibility(address token) view returns (bool ok, string reason)",
  "function maxTokensPerEpoch() view returns (uint256)",
  "function epoch() view returns (uint256)",
  "event Bought(address indexed token, uint256 ethIn, uint256 tokensOut, uint256 toHolders, uint256 toVault)",
]);

const distributorAbi = parseAbi([
  "function publishRoot(bytes32 root)",
  "function epoch() view returns (uint256)",
]);

const account = privateKeyToAccount(KEEPER_KEY);
const publicClient = createPublicClient({
  chain: robinhoodChain,
  transport: http(RPC_URL),
}) as PublicClient;
const walletClient = createWalletClient({ account, chain: robinhoodChain, transport: http(RPC_URL) });

const log = (...args: unknown[]) => console.log(new Date().toISOString(), ...args);

// --- persistence -------------------------------------------------------

const entitlementsPath = join(DATA_DIR, "entitlements.json");
const proofsPath = join(DATA_DIR, "proofs.json");
const snapshotPath = join(DATA_DIR, "ipo-holders.json");

function loadEntitlements(): Entitlement[] {
  if (!existsSync(entitlementsPath)) return [];
  const raw = JSON.parse(readFileSync(entitlementsPath, "utf8")) as any[];
  return raw.map((e) => ({
    account: e.account as Address,
    token: e.token as Address,
    cumulativeAmount: BigInt(e.cumulativeAmount),
  }));
}

/**
 * Write the tree out alongside a per-holder proof index.
 *
 * Claiming needs a proof, and a proof cannot be derived from on-chain state — so if this file is
 * lost, every holder's entitlement is unclaimable until the tree is rebuilt. Serve it publicly and
 * keep backups.
 */
function saveEntitlements(entitlements: Entitlement[], tree: CumulativeMerkleTree) {
  mkdirSync(DATA_DIR, { recursive: true });
  writeFileSync(
    entitlementsPath,
    JSON.stringify(
      entitlements.map((e) => ({ ...e, cumulativeAmount: e.cumulativeAmount.toString() })),
      null,
      2
    )
  );

  const byAccount: Record<string, Record<string, { cumulativeAmount: string; proof: Hex[] }>> = {};
  for (const e of entitlements) {
    const a = e.account.toLowerCase();
    byAccount[a] ??= {};
    byAccount[a][e.token.toLowerCase()] = {
      cumulativeAmount: e.cumulativeAmount.toString(),
      proof: tree.proof(e),
    };
  }
  writeFileSync(proofsPath, JSON.stringify({ root: tree.root, accounts: byAccount }, null, 2));
}

// --- the cycle ---------------------------------------------------------

/** Rank trending coins, then keep only the ones the treasury will actually accept. */
async function selectCoins(limit: number): Promise<Address[]> {
  const scored = await topTrending(
    publicClient,
    POOL_MANAGER,
    TREND_WINDOW_BLOCKS,
    PONS_FROM_BLOCK,
    limit * 4 // over-fetch: some will fail eligibility
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
async function buy(tokens: Address[]): Promise<Map<Address, bigint>> {
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
  log(`bought ${purchases.size} coins in block ${receipt.blockNumber}`);
  return purchases;
}

/** Fold the hour's purchases into cumulative entitlements and publish the new root. */
async function distribute(purchases: Map<Address, bigint>) {
  if (purchases.size === 0) return;

  const head = await publicClient.getBlockNumber();

  // The bonding curve's unsold supply, and protocol-owned balances, are not entitled to anything.
  const launch = await publicClient.readContract({
    address: PONS.factory as Address,
    abi: factoryAbi,
    functionName: "getLaunchedToken",
    args: [IPO],
  });
  const excluded: Address[] = [launch.curve, TREASURY, DISTRIBUTOR];
  if (process.env.VAULT_ADDRESS) excluded.push(process.env.VAULT_ADDRESS as Address);

  const balances = await snapshotHolders(
    publicClient,
    IPO,
    IPO_DEPLOY_BLOCK,
    head,
    excluded,
    snapshotPath
  );
  log(`snapshot: ${balances.size} entitled holders at block ${head}`);
  if (balances.size === 0) {
    log("no entitled holders; skipping root publication");
    return;
  }

  const next = accrue(loadEntitlements(), balances, purchases);
  const tree = new CumulativeMerkleTree(next);

  const { request } = await publicClient.simulateContract({
    account,
    address: DISTRIBUTOR,
    abi: distributorAbi,
    functionName: "publishRoot",
    args: [tree.root],
  });
  const hash = await walletClient.writeContract(request);
  await publicClient.waitForTransactionReceipt({ hash });

  saveEntitlements(next, tree);
  log(`published root ${tree.root} covering ${next.length} entitlements: ${hash}`);
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

  const purchases = await buy(tokens);
  await distribute(purchases);
}

async function main() {
  log(`keeper     ${account.address}`);
  log(`treasury   ${TREASURY}`);
  log(`distributor ${DISTRIBUTOR}`);
  log(`polling every ${POLL_MS / 1000}s`);

  const run = () => tick().catch((err) => log("cycle failed:", (err as Error).message));
  await run();
  setInterval(run, POLL_MS);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
