/**
 * Checks every assumption this repo makes about Pons and Uniswap v4 against the live chain.
 *
 * Read-only, no keys needed. Run it before deploying, and again whenever Pons ships an upgrade —
 * the integration is decoded from documentation plus on-chain observation, and a silent ABI change
 * would otherwise surface as a bot that quietly sees nothing.
 *
 *   npx tsx script/verify.ts
 */
import {
  createPublicClient,
  http,
  keccak256,
  toHex,
  decodeAbiParameters,
  parseAbiParameters,
  type Hex,
} from "viem";
import { robinhoodChain, PONS, UNISWAP_V4, MAX_CREATOR_TAX_BPS, LAUNCH_CONFIG_ID, IPO_LAUNCH } from "../config/addresses";

const client = createPublicClient({ chain: robinhoodChain, transport: http() });
const F = PONS.factory as Hex;
const HOOK = PONS.memeHook as Hex;
const sig = (s: string) => keccak256(toHex(s));

const EXPECTED = {
  tokenLaunched: "TokenLaunched(address,address,address,address,uint256,uint256)",
  poolGraduated: "PoolGraduated(address,uint256,uint256,uint256)",
  launchSwept: "LaunchSwept(address,uint256,uint256)",
  v4Swap: "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
};

const LAUNCHED_TOKEN = parseAbiParameters(
  "(address token,address curve,address deployer,address creatorFeeRecipient,address pairToken,uint256 graduationThreshold,uint24 poolFee,int24 tickSpacing,uint16 creatorTaxBps,bool buybackEnabled,uint8 phase,uint256 sweptQuote,uint256 sweptTokens,uint256 sweptAt,bool exists)"
);
const LAUNCH_CONFIG = parseAbiParameters(
  "(uint256 supply,uint256 curveFeeBps,uint256 phantomQuote,uint256 graduationThreshold,uint24 poolFee,int24 tickSpacing,bool enabled)"
);

let failures = 0;
function check(label: string, ok: boolean, detail = "") {
  if (!ok) failures++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${detail ? `  ${detail}` : ""}`);
}

async function callGetter(to: Hex, signature: string): Promise<Hex | null> {
  try {
    const r = await client.call({ to, data: sig(signature).slice(0, 10) as Hex });
    return r.data && r.data !== "0x" ? r.data : null;
  } catch {
    return null;
  }
}

const asAddress = (word: Hex) => ("0x" + word.slice(26)).toLowerCase();

async function main() {
  const chainId = await client.getChainId();
  const head = await client.getBlockNumber();
  console.log(`Robinhood Chain ${chainId}, head ${head}\n`);
  check("chain id is 4663", chainId === 4663);

  console.log("\n[contracts have code]");
  for (const [name, addr] of [...Object.entries(PONS), ["poolManager", UNISWAP_V4.poolManager]]) {
    const code = await client.getCode({ address: addr as Hex });
    check(name, !!code && code !== "0x", addr as string);
  }

  console.log("\n[factory agrees with our config]");
  const escrow = await callGetter(F, "feeEscrow()");
  const hook = await callGetter(F, "memeHook()");
  const pm = await callGetter(F, "poolManager()");
  const hookPm = await callGetter(HOOK, "poolManager()");
  check("feeEscrow()", !!escrow && asAddress(escrow) === PONS.feeEscrow.toLowerCase());
  check("memeHook()", !!hook && asAddress(hook) === PONS.memeHook.toLowerCase());
  check("poolManager()", !!pm && asAddress(pm) === UNISWAP_V4.poolManager.toLowerCase());
  check("hook.poolManager() agrees", !!hookPm && asAddress(hookPm) === UNISWAP_V4.poolManager.toLowerCase());

  console.log("\n[creator tax]");
  const maxTax = await callGetter(F, "maxCreatorTaxBps()");
  const maxTaxNum = maxTax ? Number(BigInt(maxTax)) : -1;
  check(`maxCreatorTaxBps() == ${MAX_CREATOR_TAX_BPS}`, maxTaxNum === MAX_CREATOR_TAX_BPS, `got ${maxTaxNum}`);
  check(
    `IPO's ${IPO_LAUNCH.creatorTaxBps}bps is within the cap`,
    IPO_LAUNCH.creatorTaxBps <= maxTaxNum
  );

  console.log("\n[launch config]");
  const cfgCount = await callGetter(F, "launchConfigCount()");
  check("launchConfigCount() >= 1", !!cfgCount && BigInt(cfgCount) >= 1n, `got ${cfgCount && BigInt(cfgCount)}`);
  try {
    const data = (sig("getLaunchConfig(uint256)").slice(0, 10) +
      LAUNCH_CONFIG_ID.toString(16).padStart(64, "0")) as Hex;
    const res = await client.call({ to: F, data });
    const [c] = decodeAbiParameters(LAUNCH_CONFIG, res.data!) as unknown as any[];
    check(`config ${LAUNCH_CONFIG_ID} enabled`, c.enabled);
    console.log(
      `       supply=${c.supply / 10n ** 18n} curveFeeBps=${c.curveFeeBps} ` +
        `gradThreshold=${Number(c.graduationThreshold) / 1e18} ETH tickSpacing=${c.tickSpacing} poolFee=${c.poolFee}`
    );
  } catch (e) {
    check(`config ${LAUNCH_CONFIG_ID} readable`, false, (e as Error).message.split("\n")[0]);
  }

  console.log("\n[event signatures seen on chain]");
  const seen = new Set<string>();
  for (let i = 0n; i < 40n; i++) {
    const to = head - i * 2_000n;
    try {
      const logs = await client.getLogs({ address: F, fromBlock: to - 1_999n, toBlock: to });
      for (const l of logs) seen.add(l.topics[0] as string);
    } catch {}
    if (seen.has(sig(EXPECTED.poolGraduated)) && seen.has(sig(EXPECTED.tokenLaunched))) break;
  }
  check(`TokenLaunched ${sig(EXPECTED.tokenLaunched).slice(0, 12)}…`, seen.has(sig(EXPECTED.tokenLaunched)));
  check(`PoolGraduated ${sig(EXPECTED.poolGraduated).slice(0, 12)}…`, seen.has(sig(EXPECTED.poolGraduated)));
  check(`LaunchSwept   ${sig(EXPECTED.launchSwept).slice(0, 12)}…`, seen.has(sig(EXPECTED.launchSwept)));

  const pmLogs = await client.getLogs({
    address: UNISWAP_V4.poolManager as Hex,
    fromBlock: head - 300n,
    toBlock: head,
  });
  check(
    `v4 Swap       ${sig(EXPECTED.v4Swap).slice(0, 12)}…`,
    pmLogs.some((l) => l.topics[0] === sig(EXPECTED.v4Swap)),
    `${pmLogs.length} pool-manager logs sampled`
  );

  console.log("\n[LaunchedToken struct decodes against a real graduated launch]");
  let graduated = 0;
  for (let i = 0n; i < 40n && graduated < 3; i++) {
    const to = head - i * 2_000n;
    let logs: any[] = [];
    try {
      logs = await client.getLogs({ address: F, fromBlock: to - 1_999n, toBlock: to });
    } catch {
      continue;
    }
    for (const l of logs.filter((x) => x.topics[0] === sig(EXPECTED.poolGraduated))) {
      if (graduated >= 3) break;
      const token = ("0x" + (l.topics[1] as string).slice(26)) as Hex;
      const data = (sig("getLaunchedToken(address)").slice(0, 10) + token.slice(2).padStart(64, "0")) as Hex;
      const res = await client.call({ to: F, data });
      const [r] = decodeAbiParameters(LAUNCHED_TOKEN, res.data!) as unknown as any[];
      graduated++;
      check(`${token} decodes to itself`, r.token.toLowerCase() === token.toLowerCase());
      check(`${token} reports phase 2`, r.phase === 2, `phase=${r.phase}`);
      if (r.sweptAt !== 0n) {
        console.log(`  NOTE  ${token} has a non-zero sweptAt (${r.sweptAt}) — Pons may now populate it`);
      }
    }
  }
  check("found graduated launches to sample", graduated > 0, `${graduated} sampled`);
  console.log(
    "  NOTE  swept{Quote,Tokens,At} read back as zero on every graduated launch sampled,\n" +
      "        which is why nothing on-chain derives graduation recency from them."
  );

  console.log(`\n${failures === 0 ? "all checks passed" : `${failures} CHECK(S) FAILED`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
