/**
 * Ranks graduated Pons coins by how hard they are trending right now.
 *
 * "Trending" has no on-chain definition, so it is computed here from Uniswap v4 `Swap` events over
 * a trailing window and handed to the treasury as a list of tokens. The treasury independently
 * verifies that each one is a real, graduated, ETH-paired Pons launch and caps what can be spent —
 * so a bad score wastes an epoch's budget, it cannot redirect funds.
 *
 * Four signals, each normalised to the best coin in the window so they combine on one scale:
 *
 *   volume     ETH traded            — the baseline "is anything happening here"
 *   buyRatio   share of swaps buying — separates accumulation from distribution
 *   traders    unique addresses      — a proxy for breadth, since one whale is not a trend
 *   momentum   price change          — direction of travel over the window
 *
 * Volume alone is trivially wash-traded by one address cycling a large balance; requiring breadth
 * and direction alongside it makes that meaningfully more expensive.
 */
import type { Address, Hex, PublicClient } from "viem";
import { PONS } from "../config/addresses";
import { getLogsChunked, graduatedLaunches, poolManagerAbi, type Launch } from "./pons";

export interface TrendScore {
  token: Address;
  score: number;
  volumeEth: bigint;
  swaps: number;
  buys: number;
  traders: number;
  priceChangePct: number;
}

export interface TrendWeights {
  volume: number;
  buyRatio: number;
  traders: number;
  momentum: number;
}

export const DEFAULT_WEIGHTS: TrendWeights = {
  volume: 0.4,
  buyRatio: 0.2,
  traders: 0.25,
  momentum: 0.15,
};

interface PoolActivity {
  volumeEth: bigint;
  swaps: number;
  buys: number;
  traders: Set<string>;
  firstSqrtPrice?: bigint;
  lastSqrtPrice?: bigint;
}

/**
 * Score every graduated launch that traded in [fromBlock, toBlock].
 *
 * @param poolManager Uniswap v4 PoolManager, the only emitter of v4 swaps.
 * @param launchFromBlock Block to start scanning for graduations. Deployment block of the Pons
 *        factory is correct but slow; a cached recent block is fine once the registry is warm.
 */
export async function scoreTrending(
  client: PublicClient,
  poolManager: Address,
  fromBlock: bigint,
  toBlock: bigint,
  launchFromBlock: bigint,
  weights: TrendWeights = DEFAULT_WEIGHTS
): Promise<TrendScore[]> {
  const launches = await graduatedLaunches(client, launchFromBlock, toBlock);
  if (launches.size === 0) return [];

  const swaps = await getLogsChunked<any>(client, {
    address: poolManager,
    event: poolManagerAbi[0],
    fromBlock,
    toBlock,
  });

  const activity = new Map<Hex, PoolActivity>();
  for (const log of swaps) {
    const id = log.args.id as Hex;
    if (!launches.has(id)) continue; // not a Pons pool

    let a = activity.get(id);
    if (!a) {
      a = { volumeEth: 0n, swaps: 0, buys: 0, traders: new Set() };
      activity.set(id, a);
    }

    // amount0 is the swapper's ETH delta: negative means they paid ETH, i.e. a buy.
    const amount0 = log.args.amount0 as bigint;
    a.volumeEth += amount0 < 0n ? -amount0 : amount0;
    a.swaps += 1;
    if (amount0 < 0n) a.buys += 1;
    a.traders.add((log.args.sender as string).toLowerCase());
    a.lastSqrtPrice = log.args.sqrtPriceX96 as bigint;
    if (a.firstSqrtPrice === undefined) a.firstSqrtPrice = log.args.sqrtPriceX96 as bigint;
  }

  const raw = [...activity.entries()].map(([poolId, a]) => {
    const launch = launches.get(poolId) as Launch;
    // Price is proportional to sqrtPrice squared; compare in floating point, the ratio is small.
    const first = Number(a.firstSqrtPrice ?? 0n);
    const last = Number(a.lastSqrtPrice ?? 0n);
    const priceChangePct = first > 0 ? ((last / first) ** 2 - 1) * 100 : 0;
    return {
      token: launch.token,
      volumeEth: a.volumeEth,
      swaps: a.swaps,
      buys: a.buys,
      traders: a.traders.size,
      priceChangePct,
    };
  });

  if (raw.length === 0) return [];

  const maxVolume = raw.reduce((m, r) => (r.volumeEth > m ? r.volumeEth : m), 0n);
  const maxTraders = Math.max(...raw.map((r) => r.traders));
  // Momentum is clamped to ±100% so one parabolic coin cannot flatten every other score.
  const clamp = (v: number) => Math.max(-100, Math.min(100, v));

  const scored = raw.map((r) => {
    const volume = maxVolume > 0n ? Number((r.volumeEth * 10_000n) / maxVolume) / 10_000 : 0;
    const buyRatio = r.swaps > 0 ? r.buys / r.swaps : 0;
    const traders = maxTraders > 0 ? r.traders / maxTraders : 0;
    const momentum = (clamp(r.priceChangePct) + 100) / 200; // map [-100,100] onto [0,1]

    const score =
      weights.volume * volume +
      weights.buyRatio * buyRatio +
      weights.traders * traders +
      weights.momentum * momentum;

    return { ...r, score };
  });

  return scored.sort((a, b) => b.score - a.score);
}

/** Convenience wrapper reading the pool manager from the environment. */
export async function topTrending(
  client: PublicClient,
  poolManager: Address,
  windowBlocks: bigint,
  launchFromBlock: bigint,
  limit: number
): Promise<TrendScore[]> {
  const head = await client.getBlockNumber();
  const from = head > windowBlocks ? head - windowBlocks : 0n;
  const scores = await scoreTrending(client, poolManager, from, head, launchFromBlock);
  return scores.slice(0, limit);
}

export { PONS };
