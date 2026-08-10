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



def cmd_sweep(args: argparse.Namespace) -> int:
    """One pass over every launchpad: discover, price, gate, size, rank."""
    from .chain import ChainClient
    from .runners import config as rcfg
    from .runners.scanner import format_sweep, sweep

    client = ChainClient()
    latest = client.block_number()
    # Prefer a LIVE ETH price over the config default: RH_WETH_USD ships at 3000
    # and measured ~1917 against Flash, which would inflate every market cap and
    # every ticket sized from one by the same margin.
    eth_usd = args.weth_usd
    if eth_usd is None:
        from .runners.execution import live_eth_usd
        eth_usd = live_eth_usd(fallback=rcfg.WETH_USD)
        print("ETH/USD %.2f (live)" % eth_usd if eth_usd != rcfg.WETH_USD
              else "ETH/USD %.2f (config fallback)" % eth_usd)
    result = sweep(client, from_block=max(latest - args.blocks, 0), to_block=latest,
                   quote_price_usd=eth_usd, bankroll_usd=args.bankroll)
    print(format_sweep(result, limit=args.n))
    if args.json:
        print(json.dumps(result["rows"], indent=2, default=str))
    # Non-zero when the scan was incomplete: a partial sweep must not read as a
    # quiet market to whatever is calling this.
    return 1 if result["failed_chunks"] else 0



def cmd_watch(args: argparse.Namespace) -> int:
    """Run the desk live: discover on one cadence, mark and exit on a faster one."""
    import time as _t

    from .chain import ChainClient
    from .runners import config as rcfg
    from .runners import live as L
    from .runners.execution import live_eth_usd

    client = ChainClient()
    state = L.LiveState()
    eth = args.weth_usd or live_eth_usd(fallback=rcfg.WETH_USD)
    print("watching — scan %gs / mark %gs — ETH $%.2f — DRY RUN (no signing)"
          % (L.SCAN_INTERVAL_S, L.MARK_INTERVAL_S, eth))
    deadline = _t.time() + args.seconds if args.seconds else None
    try:
        while deadline is None or _t.time() < deadline:
            scan_due, mark_due = L.due(state)
            fresh, intents = [], []
            if scan_due:
                try:
                    fresh = L.scan_tick(client, state, quote_price_usd=eth,
                                        bankroll_usd=args.bankroll,
                                        blocks=args.blocks)
                except Exception as e:              # a scan failure must not stop marking
                    print("  scan error: %s" % str(e)[:90])
                    state.last_scan_at = _t.time()
            if mark_due:
                intents = L.mark_tick(client, state, weth_usd=eth)
            out = L.format_tick(fresh, intents, state)
            if out:
                print(out, flush=True)
            _t.sleep(min(L.MARK_INTERVAL_S, 1.0))
    except KeyboardInterrupt:
        print("\nstopped")
    print("%d scans, %d marks, %d tokens seen" % (state.scans, state.marks, len(state.seen)))
    return 0



def cmd_trade(args: argparse.Namespace) -> int:
    """Prepare (and optionally submit) ONE real trade. Guarded, small, explicit.

    Two phases on purpose. Without --signature this only PREPARES: it quotes,
    validates, and prints the EIP-712 payload for an external signer. With a
    signature it submits. Nothing in this process ever holds a key.
    """
    from .chain import ChainClient
    from .runners import config as rcfg
    from .runners import execution as X
    from .runners import router as R
    from .runners import sizing as S
    from .runners import stock_tokens as ST
    from .runners import venues as V
    from .runners.scanner import _resolve, sweep

    if not args.funder or not args.funder.startswith("0x"):
        print("--funder <your wallet address> is required")
        return 2
    client = ChainClient()
    eth = args.weth_usd or X.live_eth_usd(fallback=rcfg.WETH_USD)
    latest = client.block_number()

    row = None
    if args.token:
        result = sweep(client, from_block=max(latest - args.blocks, 0), to_block=latest,
                       quote_price_usd=eth, bankroll_usd=args.bankroll)
        for r in result["rows"]:
            if (r.get("token") or "").lower() == args.token.lower():
                row = r
                break
        if row is None:
            print("token %s not found in the last %d blocks" % (args.token, args.blocks))
            return 1
    else:
        result = sweep(client, from_block=max(latest - args.blocks, 0), to_block=latest,
                       quote_price_usd=eth, bankroll_usd=args.bankroll)
        ok = [r for r in result["rows"] if r.get("ok")]
        if not ok:
            print("nothing cleared the gate in the last %d blocks" % args.blocks)
            return 1
        row = ok[0]

    if not row.get("ok"):
        print("BLOCKED: %s" % "; ".join(row.get("reasons") or ["unknown"]))
        return 1

    venue = _resolve(client, row)
    if venue is None:
        print("could not resolve a venue for %s" % row["token"])
        return 1
    route = X.route_for(venue)
    size_usd = min(row.get("size_usd") or 0.0, args.max_usd)
    if size_usd <= 0:
        print("no size")
        return 1

    sym = ST.symbol_for(venue.quote_token) or "ETH"
    print("")
    print("  token       %s  (%s)" % (row["token"], row.get("pad")))
    print("  venue       %s   route %s" % (venue.kind, route))
    print("  quote asset %s" % sym)
    print("  market cap  $%.0f" % (row.get("market_cap_usd") or 0))
    print("  tax         %s" % (("%.1f%%" % (venue.tax_bps / 100))
                                if venue.tax_bps is not None else "n/a"))
    print("  size        $%.2f  (capped at --max-usd $%.2f)" % (size_usd, args.max_usd))
    print("  entry route %s" % R.describe(R.plan_route(venue, settlement_asset=R.USDG)))
    print("  exit route  %s" % R.describe(R.exit_route(venue, settlement_asset=R.USDG)))

    if route == X.ROUTE_CURVE:
        # A curve is not on Flash. Emit calldata for an external signer instead.
        from .runners import curve_exec as CX
        qin = int(size_usd / (ST.quote_price_usd(
            venue.quote_token, weth_usd=eth, usdg=rcfg.CONTRACTS.get("usdg"),
            weth=rcfg.CONTRACTS.get("weth")) or eth) * 10 ** venue.quote_decimals)
        txs = CX.build_buy(venue, quote_in=qin, recipient=args.funder,
                           client=client, owner=args.funder)
        print("\n  CURVE-DIRECT — Flash does not route this. Send these in order:")
        for i, tx in enumerate(txs, 1):
            print("   %d. %s" % (i, tx.note))
            print("      to    %s" % tx.to)
            print("      value %d" % tx.value)
            print("      data  %s" % tx.data)
        print("\n  minOut is baked into the calldata at %d bps slippage."
              % CX.DEFAULT_SLIPPAGE_BPS)
        return 0

    contra = venue.quote_token
    if not contra or contra.lower() == V.NATIVE_ETH:
        contra = X.NATIVE_SENTINEL
    qty = size_usd / (ST.quote_price_usd(contra, weth_usd=eth,
                                         usdg=rcfg.CONTRACTS.get("usdg"),
                                         weth=rcfg.CONTRACTS.get("weth")) or eth)
    try:
        prepared = X.prepare(target=row["token"], contra=contra, qty="%.8f" % qty,
                             funder=args.funder, max_slippage=args.max_slippage,
                             max_price_impact=args.max_slippage,
                             max_loss_pct=args.max_loss_pct)
    except X.FlashError as e:
        print("\n  REFUSED: %s" % e)
        return 1

    print("\n  quote %s" % prepared.quote_id)
    print("  spend $%.2f -> receive $%.2f  (%.2f%% on the leg)"
          % (prepared.spend_notional_usd or 0, prepared.receive_notional_usd or 0,
             prepared.slippage_vs_quote_pct or 0))
    for w in prepared.warnings:
        print("  WARNING: %s" % w)
    if prepared.wrap_tx:
        print("\n  1. WRAP native ETH first:")
        print("     to   %s" % prepared.wrap_tx.get("to"))
        print("     data %s" % prepared.wrap_tx.get("data"))
    if prepared.approve_tx:
        print("\n  2. APPROVE the settlement contract (once per token):")
        print("     to   %s" % prepared.approve_tx.get("to"))
        print("     data %s" % prepared.approve_tx.get("data"))
    if not args.signature:
        print("\n  3. SIGN this EIP-712 payload with %s:" % args.funder)
        print(prepared.order_typed_data)
        print("\n  then re-run the same command adding:  --signature 0x<sig> --yes")
        print("  NOTHING HAS BEEN SUBMITTED.")
        return 0
    if not args.yes:
        print("\n  --signature given but --yes missing. Refusing to submit.")
        return 2
    out = X.submit(prepared, funder=args.funder, signature=args.signature,
                   confirm=True)
    print("\n  SUBMITTED: %s" % json.dumps(out)[:400])
    return 0



def cmd_vault(args: argparse.Namespace) -> int:
    """Show the Definitive vault address to fund, and what is in it."""
    import os

    from .runners import definitive as D
    # YOUR wallet, not the vault's — the endpoint wants the address that will
    # send the deposit. It is not a secret, so the shell is a fine place for it.
    wallet = args.wallet or os.getenv("RH_WALLET") or os.getenv("DEGEN_WALLET")
    if not wallet:
        print("\n  need your own wallet address (the one you will send from):")
        print("    python -m cambrian vault --wallet 0xYourAddress")
        print("  or set it once:  $env:RH_WALLET=\"0xYourAddress\"")
        return 2
    try:
        addr, vault_id = D.deposit_address(D.CHAIN, wallet_address=wallet,
                                           debug=args.debug)
    except D.DefinitiveError as e:
        print("\n  vault lookup failed (HTTP %s): %s" % (e.status or "?", e))
        k, s = D.api_key(), D.api_secret()
        if not k or not s:
            print("  -> DEFINITIVE_API_KEY / DEFINITIVE_API_SECRET are not set in "
                  "this window")
        else:
            print("  -> key starts %s..., secret starts %s..."
                  % (k[:9], (s or "")[:6]))
            if not k.startswith("dpka_"):
                print("  -> the KEY should start with dpka_ — are the two swapped?")
            if not s.startswith("dpks_"):
                print("  -> the SECRET should start with dpks_ — are the two swapped?")
        if e.status == 401:
            print("  -> 401 means the signature did not match. If both keys are "
                  "correct and unswapped, this is my bug — re-run with --debug "
                  "and send me\n     that output; it redacts the key and never "
                  "touches the secret.")
        elif e.status and e.status >= 500:
            print("  -> a 5xx usually means a malformed key rather than a wrong one")
        return 1
    print("\n  Robinhood Chain vault: %s" % addr)
    if vault_id:
        print("  vault id %s" % vault_id)
    print("  Send ETH or USDG here. The vault is created on demand per chain.")
    try:
        pos = D.positions()
        rows = pos.get("positions") or pos.get("data") or []
        print("\n  %d position(s)" % len(rows))
        for r in rows[:15]:
            print("   %-12s %-18s %s" % (r.get("symbol") or "?",
                                         r.get("balance") or r.get("amount") or "?",
                                         r.get("notional") or ""))
    except D.DefinitiveError as e:
        print("  positions unavailable: %s" % e)
    return 0


def cmd_trade_vault(args: argparse.Namespace) -> int:
    """Quote and (with --yes) execute ONE trade from the Definitive vault.

    No wallet, no EIP-712, no approve: on this API the key IS the authorization,
    which is exactly why --yes is mandatory and --max-usd is a hard cap.
    """
    from .chain import ChainClient
    from .runners import config as rcfg
    from .runners import definitive as D
    from .runners import execution as X
    from .runners import stock_tokens as ST
    from .runners import venues as V
    from .runners.scanner import _resolve, sweep

    client = ChainClient()
    eth = args.weth_usd or X.live_eth_usd(fallback=rcfg.WETH_USD)
    latest = client.block_number()
    result = sweep(client, from_block=max(latest - args.blocks, 0), to_block=latest,
                   quote_price_usd=eth, bankroll_usd=args.bankroll)
    rows = [r for r in result["rows"] if r.get("ok")]
    if args.token:
        rows = [r for r in result["rows"]
                if (r.get("token") or "").lower() == args.token.lower()]
    if not rows:
        print("nothing cleared the gate in the last %d blocks" % args.blocks)
        return 1
    row = rows[0]
    if not row.get("ok"):
        print("BLOCKED: %s" % "; ".join(row.get("reasons") or ["unknown"]))
        return 1
    venue = _resolve(client, row)
    size_usd = min(row.get("size_usd") or 0.0, args.max_usd)
    if size_usd <= 0:
        print("no size — check RH_BANKROLL_USD / RH_MIN_BUY_USD for a small wallet")
        return 1

    contra = args.contra or rcfg.CONTRACTS["weth"]
    qprice = ST.quote_price_usd(contra, weth_usd=eth,
                                usdg=rcfg.CONTRACTS.get("usdg"),
                                weth=rcfg.CONTRACTS.get("weth")) or eth
    qty = size_usd / qprice
    print("\n  token      %s  (%s)" % (row["token"], row.get("pad")))
    print("  market cap $%.0f   tax %s" % (
        row.get("market_cap_usd") or 0,
        ("%.1f%%" % (venue.tax_bps / 100)) if venue and venue.tax_bps is not None else "n/a"))
    print("  spending   %.8f of %s  (~$%.2f, capped at $%.2f)"
          % (qty, contra, size_usd, args.max_usd))
    try:
        q = D.quicktrade_quote(target=row["token"], contra=contra,
                               qty="%.8f" % qty, side="buy")
    except D.DefinitiveError as e:
        print("\n  quote failed (%s): %s" % (e.status, e))
        if e.raw:
            print("  raw: %s" % e.raw[:600])
        print("  -> isolate it:  quote --target %s --contra %s --qty %.4f --raw"
              % (row["token"], contra, qty))
        return 1
    c = D.quote_cost(q)
    print("\n  quote %s" % (c["quote_id"] or "-"))
    print("  spend $%s -> receive $%s  (%s on the leg)" % (
        c["spend_usd"], c["receive_usd"],
        ("%.2f%%" % c["loss_pct"]) if c["loss_pct"] is not None else "?"))
    print("  price impact %s   fee $%s   minOut %s"
          % (c["price_impact"], c["fee_usd"], c["min_out"]))
    for w in c["warnings"]:
        print("  WARNING: %s" % w)
    # `qty` is documented only as "Amount to trade", and Definitive's own two
    # examples read inconsistently about whether it denominates the target or the
    # contra asset. Rather than guess, check what the quote says we are actually
    # spending: if the units are backwards, fromNotional lands nowhere near the
    # ticket and this refuses. Cheap, and it fails toward not trading.
    if c["spend_usd"] is None:
        print("\n  REFUSED: the quote did not say what this spends.")
        return 1
    if c["spend_usd"] > args.max_usd * 1.05:
        print("\n  REFUSED: quote spends $%.2f, above the $%.2f cap — the qty "
              "units are\n  not what was assumed. Nothing submitted."
              % (c["spend_usd"], args.max_usd))
        return 1
    if c["loss_pct"] is not None and c["loss_pct"] > args.max_loss_pct:
        print("\n  REFUSED: leg loses %.2f%% (> %.1f%% limit)"
              % (c["loss_pct"], args.max_loss_pct))
        return 1
    if not args.yes:
        print("\n  NOT SUBMITTED. Re-run with --yes to execute this trade.")
        return 0
    out = D.quicktrade_submit(target=row["token"], contra=contra,
                              qty="%.8f" % qty, side="buy",
                              slippage_tolerance="%.4f" % args.max_slippage,
                              confirm=True)
    print("\n  SUBMITTED: %s" % json.dumps(out)[:400])
    print("  track it:  python -m cambrian vault --wallet <your address>")
    return 0



def cmd_quote(args: argparse.Namespace) -> int:
    """Quote ONE pair directly, with no scan and no sizing in the way.

    Exists to bisect a failing quote. `trade-vault` does discovery, sizing,
    routing and quoting in one breath, so a 400 from it could come from any of
    them. This does exactly one thing, and defaults to USDG -> WETH — a pair
    Definitive certainly knows — so a bare run answers "is the endpoint working
    at all?" before we blame a two-hour-old memecoin.
    """
    from .runners import config as rcfg
    from .runners import definitive as D

    contra = args.contra or rcfg.CONTRACTS["usdg"]
    target = args.target or rcfg.CONTRACTS["weth"]
    print("\n  %s %s of %s\n  for %s" % (args.side, args.qty, contra, target))
    try:
        q = D.quicktrade_quote(target=target, contra=contra, qty=str(args.qty),
                               side=args.side, debug=args.debug)
    except D.DefinitiveError as e:
        print("\n  FAILED %s: %s" % (e.status, e))
        if e.raw:
            print("  raw: %s" % e.raw[:1000])
        return 1
    if args.raw:
        print("\n%s" % json.dumps(q, indent=2)[:3000])
        return 0
    c = D.quote_cost(q)
    print("\n  spend $%s -> receive $%s  (%s)" % (
        c["spend_usd"], c["receive_usd"],
        ("%.2f%% on the leg" % c["loss_pct"]) if c["loss_pct"] is not None else "?"))
    print("  buy %s   sell %s" % (c["buy_amount"], c["sell_amount"]))
    print("  impact %s   fee $%s   minOut %s   marketable %s"
          % (c["price_impact"], c["fee_usd"], c["min_out"], c["marketable"]))
    for w in c["warnings"]:
        print("  WARNING: %s" % w)
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Show where credentials were found and whether they look right.

    Prints prefixes only. A secret that has to be echoed to be verified is a
    secret that ends up in a screenshot.
    """
    from . import build as B
    from . import config as appcfg
    from .runners import definitive as D

    print("\n  build %s" % B.BUILD)
    print("\n  looking for cambrian.env in:")
    for c in appcfg.key_file_candidates():
        print("    %-52s %s" % (c, "FOUND" if c.is_file() else "-"))
    k, s = D.api_key(), D.api_secret()
    for label, name, value, prefix in (
            ("DEFINITIVE_API_KEY   ", "DEFINITIVE_API_KEY", k, "dpka_"),
            ("DEFINITIVE_API_SECRET", "DEFINITIVE_API_SECRET", s, "dpks_")):
        shown = ("%s... (%d chars)" % (value[:len(prefix) + 4], len(value))
                 if value else "NOT SET")
        print("\n  %s  %s" % (label, shown))
        src = appcfg.KEY_SOURCES.get(name)
        print("    from %s" % (src or ("the environment" if value else "-")))
    in_files = appcfg.credentials_in_files()
    ok = True
    for name, value, prefix, env_name in (
            ("DEFINITIVE_API_KEY", k, "dpka_", "DEFINITIVE_API_KEY"),
            ("DEFINITIVE_API_SECRET", s, "dpks_", "DEFINITIVE_API_SECRET")):
        if value and value.startswith(prefix):
            continue
        ok = False
        # The loader recovers a swap or a pasted terminal line, and now
        # overrides shell text with a good file value, so reaching here means
        # the prefix is genuinely absent from BOTH — say which, precisely,
        # because "put it in the file" and "clear your shell" are different
        # fixes and guessing between them is what cost the last two rounds.
        if not value:
            print("\n  -> %s is not set anywhere." % name)
        elif name in in_files:
            print("\n  -> %s is being overridden by something this process "
                  "cannot see." % name)
        else:
            print("\n  -> %s holds a value that is not a %s credential, and no "
                  "%s\n     string appears in cambrian.env either — so the "
                  "credential itself is\n     missing, not merely misplaced."
                  % (name, prefix, prefix))
            if not appcfg.KEY_SOURCES.get(name):
                print("     It came from the SHELL in this window "
                      "(`$env:%s`), not from the file." % env_name)
        print("     Put the real value in cambrian.env:  %s=%s..."
              % (name, prefix))
    print("\n  %s" % ("looks right — try: vault" if ok else "not ready yet"))
    return 0 if ok else 1


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

    if args.find_contracts:
        return _find_contracts(args)

    if args.watch:
        return cmd_runners_watch(args)

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
    print(f"Fresh runners — {len(candidates)} candidates, {len(ranked)} flagged\n")
    print(f"  {'TIER':<7}{'TOKEN':<14}{'SCORE':>6}  SIGNALS / REASONS")
    _emit_runners(ranked, Journal(config.ORDER_JOURNAL))
    return 0


def _emit_runners(ranked, journal) -> None:
    for s in ranked:
        color = _RED if s.tier == "hot" else _YEL if s.tier == "watch" else _DIM
        detail = "; ".join(s.signals) if s.signals else "; ".join(s.reasons)
        label = s.candidate.symbol or s.candidate.token[:12]
        print(f"  {color}{s.tier.upper():<7}{_RST}{label:<14}{s.score:>6.2f}  {detail}")
        if s.tier in ("hot", "watch"):
            journal.record("signal", {"desk": "runners", "signal": "runner",
                                      "subject": s.candidate.token, "tier": s.tier,
                                      "score": s.score, "signals": list(s.signals)})


def cmd_runners_watch(args: argparse.Namespace) -> int:
    """Always-on loop: rescan every interval, alert only on newly-flagged runners."""
    import time
    from .runners.score import rank_runners
    from .runners.watch import load_seen, save_seen, select_new
    from .runners import config as rcfg
    seen = load_seen(rcfg.SEEN_FILE)
    journal = Journal(config.ORDER_JOURNAL)
    print(f"watching for fresh runners every {args.interval}s "
          f"({len(seen)} tokens already seen) — Ctrl-C to stop")
    try:
        while True:
            candidates = _scan_live_candidates(args)
            if candidates is not None:
                fresh = select_new(rank_runners(candidates, include_cold=args.all), seen)
                if fresh:
                    _emit_runners(fresh, journal)
                    for s in fresh:
                        seen.add(s.candidate.token.lower())
                    save_seen(rcfg.SEEN_FILE, seen)
                else:
                    print(f"  {_DIM}· no new runners{_RST}")
            time.sleep(max(args.interval, 1))
    except KeyboardInterrupt:
        print("\nstopped.")
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
                         window_blocks=rcfg.WINDOW_BLOCKS, bs_client=bs,
                         pool_manager=c.get("pool_manager") or None,
                         native_eth=c.get("native_eth"))
    except (RpcError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return None


def _find_contracts(args: argparse.Namespace) -> int:
    """Self-bootstrap the V3 factory + v4 PoolManager from on-chain events."""
    from .chain import ChainClient, RpcError
    from .runners.feed import find_contracts
    from .runners import config as rcfg
    try:
        client = ChainClient()
        latest = client.block_number()
        found = find_contracts(client, from_block=hex(max(latest - args.blocks, 0)))
    except (RpcError, requests.RequestException) as exc:
        print(f"{_RED}{exc}{_RST}")
        return 1
    for key, counts in found.items():
        print(f"\n{key} (emitters of {'PoolCreated' if key=='v3_factory' else 'Initialize'}):")
        if not counts:
            print(f"  {_DIM}none in last {args.blocks} blocks — widen --blocks{_RST}")
        for addr, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {_GREEN}{addr}{_RST}  ({n} events)")
    cfg_v3 = rcfg.CONTRACTS.get("v3_factory", "").lower()
    if cfg_v3 and cfg_v3 in found.get("v3_factory", {}):
        print(f"\n  {_GREEN}✓ configured v3_factory matches on-chain{_RST}")
    print(f"\n  {_DIM}put the top pool_manager address into CONTRACTS to enable v4{_RST}")
    return 0


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
    print(f"Fresh V3 WETH pools in last {args.blocks} blocks: {len(pools)}")
    for p in pools:
        print(f"  {_GREEN}V3 {_RST} token {p['token']}  pool {p['pool']}  fee {p['fee']}")

    # v4 (Pons v2 launches here) — only if the PoolManager is configured.
    if c.get("pool_manager"):
        from .runners.feed import discover_new_pools_v4
        try:
            v4 = discover_new_pools_v4(client, pool_manager=c["pool_manager"],
                                       weth=c["weth"], native_eth=c.get("native_eth"),
                                       from_block=from_block)
            print(f"\nFresh v4 pools (ETH/WETH-paired): {len(v4)}")
            for p in v4:
                hook = p["hooks"]
                hooked = "" if hook.lower() == "0x" + "0" * 40 else f"  {_YEL}hook {hook}{_RST}"
                print(f"  {_GREEN}v4 {_RST} token {p['token']}  vs {p['quote']}"
                      f"  id {p['pool_id'][:14]}…{hooked}")
        except (RpcError, requests.RequestException) as exc:
            print(f"  {_YEL}v4 discovery error: {exc}{_RST}")
    else:
        print(f"\n  {_DIM}(set pool_manager in CONTRACTS to also catch v4 / "
              f"Pons-v2 launches){_RST}")
    if not pools:
        print(f"  {_DIM}(no v3 pools — or verify v3_factory/weth/POOLCREATED_TOPIC0){_RST}")
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
    from . import build as _build
    p = argparse.ArgumentParser(prog="cambrian", description=__doc__)
    p.add_argument("--version", action="version", version="cambrian %s" % _build.BUILD)
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

    sw = sub.add_parser("sweep",
                        help="scan every launchpad: discover, price, gate, size")
    sw.add_argument("--blocks", type=int, default=1200,
                    help="how far back to scan (default 1200 ~ 2 min)")
    sw.add_argument("--bankroll", type=float, default=None,
                    help="account size in USD for position sizing")
    sw.add_argument("--weth-usd", type=float, default=None, dest="weth_usd")
    sw.add_argument("-n", type=int, default=25, help="max rows to show")
    sw.add_argument("--json", action="store_true", help="also emit raw rows as JSON")
    sw.set_defaults(func=cmd_sweep)

    wt = sub.add_parser("watch",
                        help="run the desk live (dry run): discover, mark, exit")
    wt.add_argument("--blocks", type=int, default=1200, help="scan window per sweep")
    wt.add_argument("--bankroll", type=float, default=None)
    wt.add_argument("--weth-usd", type=float, default=None, dest="weth_usd")
    wt.add_argument("--seconds", type=float, default=None,
                    help="stop after N seconds (default: run until interrupted)")
    wt.set_defaults(func=cmd_watch)

    td = sub.add_parser("trade",
                        help="prepare (and with a signature, submit) ONE real trade")
    td.add_argument("--funder", required=True, help="your wallet address")
    td.add_argument("--token", default=None, help="specific token (default: best candidate)")
    td.add_argument("--max-usd", type=float, default=15.0, dest="max_usd",
                    help="hard cap on this trade (default 15)")
    td.add_argument("--bankroll", type=float, default=None)
    td.add_argument("--blocks", type=int, default=1500)
    td.add_argument("--weth-usd", type=float, default=None, dest="weth_usd")
    td.add_argument("--max-slippage", type=float, default=0.05, dest="max_slippage")
    td.add_argument("--max-loss-pct", type=float, default=10.0, dest="max_loss_pct")
    td.add_argument("--signature", default=None, help="EIP-712 signature from your wallet")
    td.add_argument("--yes", action="store_true", help="required to actually submit")
    td.set_defaults(func=cmd_trade)

    vt = sub.add_parser("vault", help="show the Definitive vault address and positions")
    vt.add_argument("--wallet", default=None,
                    help="your own wallet address (required by the API)")
    vt.add_argument("--debug", action="store_true",
                    help="print the signed string (key redacted) to diagnose a 401")
    vt.set_defaults(func=cmd_vault)

    ky = sub.add_parser("keys", help="check where credentials were found")
    ky.set_defaults(func=cmd_keys)

    qt = sub.add_parser("quote", help="quote one pair directly (no scan, no sizing)")
    qt.add_argument("--target", default=None, help="asset to buy (default WETH)")
    qt.add_argument("--contra", default=None, help="asset to spend (default USDG)")
    qt.add_argument("--qty", default="8")
    qt.add_argument("--side", default="buy", choices=("buy", "sell"))
    qt.add_argument("--raw", action="store_true", help="dump the whole response")
    qt.add_argument("--debug", action="store_true", help="print the signed string")
    qt.set_defaults(func=cmd_quote)

    tv = sub.add_parser("trade-vault",
                        help="quote/execute ONE trade from the Definitive vault")
    tv.add_argument("--max-usd", type=float, default=8.0, dest="max_usd")
    tv.add_argument("--token", default=None)
    tv.add_argument("--contra", default=None, help="asset to spend (default WETH)")
    tv.add_argument("--bankroll", type=float, default=None)
    tv.add_argument("--blocks", type=int, default=1500)
    tv.add_argument("--weth-usd", type=float, default=None, dest="weth_usd")
    tv.add_argument("--max-loss-pct", type=float, default=10.0, dest="max_loss_pct")
    # Definitive defaults slippageTolerance to 1%, which a fresh launch will not
    # fill inside. 5% is the desk's own cap and is passed explicitly.
    tv.add_argument("--max-slippage", type=float, default=0.05, dest="max_slippage",
                    help="slippage tolerance sent to Definitive (0.05 = 5%%)")
    tv.add_argument("--yes", action="store_true", help="required to execute")
    tv.set_defaults(func=cmd_trade_vault)

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
    ru.add_argument("--find-contracts", action="store_true",
                    help="self-discover the V3 factory + v4 PoolManager from chain events")
    ru.add_argument("--watch", action="store_true",
                    help="always-on: rescan on an interval, alert only on new runners")
    ru.add_argument("--interval", type=int, default=60,
                    help="seconds between scans in --watch mode")
    ru.add_argument("--blocks", type=int, default=5000,
                    help="how many blocks back to scan")
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
