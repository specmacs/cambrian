/**
 * IPO holder balances, rebuilt from Transfer events.
 *
 * Pons tokens are plain fixed-supply ERC-20s with no snapshot hook, so balances have to be
 * reconstructed off-chain. The scan is incremental: each run resumes from the last block it
 * processed and folds new transfers into the cached balances.
 */
import { parseAbi, type Address, type PublicClient } from "viem";
import { getLogsChunked } from "./pons";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname } from "node:path";

const erc20Abi = parseAbi([
  "event Transfer(address indexed from, address indexed to, uint256 value)",
]);

const ZERO = "0x0000000000000000000000000000000000000000";

export interface SnapshotState {
  lastBlock: string;
  balances: Record<string, string>;
}

export function loadState(path: string): SnapshotState | undefined {
  if (!existsSync(path)) return undefined;
  return JSON.parse(readFileSync(path, "utf8")) as SnapshotState;
}

export function saveState(path: string, state: SnapshotState) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(state, null, 2));
}

/**
 * Bring the cached balance set up to `toBlock`.
 *
 * @param excluded Addresses that hold IPO but are not entitled to distributions — the bonding
 *        curve's unsold supply, the treasury, the vault, and the distributor itself.
 */
export async function snapshotHolders(
  client: PublicClient,
  ipo: Address,
  deployBlock: bigint,
  toBlock: bigint,
  excluded: Address[],
  statePath?: string
): Promise<Map<Address, bigint>> {
  const cached = statePath ? loadState(statePath) : undefined;
  const balances = new Map<string, bigint>();
  let fromBlock = deployBlock;

  if (cached) {
    for (const [addr, bal] of Object.entries(cached.balances)) balances.set(addr, BigInt(bal));
    fromBlock = BigInt(cached.lastBlock) + 1n;
  }

  if (fromBlock <= toBlock) {
    const logs = await getLogsChunked<any>(client, {
      address: ipo,
      event: erc20Abi[0],
      fromBlock,
      toBlock,
    });

    for (const log of logs) {
      const from = (log.args.from as string).toLowerCase();
      const to = (log.args.to as string).toLowerCase();
      const value = log.args.value as bigint;
      if (from !== ZERO) balances.set(from, (balances.get(from) ?? 0n) - value);
      if (to !== ZERO) balances.set(to, (balances.get(to) ?? 0n) + value);
    }
  }

  if (statePath) {
    saveState(statePath, {
      lastBlock: toBlock.toString(),
      balances: Object.fromEntries([...balances].map(([k, v]) => [k, v.toString()])),
    });
  }

  const skip = new Set(excluded.map((a) => a.toLowerCase()));
  const out = new Map<Address, bigint>();
  for (const [addr, bal] of balances) {
    if (bal > 0n && !skip.has(addr)) out.set(addr as Address, bal);
  }
  return out;
}
