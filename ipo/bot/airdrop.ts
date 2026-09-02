/**
 * Turns an hour's purchases into batches of direct transfers.
 *
 * Holders do nothing — no claim, no approval, no gas. The keeper snapshots IPO balances, splits
 * each coin pro rata, and pushes the transfers out in batches sized to fit a block.
 */
import type { Address } from "viem";

export interface TokenPlan {
  token: Address;
  recipients: Address[];
  amounts: bigint[];
  /** Sum of `amounts`; always <= the amount available for this token. */
  total: bigint;
  /** Holders whose pro-rata share fell below `minPayout` and were left out. */
  dusted: number;
  /** Available minus total — rounding and dusted shares, which roll into the next epoch. */
  remainder: bigint;
}

/**
 * Split each token's available balance across holders in proportion to their IPO.
 *
 * @param available Distributable amount per token. Pass the airdropper's `undistributed(token)`
 *        rather than just this epoch's purchase: that carries forward rounding dust and anything a
 *        previous run failed to deliver, so nothing is stranded.
 * @param balances IPO balance per entitled holder at the snapshot.
 * @param minPayout Shares below this are skipped. A long tail of dust holders would otherwise cost
 *        more in gas than the tokens are worth; their share rolls forward instead.
 */
export function planAirdrop(
  available: Map<Address, bigint>,
  balances: Map<Address, bigint>,
  minPayout = 0n
): TokenPlan[] {
  const totalIpo = [...balances.values()].reduce((a, b) => a + b, 0n);
  if (totalIpo === 0n) return [];

  const plans: TokenPlan[] = [];
  for (const [token, amount] of available) {
    if (amount === 0n) continue;

    const recipients: Address[] = [];
    const amounts: bigint[] = [];
    let total = 0n;
    let dusted = 0;

    for (const [holder, balance] of balances) {
      const share = (amount * balance) / totalIpo;
      if (share < minPayout || share === 0n) {
        dusted++;
        continue;
      }
      recipients.push(holder);
      amounts.push(share);
      total += share;
    }

    // A token whose every share rounds away is not worth a transaction this hour.
    if (recipients.length === 0) continue;

    plans.push({ token, recipients, amounts, total, dusted, remainder: amount - total });
  }
  return plans;
}

export interface Batch {
  token: Address;
  batchIndex: number;
  recipients: Address[];
  amounts: bigint[];
  total: bigint;
}

/** Chop a plan into transaction-sized pieces. */
export function toBatches(plan: TokenPlan, batchSize: number): Batch[] {
  const out: Batch[] = [];
  for (let i = 0; i < plan.recipients.length; i += batchSize) {
    const recipients = plan.recipients.slice(i, i + batchSize);
    const amounts = plan.amounts.slice(i, i + batchSize);
    out.push({
      token: plan.token,
      batchIndex: out.length,
      recipients,
      amounts,
      total: amounts.reduce((a, b) => a + b, 0n),
    });
  }
  return out;
}
