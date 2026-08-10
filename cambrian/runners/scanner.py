"""One sweep across every launchpad: discover, price, gate, size, rank.

This is the thing you actually run. It answers, in one pass and for every token
launched in a window, the only questions that matter before acting: what is it,
what is it worth, will the tax eat me, how much should I buy, and what will the
round trip cost.

Two operational details that are easy to get wrong and expensive to miss:

**The RPC caps `eth_getLogs` at ~2,048 blocks** and returns an ERROR rather than
a truncated result. A scanner that swallows that error reports zero launches,
which is indistinguishable from a quiet market — this is exactly how pons and
flap sat unwatched. `chunked_logs` splits the range and re-raises nothing
silently: a failed chunk is counted and surfaced.

**Never pass a wide range with `toBlock="latest"`.** The range grows as blocks
arrive and trips the cap mid-run. Every scan here pins an explicit end block.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from . import config as rcfg
from . import sizing as S
from . import stock_tokens as ST
from . import venues as V

# Stay under the node's ~2048-block ceiling with room for the chain to advance.
MAX_LOG_SPAN = int(os.getenv("RH_MAX_LOG_SPAN", "1500"))


def chunked_logs(client, *, address: str, topics: list, from_block: int,
                 to_block: int, span: int = MAX_LOG_SPAN) -> tuple[list, int]:
    """Logs over an arbitrary range, split to respect the node's cap.

    Returns (logs, failed_chunks). The failure count is returned rather than
    swallowed so a partial scan can never masquerade as a quiet market.
    """
    logs, failed = [], 0
    lo = from_block
    while lo <= to_block:
        hi = min(lo + span, to_block)
        try:
            got = client.get_logs(address=address, topics=topics,
                                  from_block=hex(lo), to_block=hex(hi))
            logs.extend(got or [])
        except Exception:
            failed += 1
        lo = hi + 1
    return logs, failed


def _provenance(client, from_block: int, to_block: int) -> tuple[dict, int]:
    """Tokens that PROVABLY came from a launchpad, by pad.

    This is the check the desk was missing, and its absence is how a Uniswap
    honeypot got bought from a "verified pad": pools.trade launches were being
    recognised as "a hookless v4 pool paired with WETH", and **anyone can create
    one of those**. The pad label was an inference dressed up as provenance.

    `config.py` already states the invariant — only the launcher can emit logs at
    the launcher's own address, so membership in `TokenCreated` cannot be forged
    the way an address suffix or a pool shape can. `UNI_LAUNCHER_ADDRESSES` and
    `FLAP_PORTAL` were both defined for exactly this and read by nothing.

    Scanned over a WIDER window than the sweep: a pool can be initialised well
    after its token was created, and a launch that scrolled out of the discovery
    window is still a real launch.
    """
    span = int(os.getenv("RH_PROVENANCE_BLOCKS", "6000"))
    lo = max(from_block - span, 0)
    verified: dict = {"pools-trade": set(), "flap": set()}
    failures = 0

    for addr in rcfg.UNI_LAUNCHER_ADDRESSES:
        logs, f = chunked_logs(client, address=addr,
                               topics=[rcfg.EVT_TOKEN_CREATED],
                               from_block=lo, to_block=to_block)
        failures += f
        for lg in logs:
            # TokenCreated(address) — indexed or not depending on generation, so
            # take the topic when present and fall back to the first data word.
            tops = lg.get("topics") or []
            raw = tops[1] if len(tops) > 1 else (lg.get("data") or "0x")[2:66]
            if raw and len(raw) >= 40:
                verified["pools-trade"].add("0x" + raw[-40:].lower())

    logs, f = chunked_logs(client, address=rcfg.FLAP_PORTAL,
                           topics=[rcfg.EVT_FLAP_LAUNCH],
                           from_block=lo, to_block=to_block)
    failures += f
    for lg in logs:
        # TokenCreated(uint256 ts, address creator, uint256 nonce, address token,
        #              string, string, string) — all unindexed; token is word 3.
        data = (lg.get("data") or "0x")[2:]
        if len(data) >= 256:
            verified["flap"].add("0x" + data[192:256][-40:].lower())
    return verified, failures


def _discover(client, from_block: int, to_block: int) -> tuple[list[dict], int]:
    """Raw launches across every configured pad, tagged by pad name."""
    from .pons_v2 import decode_token_launched
    from .uniswap_v2 import decode_pair_created
    out: list[dict] = []
    failures = 0

    weth = rcfg.CONTRACTS["weth"].lower()

    logs, f = chunked_logs(client, address=rcfg.PONS_V2["factory"],
                           topics=[rcfg.EVT_PONS_V2_TOKEN_LAUNCHED],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        d = decode_token_launched(lg)
        out.append({"pad": "pons-v2", "token": d["token"], "pool": d["curve"],
                    "quote_token": d["pair_token"], "block": int(lg["blockNumber"], 16)})

    logs, f = chunked_logs(client, address=rcfg.V2_FACTORY,
                           topics=[rcfg.EVT_V2_PAIR_CREATED],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        d = decode_pair_created(lg)
        t0, t1 = d["token0"].lower(), d["token1"].lower()
        if weth not in (t0, t1):
            continue
        out.append({"pad": "flap", "token": d["token1"] if t0 == weth else d["token0"],
                    "pool": d["pair"], "quote_token": rcfg.CONTRACTS["weth"],
                    "block": int(lg["blockNumber"], 16)})

    # pools.trade: launches settle into HOOKLESS v4 pools. Discovering them off
    # the PoolManager's Initialize rather than the launchers' TokenCreated gets
    # the PoolId in the same read — v4 pools have no address, so TokenCreated
    # alone would leave nothing to price against. Hooked pools belong to other
    # pads and are filtered out here.
    from .uniswap_v4 import INITIALIZE_TOPIC0, decode_initialize
    native = "0x" + "0" * 40
    logs, f = chunked_logs(client, address=rcfg.CONTRACTS["pool_manager"],
                           topics=[INITIALIZE_TOPIC0],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        d = decode_initialize(lg)
        if d["hooks"].lower() != native:
            continue                      # hooked pool -> a different pad
        c0, c1 = d["currency0"].lower(), d["currency1"].lower()
        quote = None
        if c0 in (weth, native):
            quote, token, q_is_0 = d["currency0"], d["currency1"], True
        elif c1 in (weth, native):
            quote, token, q_is_0 = d["currency1"], d["currency0"], False
        if quote is None:
            continue
        out.append({"pad": "pools-trade", "token": token, "pool": d["pool_id"],
                    "quote_token": quote, "quote_is_currency0": q_is_0,
                    "block": int(lg["blockNumber"], 16)})

    logs, f = chunked_logs(client, address=rcfg.LAUNCHPADS["pons"]["address"],
                           topics=[rcfg.EVT_PONS_TOKEN_LAUNCHED],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        # v1: topic1 token, topic2 deployer (NOT the pool); pool is data word 1.
        #
        # Word 5 is `restrictionsEndBlock`, and reading it is the difference
        # between a working desk and one that buys tokens it cannot sell. Pons
        # holds launch restrictions for a window after deployment — an
        # anti-sniper measure, not a scam — and inside that window a sell is
        # refused. A buy at t=0 therefore looks EXACTLY like a honeypot: the
        # token is real, the pad is legitimate, the tax is 0%, and you cannot
        # get out. This field was documented in config and used nowhere, and
        # that omission is what got the first live position stuck.
        data = lg["data"][2:]

        def _word(i, d=data):
            try:
                return int(d[i * 64:(i + 1) * 64], 16)
            except ValueError:
                return 0

        out.append({"pad": "pons-v1", "token": "0x" + lg["topics"][1][26:],
                    "pool": "0x" + data[64:128][24:],
                    "quote_token": rcfg.CONTRACTS["weth"],
                    "restrictions_end_block": _word(5),
                    "initial_buy_wei": _word(6),
                    "block": int(lg["blockNumber"], 16)})
    return out, failures


def _resolve(client, hit: dict) -> V.Venue | None:
    try:
        if hit["pad"] == "pons-v2":
            return V.from_pons_v2(client, token=hit["token"], curve=hit["pool"],
                                  quote_token=hit["quote_token"])
        if hit["pad"] == "flap":
            return V.from_flap(client, token=hit["token"], pair=hit["pool"])
        if hit["pad"] == "pools-trade":
            return V.from_v4_pool(client, token=hit["token"], pool_id=hit["pool"],
                                  quote_token=hit["quote_token"],
                                  quote_is_currency0=hit["quote_is_currency0"])
        # Pons v1 launches into a Uniswap **V3** pool. Reading it with the V2
        # reader returns nothing, which failed 11 of 11 live launches as "no
        # price" — the gate was working, the resolver was not.
        return V.from_v3_pool(client, token=hit["token"], pool=hit["pool"],
                              quote_token=hit["quote_token"], kind=V.PONS_V1)
    except Exception:
        return None


def _restricted(hit: dict, latest_block: int) -> int:
    """Blocks remaining before this launch can be SOLD, 0 if unrestricted.

    Pons enforces a post-launch restriction window. Inside it a sell reverts, so
    buying at t=0 buys a position you cannot exit — indistinguishable from a
    honeypot from the outside, and the reason the first live scanner entry got
    stuck.
    """
    end = int(hit.get("restrictions_end_block") or 0)
    return max(end - latest_block, 0) if end else 0


def sweep(client, *, from_block: int, to_block: int, quote_price_usd: float,
          bankroll_usd: float | None = None, workers: int = 8) -> dict:
    """Discover -> resolve -> gate -> size, across every pad in one pass.

    Rows come back for BLOCKED tokens too, each carrying its reason. A scanner
    that shows only what passed makes a broken gate look like a quiet market.
    """
    hits, failures = _discover(client, from_block, to_block)
    verified, vf = _provenance(client, from_block, to_block)
    failures += vf
    # A pad label inferred from pool shape is not provenance. Only pons emits its
    # own launch from its own factory in the same read; pools.trade and flap are
    # discovered by pool shape, which anyone can imitate, so both must be
    # confirmed against a launcher's own event before they are tradeable.
    for h in hits:
        pad = h.get("pad")
        if pad in verified:
            h["verified"] = (h.get("token") or "").lower() in verified[pad]
        else:
            h["verified"] = True          # pons v1/v2: emitted by its own factory
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for hit, venue in zip(hits, pool.map(lambda h: _resolve(client, h), hits)):
            if venue is None:
                rows.append({**hit, "ok": False, "reasons": ["unresolvable"],
                             "market_cap_usd": None, "size_usd": None})
                continue
            # Price the QUOTE asset, not ETH. ~60% of Pons v2 launches are
            # denominated in a tokenized stock; valuing a SPY-quoted launch at
            # the ETH price misstates market cap and therefore the ticket.
            qp = ST.quote_price_usd(venue.quote_token, weth_usd=quote_price_usd,
                                    usdg=rcfg.CONTRACTS.get("usdg"),
                                    weth=rcfg.CONTRACTS.get("weth"))
            if qp is None:
                rows.append({**hit, "ok": False,
                             "reasons": ["quote asset %s not priceable"
                                         % (venue.quote_token or "?")[:10]],
                             "market_cap_usd": None, "size_usd": None,
                             "tax_bps": venue.tax_bps})
                continue
            plan = S.plan_entry(venue, quote_price_usd=qp,
                                bankroll_usd=bankroll_usd)
            if not hit.get("verified", True):
                rows.append({**hit, "ok": False, "market_cap_usd": None,
                             "size_usd": None, "tax_bps": venue.tax_bps,
                             "reasons": ["no launcher provenance — pool exists but "
                                         "no %s launch event names this token"
                                         % (hit.get("pad") or "pad")]})
                continue
            blocks_left = _restricted(hit, to_block)
            if blocks_left:
                # ~100ms blocks, so this is also roughly the wait in tenths of a
                # second. Blocked rather than skipped: it becomes tradeable on
                # its own, and the next sweep will pick it up.
                plan = {**plan, "ok": False,
                        "reasons": ["sells restricted for %d more blocks (~%.0fs)"
                                    % (blocks_left, blocks_left * 0.1)]
                                   + list(plan.get("reasons") or [])}
            rows.append({**hit, **plan, "blocks_restricted": blocks_left,
                         "quote_symbol": ST.symbol_for(venue.quote_token),
                         "quote_price_usd": qp,
                         "stock_paired": V.is_stock_paired(venue)})
    rows.sort(key=lambda r: (not r.get("ok"), -(r.get("market_cap_usd") or 0)))
    return {
        "rows": rows,
        "scanned_blocks": max(to_block - from_block, 0),
        "found": len(hits),
        "tradeable": sum(1 for r in rows if r.get("ok")),
        "failed_chunks": failures,
    }


def _quote_label(row: dict) -> str:
    """Ticker for a stock pair, else the well-known name, else a short address."""
    if row.get("quote_symbol"):
        return row["quote_symbol"][:6]
    q = (row.get("quote_token") or "").lower()
    if not q or q == "0x" + "0" * 40:
        return "ETH"
    if q == rcfg.CONTRACTS["weth"].lower():
        return "WETH"
    if q == (rcfg.CONTRACTS.get("usdg") or "").lower():
        return "USDG"
    return q[2:8]


MAX_TAX_FEE_FARM_BPS = 1000     # the cap; nobody launching a real token picks it


def fee_farm_share(result: dict) -> tuple[int, int]:
    """(tokens at the maximum tax, total tokens with a readable tax).

    Owner's read, and the data agrees: flap tokens above the tax gate are mostly
    fee farms rather than launches. Measured over 4,500 blocks, 36 of 53 flap
    tokens sat at EXACTLY 10.0% — the maximum. A real launch does not choose the
    cap, because the cap makes the token unsellable at a profit.
    """
    taxed = [r.get("tax_bps") for r in result["rows"] if r.get("tax_bps") is not None]
    return sum(1 for t in taxed if t >= MAX_TAX_FEE_FARM_BPS), len(taxed)


def format_sweep(result: dict, *, limit: int = 25, show_blocked: int = 5) -> str:
    """Human-readable sweep, actionable rows first.

    Blocked rows are summarised rather than listed in full. They must not vanish —
    a scanner showing only passes makes a broken gate look like a quiet market —
    but at ~68% of flap sitting at the maximum tax, listing every one buries the
    handful of rows worth acting on. So: all passing rows, a few blocked ones, and
    a counted breakdown of the rest.
    """
    lines = ["", "%-9s %-12s %-7s %-11s %-7s %-9s %-7s %s"
             % ("PAD", "TOKEN", "QUOTE", "MCAP", "TAX", "SIZE", "RTRIP", "VERDICT")]
    lines.append("-" * 100)
    passing = [r for r in result["rows"] if r.get("ok")]
    blocked = [r for r in result["rows"] if not r.get("ok")]
    for r in (passing[:limit] + blocked[:show_blocked]):
        mc, size = r.get("market_cap_usd"), r.get("size_usd")
        rt = r.get("round_trip") or {}
        tax = r.get("tax_bps")
        lines.append("%-9s %-12s %-7s %-11s %-7s %-9s %-7s %s" % (
            r.get("pad", "?"), (r.get("token") or "?")[:10],
            _quote_label(r),
            ("$%.0f" % mc) if mc else "?",
            ("%.1f%%" % (tax / 100)) if tax is not None else "n/a",
            ("$%.2f" % size) if size else "-",
            ("%.0f%%" % rt["returned_pct"]) if rt.get("returned_pct") else "-",
            "OK" if r.get("ok") else "; ".join(r.get("reasons") or ["blocked"])))
    lines.append("-" * 100)
    if len(blocked) > show_blocked:
        import collections as _c
        why = _c.Counter()
        for r in blocked:
            reason = (r.get("reasons") or ["blocked"])[0]
            why["tax" if reason.startswith("tax") else reason] += 1
        lines.append("  ...%d more blocked: %s"
                     % (len(blocked) - show_blocked,
                        ", ".join("%s x%d" % (k, v) for k, v in why.most_common(4))))
    farms, taxed = fee_farm_share(result)
    if taxed:
        lines.append("  %d of %d taxed tokens sit at the %g%% maximum — fee farms, not launches"
                     % (farms, taxed, MAX_TAX_FEE_FARM_BPS / 100))
    lines.append("%d launches over %d blocks | %d tradeable | %d blocked%s"
                 % (result["found"], result["scanned_blocks"], result["tradeable"],
                    result["found"] - result["tradeable"],
                    "" if not result["failed_chunks"]
                    else "  ** %d LOG CHUNKS FAILED — counts are incomplete **"
                         % result["failed_chunks"]))
    return "\n".join(lines)
