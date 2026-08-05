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

    sub.choices["status"].set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
