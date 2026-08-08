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


def _discover(client, from_block: int, to_block: int) -> tuple[list[dict], int]:
    """Raw launches across every configured pad, tagged by pad name."""
    from .pons_v2 import decode_token_launched
    from .uniswap_v2 import decode_pair_created
    out: list[dict] = []
    failures = 0

    logs, f = chunked_logs(client, address=rcfg.PONS_V2["factory"],
                           topics=[rcfg.EVT_PONS_V2_TOKEN_LAUNCHED],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        d = decode_token_launched(lg)
        out.append({"pad": "pons-v2", "token": d["token"], "pool": d["curve"],
                    "quote_token": d["pair_token"], "block": int(lg["blockNumber"], 16)})

    weth = rcfg.CONTRACTS["weth"].lower()
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

    logs, f = chunked_logs(client, address=rcfg.LAUNCHPADS["pons"]["address"],
                           topics=[rcfg.EVT_PONS_TOKEN_LAUNCHED],
                           from_block=from_block, to_block=to_block)
    failures += f
    for lg in logs:
        # v1: topic1 token, topic2 deployer (NOT the pool); pool is data word 1.
        data = lg["data"][2:]
        out.append({"pad": "pons-v1", "token": "0x" + lg["topics"][1][26:],
                    "pool": "0x" + data[64:128][24:],
                    "quote_token": rcfg.CONTRACTS["weth"],
                    "block": int(lg["blockNumber"], 16)})
    return out, failures


def _resolve(client, hit: dict) -> V.Venue | None:
    try:
        if hit["pad"] == "pons-v2":
            return V.from_pons_v2(client, token=hit["token"], curve=hit["pool"],
                                  quote_token=hit["quote_token"])
        if hit["pad"] == "flap":
            return V.from_flap(client, token=hit["token"], pair=hit["pool"])
        return V.from_v2_pair(client, token=hit["token"], pair=hit["pool"],
                              quote_token=hit["quote_token"], kind=V.PONS_V1)
    except Exception:
        return None


def sweep(client, *, from_block: int, to_block: int, quote_price_usd: float,
          bankroll_usd: float | None = None, workers: int = 8) -> dict:
    """Discover -> resolve -> gate -> size, across every pad in one pass.

    Rows come back for BLOCKED tokens too, each carrying its reason. A scanner
    that shows only what passed makes a broken gate look like a quiet market.
    """
    hits, failures = _discover(client, from_block, to_block)
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
            rows.append({**hit, **plan,
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


def format_sweep(result: dict, *, limit: int = 25) -> str:
    """Human-readable sweep, actionable rows first."""
    lines = ["", "%-9s %-12s %-7s %-11s %-7s %-9s %-7s %s"
             % ("PAD", "TOKEN", "QUOTE", "MCAP", "TAX", "SIZE", "RTRIP", "VERDICT")]
    lines.append("-" * 100)
    for r in result["rows"][:limit]:
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
    lines.append("%d launches over %d blocks | %d tradeable | %d blocked%s"
                 % (result["found"], result["scanned_blocks"], result["tradeable"],
                    result["found"] - result["tradeable"],
                    "" if not result["failed_chunks"]
                    else "  ** %d LOG CHUNKS FAILED — counts are incomplete **"
                         % result["failed_chunks"]))
    return "\n".join(lines)
