/**
 * Cumulative-entitlement merkle tree, matching `IPODistributor`.
 *
 * Leaf: keccak256(keccak256(abi.encode(account, token, cumulativeAmount)))
 *
 * The inner hash is doubled so a leaf can never be reinterpreted as an internal node, and pairs are
 * hashed in sorted order to match OpenZeppelin's `MerkleProof.verifyCalldata`.
 */
import { encodeAbiParameters, keccak256, concat, type Address, type Hex } from "viem";

export interface Entitlement {
  account: Address;
  token: Address;
  /** Total ever owed to this account for this token, across all epochs. */
  cumulativeAmount: bigint;
}

export function leafHash(e: Entitlement): Hex {
  const encoded = encodeAbiParameters(
    [{ type: "address" }, { type: "address" }, { type: "uint256" }],
    [e.account, e.token, e.cumulativeAmount]
  );
  return keccak256(keccak256(encoded));
}

/** Hash a pair in sorted order, as OpenZeppelin's commutative hash does. */
function hashPair(a: Hex, b: Hex): Hex {
  return BigInt(a) < BigInt(b) ? keccak256(concat([a, b])) : keccak256(concat([b, a]));
}

export class CumulativeMerkleTree {
  readonly leaves: Hex[];
  private readonly layers: Hex[][];
  private readonly index = new Map<Hex, number>();

  constructor(readonly entitlements: Entitlement[]) {
    if (entitlements.length === 0) throw new Error("cannot build a tree with no entitlements");

    // Sort by leaf hash so the tree is deterministic regardless of input order.
    this.leaves = entitlements.map(leafHash).sort((a, b) => (BigInt(a) < BigInt(b) ? -1 : 1));
    this.leaves.forEach((leaf, i) => this.index.set(leaf, i));

    this.layers = [this.leaves];
    let current = this.leaves;
    while (current.length > 1) {
      const next: Hex[] = [];
      for (let i = 0; i < current.length; i += 2) {
        // An odd node is promoted unchanged rather than paired with itself.
        next.push(i + 1 < current.length ? hashPair(current[i], current[i + 1]) : current[i]);
      }
      this.layers.push(next);
      current = next;
    }
  }

  get root(): Hex {
    return this.layers[this.layers.length - 1][0];
  }

  proof(e: Entitlement): Hex[] {
    const leaf = leafHash(e);
    let idx = this.index.get(leaf);
    if (idx === undefined) throw new Error(`entitlement not in tree: ${e.account}/${e.token}`);

    const out: Hex[] = [];
    for (let level = 0; level < this.layers.length - 1; level++) {
      const layer = this.layers[level];
      const pairIdx = idx % 2 === 0 ? idx + 1 : idx - 1;
      if (pairIdx < layer.length) out.push(layer[pairIdx]);
      idx = Math.floor(idx / 2);
    }
    return out;
  }
}

/**
 * Fold this epoch's purchases into the running cumulative totals.
 *
 * @param prior      Cumulative entitlements as of the last published root.
 * @param balances   IPO balance per holder at this epoch's snapshot.
 * @param purchases  Amount of each token bought this epoch and being handed out.
 * @returns          Updated cumulative entitlements, ready to build a new tree from.
 */
export function accrue(
  prior: Entitlement[],
  balances: Map<Address, bigint>,
  purchases: Map<Address, bigint>
): Entitlement[] {
  const total = [...balances.values()].reduce((a, b) => a + b, 0n);
  if (total === 0n) return prior;

  const key = (account: Address, token: Address) =>
    `${account.toLowerCase()}:${token.toLowerCase()}`;
  const cumulative = new Map<string, Entitlement>();
  for (const e of prior) {
    cumulative.set(key(e.account, e.token), { ...e });
  }

  for (const [token, amount] of purchases) {
    if (amount === 0n) continue;
    for (const [account, balance] of balances) {
      if (balance === 0n) continue;
      const share = (amount * balance) / total;
      if (share === 0n) continue;
      const k = key(account, token);
      const existing = cumulative.get(k);
      if (existing) {
        existing.cumulativeAmount += share;
      } else {
        cumulative.set(k, { account, token, cumulativeAmount: share });
      }
    }
  }

  return [...cumulative.values()];
}
