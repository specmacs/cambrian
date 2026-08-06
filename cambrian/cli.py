"""Command-line entry point.

Everything here runs today, in DRY_RUN, with no live addresses:

    python -m cambrian status
    python -m cambrian evaluate-degen path/to/candidate.json
    python -m cambrian evaluate-lp   path/to/pool.json
    python -m cambrian tail -n 20

`status` reports what config is blocking each desk and (if RPC is set) whether
the chain is reachable and is the chain we expect. The `evaluate-*` commands run
a real decision through the real desk logic against a JSON fixture, journal the
result, and — with `--execute` — route an approved decision through the dry-run
executor so you can watch the whole path end to end.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import requests

from . import config
from .decision import Decision
from .desks import degen as degen_desk
from .desks import lp as lp_desk
from .execution import Order, executor_for, preflight_live_blockers
from .feeds.context import build_lp_context
from .feeds.earnings import ManualEarningsCalendar
from .journal import Journal
from .snapshots import DegenCandidate, DegenState, LPPool, LPState

# ANSI, kept trivial so output is readable but never depends on color.
_GREEN, _RED, _YEL, _DIM, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _print_decision(d: Decision) -> None:
    color = _GREEN if d.approved else _RED
    verdict = "APPROVED" if d.approved else "REJECTED"
    print(f"{color}{verdict}{_RST}  [{d.desk}] {d.subject}")
    for note in d.notes:
        print(f"  {_DIM}· {note}{_RST}")
    for reason in d.reasons:
        print(f"  {_RED}✗ {reason}{_RST}")


# --- status -----------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    print(f"{config.CHAIN_NAME} (chain {config.CHAIN_ID})")
    mode = f"{_YEL}DRY_RUN{_RST}" if config.DRY_RUN else f"{_RED}LIVE{_RST}"
    print(f"mode: {mode}   journal: {config.ORDER_JOURNAL}")

    gaps = config.missing_config()
    if gaps:
        print(f"\n{_RED}config gaps (these fail the relevant desk closed):{_RST}")
        for g in gaps:
            print(f"  ✗ {g}")
    else:
        print(f"\n{_GREEN}config complete{_RST}")

    print("\ndesks:")
    print(f"  degen: {'HALTED' if config.DEGEN.halted else 'live'}")
    print(f"  lp:    {'HALTED' if config.LP.halted else 'live'}")

    blockers = preflight_live_blockers()
    print("\nlive-trading preflight:")
    if blockers:
        for b in blockers:
            print(f"  {_YEL}·{_RST} {b}")
    else:
        print(f"  {_GREEN}clear (but live execution itself is unimplemented){_RST}")

    if config.RPC_URL:
        print("\nchain:")
        try:
            from .chain import ChainClient
            client = ChainClient()
            cid = client.chain_id()
            ok = cid == config.CHAIN_ID
            mark = _GREEN + "✓" + _RST if ok else _RED + "✗" + _RST
            print(f"  {mark} reachable, chainId={cid} "
                  f"(expected {config.CHAIN_ID})")
            print(f"    block {client.block_number()}")
        except Exception as exc:
            print(f"  {_RED}✗ RPC error: {exc}{_RST}")
    else:
        print(f"\nchain: {_DIM}RPC_URL unset, skipping reachability check{_RST}")

    return 1 if gaps else 0


# --- evaluate degen ---------------------------------------------------------

def cmd_evaluate_degen(args: argparse.Namespace) -> int:
    fx = _load(args.fixture)
    candidate = DegenCandidate(**fx["candidate"])
    state = DegenState(**fx["state"])
    decision = degen_desk.evaluate(
        candidate,
        state,
        trusted_factories=fx.get("trusted_factories"),
        reviewed_hooks=fx.get("reviewed_hooks"),
    )
    return _finish(decision, args, wallet=config.DEGEN_WALLET,
                   order_kind="buy", notional=candidate.notional_usd)


# --- evaluate lp ------------------------------------------------------------

def cmd_evaluate_lp(args: argparse.Namespace) -> int:
    fx = _load(args.fixture)
    pool = LPPool(**fx["pool"])
    state = LPState(**fx["state"])
    now_utc = (
        datetime.fromisoformat(fx["now_utc"])
        if fx.get("now_utc")
        else datetime.now(timezone.utc)
    )
    earnings = ManualEarningsCalendar(fx.get("earnings"))
    ctx = build_lp_context(fx["ticker"], now_utc, earnings)
    decision = lp_desk.evaluate(
        pool,
        ctx,
        state,
        stock_tokens=fx.get("stock_tokens"),
        quote_tokens=fx.get("quote_tokens"),
    )
    return _finish(decision, args, wallet=config.LP_WALLET,
                   order_kind="add_liquidity", notional=pool.position_usd)


def _finish(decision: Decision, args: argparse.Namespace, *, wallet: str,
            order_kind: str, notional: float) -> int:
    journal = Journal(config.ORDER_JOURNAL)
    journal.record("decision", decision.as_dict())
    _print_decision(decision)

    if decision.approved and args.execute:
        executor = executor_for(journal, wallet)
        receipt = executor.submit(Order(
            desk=decision.desk,
            kind=order_kind,
            subject=decision.subject,
            notional_usd=notional,
            wallet=wallet or "<unset>",
        ))
        print(f"  {_DIM}→ {receipt.detail}{_RST}")

    return 0 if decision.approved else 2


# --- tail -------------------------------------------------------------------

def cmd_tail(args: argparse.Namespace) -> int:
    journal = Journal(config.ORDER_JOURNAL)
    for rec in journal.tail(args.n):
        print(json.dumps(rec, sort_keys=True))
    return 0


# --- review-hook ------------------------------------------------------------

def cmd_probe_cambrian(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianClient, CambrianError
    try:
        client = CambrianClient()
        chains = client.chains()
        print(f"{_GREEN}auth OK{_RST} — chains indexed: "
              + ", ".join(f"{c.get('name')}({c.get('id')})" for c in chains))
        if args.path:
            print(f"\n{_DIM}raw {args.path}:{_RST}")
            print(json.dumps(client.get(args.path), indent=2)[:3000])
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    return 0


# --- Base pivot: scan / evaluate / allocate ---------------------------------

def _load_base_pools(client, dexes=None):
    """Fetch + normalize pools across Base DEXes. Returns (pools, errors)."""
    from . import base_config
    from .base.pools import normalize_pool
    endpoints = base_config.BASE_POOL_ENDPOINTS
    if dexes:
        endpoints = {k: v for k, v in endpoints.items() if k in dexes}
    pools, errors = [], []
    for dex, endpoint in endpoints.items():
        try:
            rows = client.query_all(endpoint)  # full set, all pages
            pools.extend(normalize_pool(r, dex) for r in rows)
        except Exception as exc:  # one DEX failing shouldn't kill the scan
            errors.append(f"{dex}: {exc}")
    return pools, errors


def cmd_scan_base(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from . import base_config
    from .base.pools import scan
    try:
        client = CambrianClient()
        if args.raw:
            # Show the true column names for one DEX so aliases can be confirmed.
            endpoint = next(iter(base_config.BASE_POOL_ENDPOINTS.values()))
            rows = client.pools(endpoint)
            print(f"{_DIM}columns from {endpoint}:{_RST}")
            print(", ".join((rows[0].keys() if rows else [])) or "(no rows)")
            if rows:
                print(json.dumps(rows[0], indent=2)[:2000])
            return 0
        pools, errors = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1

    ranked = scan(pools, min_tvl_usd=args.min_tvl, max_results=args.n)
    print(f"Base LP yield — {len(pools)} pools scanned, "
          f"{len(ranked)} clear TVL>={_usd(args.min_tvl)}\n")
    print(f"  {'DEX':<14} {'PAIR':<16} {'TVL':>13} {'ALL-IN APR':>11} {'FEES ONLY':>10}")
    for p in ranked:
        print(f"  {p.dex:<14} {p.label[:16]:<16} {_usd(p.tvl_usd):>13} "
              f"{_pct(p.fee_apr):>11} {_pct(p.swap_fee_apr):>10}")
    for e in errors:
        print(f"  {_YEL}· {e}{_RST}")
    if pools and not ranked:
        print(f"\n{_YEL}Pools loaded but none ranked — likely a column-name "
              f"mismatch. Run `scan-base --raw` and check base/pools.py ALIASES.{_RST}")
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    """The whole yield field on Base: LP pools + lending, ranked by APY."""
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.lending import normalize_market
    from .base.opportunities import build_opportunities
    try:
        client = CambrianClient()
        pools, errors = _load_base_pools(client)
        markets = [normalize_market(r) for r in client.lending_overview()]
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1

    opps = build_opportunities(pools, markets, min_tvl_usd=args.min_tvl,
                               max_results=args.n)
    print(f"Best yield across Base — {len(pools)} pools + {len(markets)} lending "
          f"markets, top {len(opps)} above {_usd(args.min_tvl)} TVL\n")
    print(f"  {'#':>2} {'KIND':<5}{'VENUE':<14}{'ASSET/PAIR':<16}{'APR':>9}{'TVL':>14}  DETAIL")
    for i, o in enumerate(opps, 1):
        kc = _GREEN if o.kind == "LEND" else _YEL
        print(f"  {i:>2} {kc}{o.kind:<5}{_RST}{o.venue:<14}{o.label[:16]:<16}"
              f"{_pct(o.apr):>9}{_usd(o.tvl_usd):>14}  {_DIM}{o.detail}{_RST}")
    for e in errors:
        print(f"  {_YEL}· {e}{_RST}")
    return 0


def cmd_evaluate_base_lp(args: argparse.Namespace) -> int:
    """Scan Base pools, then run each through the Base LP desk (dry-run)."""
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.pools import scan
    from .desks import base_lp
    from .desks.base_lp import BaseLPState
    try:
        client = CambrianClient()
        pools, _ = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    ranked = scan(pools, min_tvl_usd=config_base_min_tvl(), max_results=args.n)
    state = BaseLPState(total_deployed_usd=0.0, open_positions=0)
    journal = Journal(config.ORDER_JOURNAL)
    approved = 0
    for p in ranked:
        d = base_lp.evaluate(p, args.position, state)
        journal.record("decision", d.as_dict())
        _print_decision(d)
        approved += d.approved
    print(f"\n{approved}/{len(ranked)} approved at {_usd(args.position)}/position")
    return 0


def cmd_allocate(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.pools import pools_holding
    from .base.lending import normalize_market, best_supply
    from .base.allocate import recommend
    from . import base_config
    try:
        client = CambrianClient()
        pools, _ = _load_base_pools(client)
        markets = [normalize_market(r) for r in client.lending_overview()]
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1

    holding = pools_holding(pools, args.token)
    lp_apr = max((p.fee_apr for p in holding if p.fee_apr is not None), default=None)
    lend = best_supply(markets, address=args.token)
    lend_apr = lend.supply_apr if lend else None

    alloc = recommend(args.token, lp_apr, lend_apr,
                      stress_price_ratio=base_config.BASE_LP.stress_price_ratio)
    color = _GREEN if alloc.choice == "LP" else _YEL
    print(f"{color}{alloc.choice}{_RST}  {args.token}")
    print(f"  LP fee APR:   {_pct(alloc.lp_apr)}  (net {_pct(alloc.lp_net_apr)} "
          f"after {_pct(alloc.il_haircut)} stress-IL)")
    print(f"  Lending APR:  {_pct(alloc.lend_apr)}")
    print(f"  {alloc.reason}")
    return 0


def _print_portfolio(pf):
    print(f"Deposit {_usd(pf.capital_usd)} → "
          f"deploy {_usd(pf.deployed_usd)}, reserve {_usd(pf.reserve_usd)}, "
          f"blended risk-adj APR {_pct(pf.blended_apr)}\n")
    print(f"  {'SLEEVE':<10}{'DEX':<14}{'PAIR':<16}{'USD':>10}{'WT':>7}{'APR*':>8}")
    for p in pf.positions:
        print(f"  {p.sleeve:<10}{p.dex:<14}{p.label[:16]:<16}"
              f"{_usd(p.usd):>10}{p.weight:>7.1%}{_pct(p.expected_apr):>8}")
    print(f"\n  {_DIM}*APR = risk-adjusted net (emissions discounted, IL "
          f"subtracted){_RST}")
    for p in pf.positions:
        print(f"  {_DIM}· {p.label}: {p.reason}{_RST}")


def cmd_plan(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.score import score_all
    from .base.portfolio import build_portfolio
    try:
        client = CambrianClient()
        pools, errors = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    pf = build_portfolio(args.capital, score_all(pools))
    _print_portfolio(pf)
    for e in errors:
        print(f"  {_YEL}· {e}{_RST}")
    if not pf.positions:
        print(f"\n{_YEL}Nothing cleared the risk filters — likely a column "
              f"mismatch; run `scan-base --raw`.{_RST}")
    return 0


def cmd_rebalance(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.score import score_all
    from .base.portfolio import build_portfolio
    from .base.rebalance import plan_rebalance
    from .base import state
    from . import base_config
    try:
        client = CambrianClient()
        pools, _ = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1

    target = build_portfolio(args.capital, score_all(pools))
    current = state.current_usd(base_config.POSITIONS_FILE)
    actions = plan_rebalance(current, target)

    _print_portfolio(target)
    print(f"\nrebalance plan ({len(actions)} actions):")
    if not actions:
        print(f"  {_GREEN}already on target — nothing to do{_RST}")
    journal = Journal(config.ORDER_JOURNAL)
    for a in actions:
        color = _GREEN if a.kind == "ENTER" else _RED if a.kind == "EXIT" else _YEL
        print(f"  {color}{a.kind:<7}{_RST} {a.label:<18} "
              f"{_usd(a.from_usd)} → {_usd(a.to_usd)}  ({_usd(a.delta_usd)})")
        journal.record("order", {"dry_run": True, "desk": "base-lp",
                                 "order_kind": a.kind.lower(), "subject": a.pool,
                                 "label": a.label, "notional_usd": a.to_usd})

    if args.apply:
        new_state = {p.pool: {"usd": p.usd, "label": p.label, "sleeve": p.sleeve,
                              "entry_apr": p.entry_apr, "entry_tvl": p.entry_tvl}
                     for p in target.positions}
        state.save_positions(base_config.POSITIONS_FILE, new_state)
        print(f"\n  {_DIM}paper positions updated (dry-run){_RST}")
    else:
        print(f"\n  {_DIM}dry plan only — add --apply to record it as the new "
              f"paper portfolio{_RST}")
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    from .base import state
    from . import base_config
    pos = state.load_positions(base_config.POSITIONS_FILE)
    if not pos:
        print("no open paper positions")
        return 0
    total = sum(float(r.get("usd", 0)) for r in pos.values())
    print(f"paper portfolio — {len(pos)} positions, {_usd(total)} deployed\n")
    for pool, rec in sorted(pos.items(), key=lambda kv: -float(kv[1].get("usd", 0))):
        print(f"  {rec.get('label', pool):<18} {_usd(float(rec.get('usd', 0)))}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """One full autonomous cycle: scan -> size -> monitor -> execute (paper)."""
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.score import score_all
    from .base.portfolio import build_portfolio
    from .base.monitor import Holding, assess
    from .base.manager import plan_cycle
    from .base.execution import PaperExecutor
    from .base import state
    from . import base_config

    try:
        client = CambrianClient()
        pools, _ = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1

    target = build_portfolio(args.capital, score_all(pools))
    current = state.current_usd(base_config.POSITIONS_FILE)
    saved = state.load_positions(base_config.POSITIONS_FILE)
    cur_pool = {p.address: p for p in pools}

    holdings = [
        Holding(pool=pool, label=rec.get("label", pool),
                usd=float(rec.get("usd", 0)),
                is_stable=_is_stable_label(rec.get("label", "")),
                entry_apr=rec.get("entry_apr"),
                current_apr=(cur_pool[pool].fee_apr if pool in cur_pool else None),
                entry_tvl=rec.get("entry_tvl"),
                current_tvl=(cur_pool[pool].tvl_usd if pool in cur_pool else None))
        for pool, rec in saved.items()
    ]
    report = assess(holdings, market_drawdown=args.market_drop,
                    portfolio_drawdown=args.book_drawdown)

    final, actions = plan_cycle(target, current, report)

    if report.go_stable:
        print(f"{_RED}CIRCUIT BREAKER — FLIGHT TO STABLES{_RST}")
        for r in report.breaker_reasons:
            print(f"  {_RED}! {r}{_RST}")
        print()
    _print_portfolio(final)

    print(f"\ncycle: {len(actions)} action(s)"
          + (f", {len(report.rotations)} rotation signal(s)" if not report.go_stable else ""))
    for a in actions:
        color = _GREEN if a.kind == "ENTER" else _RED if a.kind == "EXIT" else _YEL
        print(f"  {color}{a.kind:<7}{_RST} {a.label:<18} "
              f"{_usd(a.from_usd)} → {_usd(a.to_usd)}")

    if args.apply:
        journal = Journal(config.ORDER_JOURNAL)
        new_state = PaperExecutor().execute(actions, final, journal)
        state.save_positions(base_config.POSITIONS_FILE, new_state)
        print(f"\n  {_DIM}executed (paper) — positions updated{_RST}")
    else:
        print(f"\n  {_DIM}dry cycle — add --apply to journal + persist{_RST}")
    return 0


def cmd_runners(args: argparse.Namespace) -> int:
    """Rank fresh RH-Chain launches by runner signal. --fixture for now; live
    discovery watches the launchpad factories once LAUNCHPADS is filled."""
    from .runners.snapshots import RunnerCandidate
    from .runners.score import rank_runners
    from .runners import config as rcfg

    if args.discover:
        return _discover_fresh_pools(args)

    if args.fixture:
        fx = _load(args.fixture)
        candidates = [RunnerCandidate(**c) for c in fx["candidates"]]
    else:
        candidates = _scan_live_candidates(args)
        if candidates is None:
            return 1

    ranked = rank_runners(candidates, include_cold=args.all)
    journal = Journal(config.ORDER_JOURNAL)
    print(f"Fresh runners — {len(candidates)} candidates, {len(ranked)} flagged\n")
    print(f"  {'TIER':<7}{'TOKEN':<14}{'SCORE':>6}  SIGNALS / REASONS")
    for s in ranked:
        color = _RED if s.tier == "hot" else _YEL if s.tier == "watch" else _DIM
        detail = "; ".join(s.signals) if s.signals else "; ".join(s.reasons)
        label = s.candidate.symbol or s.candidate.token[:12]
        print(f"  {color}{s.tier.upper():<7}{_RST}{label:<14}{s.score:>6.2f}  {detail}")
        if s.tier in ("hot", "watch"):
            journal.record("signal", {"desk": "runners", "signal": "runner",
                                      "subject": s.candidate.token, "tier": s.tier,
                                      "score": s.score, "signals": list(s.signals)})
    return 0


def _scan_live_candidates(args: argparse.Namespace):
    """Full live pipeline: discover fresh pools + enrich each. None on error."""
    from .chain import ChainClient, RpcError
    from .runners.feed import scan_live
    from .runners.blockscout import Blockscout
    from .runners import config as rcfg
    c = rcfg.CONTRACTS
    if not c.get("v3_factory") or not c.get("weth"):
        print(f"{_RED}Set v3_factory + weth in runners/config.py CONTRACTS{_RST}")
        return None
    try:
        client = ChainClient()
        bs = Blockscout()
        return scan_live(client, blocks=args.blocks, weth_usd=rcfg.WETH_USD,
                         window_blocks=rcfg.WINDOW_BLOCKS, bs_client=bs)
    except (RpcError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return None


def _discover_fresh_pools(args: argparse.Namespace) -> int:
    from .feeds.cambrian_api import CambrianError  # reused error type
    from .chain import ChainClient, RpcError
    from .runners.feed import discover_new_pools
    from .runners import config as rcfg
    c = rcfg.CONTRACTS
    if not c.get("v3_factory") or not c.get("weth"):
        print(f"{_RED}Set v3_factory + weth in runners/config.py CONTRACTS{_RST}")
        return 1
    try:
        client = ChainClient()
        latest = client.block_number()
        from_block = hex(max(latest - args.blocks, 0))
        pools = discover_new_pools(client, v3_factory=c["v3_factory"],
                                   weth=c["weth"], from_block=from_block)
    except (RpcError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    print(f"Fresh WETH pools in last {args.blocks} blocks: {len(pools)}\n")
    for p in pools:
        print(f"  {_GREEN}NEW{_RST} token {p['token']}  pool {p['pool']}  "
              f"fee {p['fee']}")
    if not pools:
        print(f"  {_DIM}(none — or verify v3_factory/weth/POOLCREATED_TOPIC0){_RST}")
    return 0


def _is_stable_label(label: str) -> bool:
    from . import base_config
    if "/" not in label:
        return False
    a, b = (s.strip().upper() for s in label.split("/", 1))
    return a in base_config.STABLES and b in base_config.STABLES


def cmd_monitor(args: argparse.Namespace) -> int:
    """Watch current positions; fire rotations on decay/drain and flight-to-stables."""
    from .feeds.cambrian_api import CambrianClient, CambrianError
    from .base.monitor import Holding, assess
    from .base import state
    from . import base_config

    pos = state.load_positions(base_config.POSITIONS_FILE)
    if not pos:
        print("no open paper positions to monitor")
        return 0
    try:
        client = CambrianClient()
        live, _ = _load_base_pools(client)
    except (CambrianError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    cur = {p.address: p for p in live}

    holdings = []
    for pool, rec in pos.items():
        lp = cur.get(pool)
        holdings.append(Holding(
            pool=pool, label=rec.get("label", pool), usd=float(rec.get("usd", 0)),
            is_stable=_is_stable_label(rec.get("label", "")),
            entry_apr=rec.get("entry_apr"),
            current_apr=(lp.fee_apr if lp else None),
            entry_tvl=rec.get("entry_tvl"),
            current_tvl=(lp.tvl_usd if lp else None),
        ))

    report = assess(holdings, market_drawdown=args.market_drop,
                    portfolio_drawdown=args.book_drawdown)
    journal = Journal(config.ORDER_JOURNAL)

    if report.go_stable:
        print(f"{_RED}CIRCUIT BREAKER — FLIGHT TO STABLES{_RST}")
        for r in report.breaker_reasons:
            print(f"  {_RED}! {r}{_RST}")
    print(f"\n  {'POOL':<18}{'ACTION':<9}WHY")
    for t in report.triggers:
        color = _RED if t.action == "ROTATE" else _GREEN
        print(f"  {t.label[:18]:<18}{color}{t.action:<9}{_RST}"
              f"{'; '.join(t.reasons)}")
        if t.action == "ROTATE":
            journal.record("signal", {"desk": "base-lp", "signal": "rotate",
                                      "subject": t.pool, "label": t.label,
                                      "reasons": list(t.reasons)})
    n = len(report.rotations)
    print(f"\n  {n} rotation signal(s)"
          + (" — all to stables" if report.go_stable else ""))
    print(f"  {_DIM}(price stop-loss needs token-price wiring; APR-decay + "
          f"TVL-drain + breaker are live){_RST}")
    return 0


def _usd(v):
    from .units import usd
    return usd(v) if v is not None else "?"


def _pct(v):
    from .units import pct
    return pct(v) if v is not None else "?"


def config_base_min_tvl():
    from . import base_config
    return base_config.BASE_LP.min_pool_tvl_usd


def cmd_review_hook(args: argparse.Namespace) -> int:
    from .tools.hook_review import review_hook
    try:
        report = review_hook(args.target)
    except RuntimeError as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    print(report)
    print(f"\n{_YEL}Reminder: this does NOT add the hook to REVIEWED_HOOKS. "
          f"Read the source yourself before trusting it.{_RST}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cambrian", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show config gaps, mode, and chain health")

    for name, fn in (("evaluate-degen", cmd_evaluate_degen),
                     ("evaluate-lp", cmd_evaluate_lp)):
        sp = sub.add_parser(name, help="run a desk decision against a JSON fixture")
        sp.add_argument("fixture", help="path to the candidate/pool fixture JSON")
        sp.add_argument("--execute", action="store_true",
                        help="route an approved decision through the (dry-run) executor")
        sp.set_defaults(func=fn)

    tp = sub.add_parser("tail", help="print the last N journal entries")
    tp.add_argument("-n", type=int, default=20)
    tp.set_defaults(func=cmd_tail)

    rp = sub.add_parser("review-hook",
                        help="Claude-assisted first-pass review of a v4 hook "
                             "(a .sol file or an on-chain address)")
    rp.add_argument("target", help="path to a .sol file, or a hook contract address")
    rp.set_defaults(func=cmd_review_hook)

    pc = sub.add_parser("probe-cambrian",
                        help="auth-check the Cambrian API and list indexed chains")
    pc.add_argument("--path", help="also dump the raw JSON from this endpoint path "
                                   "(e.g. /evm/dexes)")
    pc.set_defaults(func=cmd_probe_cambrian)

    sb = sub.add_parser("scan-base",
                        help="rank Base DEX pools by fee APR (read-only, live data)")
    sb.add_argument("--min-tvl", type=float, default=250_000.0,
                    help="TVL floor in USD (default 250k)")
    sb.add_argument("-n", type=int, default=25, help="max rows to show")
    sb.add_argument("--raw", action="store_true",
                    help="print the real column names from one DEX (to confirm ALIASES)")
    sb.set_defaults(func=cmd_scan_base)

    fd = sub.add_parser("field",
                        help="best APY across the whole field (LP + lending), ranked")
    fd.add_argument("--min-tvl", type=float, default=250_000.0,
                    help="TVL floor in USD (default 250k)")
    fd.add_argument("-n", type=int, default=40, help="max rows to show")
    fd.set_defaults(func=cmd_field)

    eb = sub.add_parser("evaluate-base-lp",
                        help="scan Base pools and run each through the Base LP desk")
    eb.add_argument("--position", type=float, default=250.0,
                    help="intended position size in USD")
    eb.add_argument("-n", type=int, default=25)
    eb.set_defaults(func=cmd_evaluate_base_lp)

    al = sub.add_parser("allocate",
                        help="LP vs lending for a token address (which yields more)")
    al.add_argument("token", help="token contract address (on Base)")
    al.set_defaults(func=cmd_allocate)

    pl = sub.add_parser("plan",
                        help="turn a deposit into a target LP portfolio (the brain)")
    pl.add_argument("capital", type=float, help="deposit size in USD, e.g. 10000")
    pl.set_defaults(func=cmd_plan)

    rb = sub.add_parser("rebalance",
                        help="plan the moves from current paper positions to a "
                             "fresh target (the babysitter)")
    rb.add_argument("capital", type=float, help="deposit size in USD")
    rb.add_argument("--apply", action="store_true",
                    help="record the target as the new paper portfolio")
    rb.set_defaults(func=cmd_rebalance)

    ps = sub.add_parser("positions", help="show current paper positions")
    ps.set_defaults(func=cmd_positions)

    ru = sub.add_parser("runners",
                        help="rank fresh RH-Chain launches by runner signal "
                             "(watch the pads from above)")
    ru.add_argument("--fixture", help="JSON of candidates to score")
    ru.add_argument("--discover", action="store_true",
                    help="list fresh WETH pools live from the V3 factory")
    ru.add_argument("--blocks", type=int, default=5000,
                    help="how many blocks back to scan for --discover")
    ru.add_argument("--all", action="store_true", help="include cold candidates")
    ru.set_defaults(func=cmd_runners)

    mo = sub.add_parser("monitor",
                        help="watch positions; fire rotations + flight-to-stables")
    mo.add_argument("--market-drop", type=float, default=None,
                    help="benchmark drawdown as a fraction (e.g. 0.15) to test the breaker")
    mo.add_argument("--book-drawdown", type=float, default=None,
                    help="portfolio drawdown from high-water as a fraction")
    mo.set_defaults(func=cmd_monitor)

    rn = sub.add_parser("run",
                        help="one full autonomous cycle: scan -> size -> monitor "
                             "-> execute (paper)")
    rn.add_argument("capital", type=float, help="deposit size in USD")
    rn.add_argument("--apply", action="store_true",
                    help="journal the moves and persist the new positions")
    rn.add_argument("--market-drop", type=float, default=None,
                    help="benchmark drawdown fraction, to exercise the breaker")
    rn.add_argument("--book-drawdown", type=float, default=None)
    rn.set_defaults(func=cmd_run)

    sub.choices["status"].set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
