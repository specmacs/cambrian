/**
 * Shared Pons/Uniswap-v4 plumbing: which launches exist, and which v4 pool each graduated one
 * trades in.
 */
import {
  encodeAbiParameters,
  keccak256,
  parseAbi,
  type Address,
  type Hex,
  type PublicClient,
} from "viem";
import { PONS } from "../config/addresses";

export const factoryAbi = parseAbi([
  "event TokenLaunched(address indexed token, address indexed curve, address indexed deployer, address pairToken, uint256 launchConfigId, uint256 graduationThreshold)",
  "event PoolGraduated(address indexed token, address indexed curve, address pairToken)",
  "function getLaunchedToken(address token) view returns ((address token,address curve,address deployer,address creatorFeeRecipient,address pairToken,uint256 graduationThreshold,uint24 poolFee,int24 tickSpacing,uint16 creatorTaxBps,bool buybackEnabled,uint8 phase,uint256 sweptQuote,uint256 sweptTokens,uint256 sweptAt,bool exists))",
]);

/** Uniswap v4 emits one Swap per pool id; there is no per-token event to listen to. */
export const poolManagerAbi = parseAbi([
  "event Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)",
]);

export const NATIVE: Address = "0x0000000000000000000000000000000000000000";

export interface Launch {
  token: Address;
  curve: Address;
  poolFee: number;
  tickSpacing: number;
  graduationThreshold: bigint;
  sweptAt: bigint;
  phase: number;
  pairToken: Address;
  poolId: Hex;
}

/**
 * Pool id for a graduated Pons launch: keccak256 of the abi-encoded PoolKey, as v4 computes it.
 * Native ETH is address(0) and therefore always currency0.
 */
export function poolIdFor(token: Address, poolFee: number, tickSpacing: number): Hex {
  return keccak256(
    encodeAbiParameters(
      [
        {
          type: "tuple",
          components: [
            { name: "currency0", type: "address" },
            { name: "currency1", type: "address" },
            { name: "fee", type: "uint24" },
            { name: "tickSpacing", type: "int24" },
            { name: "hooks", type: "address" },
          ],
        },
      ],
      [
        {
          currency0: NATIVE,
          currency1: token,
          fee: poolFee,
          tickSpacing,
          hooks: PONS.memeHook as Address,
        },
      ]
    )
  );
}

/** Fetch logs in chunks, since public RPCs cap the block range of a single query. */
export async function getLogsChunked<T>(
  client: PublicClient,
  params: { address: Address; event: any; fromBlock: bigint; toBlock: bigint },
  chunkSize = 5_000n
): Promise<T[]> {
  const out: T[] = [];
  for (let from = params.fromBlock; from <= params.toBlock; from += chunkSize) {
    const to = from + chunkSize - 1n > params.toBlock ? params.toBlock : from + chunkSize - 1n;
    const logs = await client.getLogs({
      address: params.address,
      event: params.event,
      fromBlock: from,
      toBlock: to,
    });
    out.push(...(logs as T[]));
  }
  return out;
}

/**
 * Every launch that has graduated into a v4 pool, keyed by pool id.
 *
 * Reads `PoolGraduated` for the candidate set, then re-reads each launch record from the factory,
 * because the event tells us a pool exists but not the tick spacing needed to identify it.
 */
export async function graduatedLaunches(
  client: PublicClient,
  fromBlock: bigint,
  toBlock: bigint
): Promise<Map<Hex, Launch>> {
  const logs = await getLogsChunked<any>(client, {
    address: PONS.factory as Address,
    event: factoryAbi[1],
    fromBlock,
    toBlock,
  });

  const byPool = new Map<Hex, Launch>();
  for (const log of logs) {
    const token = log.args.token as Address;
    try {
      const r = await client.readContract({
        address: PONS.factory as Address,
        abi: factoryAbi,
        functionName: "getLaunchedToken",
        args: [token],
      });
      if (!r.exists || r.phase !== 2 || r.pairToken !== NATIVE) continue;
      const poolId = poolIdFor(token, r.poolFee, r.tickSpacing);
      byPool.set(poolId, {
        token,
        curve: r.curve,
        poolFee: r.poolFee,
        tickSpacing: r.tickSpacing,
        graduationThreshold: r.graduationThreshold,
        sweptAt: r.sweptAt,
        phase: r.phase,
        pairToken: r.pairToken,
        poolId,
      });
    } catch {
      // A launch the factory will not describe is one we will not buy.
    }
  }
  return byPool;
}
