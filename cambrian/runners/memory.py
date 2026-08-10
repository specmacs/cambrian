"""The desk's memory: what it did, and what happened.

Written because the owner asked whether to move to LLM trading, on the grounds
that *"there is nothing back testing our trading performance and making
corrections to it"*. That diagnosis is exactly right and the conclusion does not
follow. The missing piece is not a different kind of decider — it is the
feedback loop. An LLM handed no outcome data guesses thresholds too; it just
guesses less legibly, more slowly, and at ~1-2s per call on a chain that produces
a block every 100ms.

So: record the FEATURES visible at entry alongside the realised outcome, and
compute base rates from them. That single file answers questions no amount of
judgement can — is the `micro` profile actually paying? do stalls save money or
cost it? is pons-v1 better than pools.trade at this size? — and it is the input
any later model would need anyway. Statistics first, because at 40 trades a
slice-level win rate is worth more than any opinion, and it costs a microsecond.

`examples/cambrian_desk.py` has had this for the paper desk the whole time
(`record_outcome` / `base_rates` / `memory_brief`). This is that machinery,
ported to the vault path and extended with the fields that now matter: the exit
profile, and the reason the position was closed.

Deliberately a plain JSONL append. A trade record that needs a database to be
written is a trade record that goes missing on the day the database is down.
"""

from __future__ import annotations

import json
import os
import time

TRADES_FILE = os.getenv("RH_TRADES_FILE", "cambrian_trades.jsonl")


def record(*, token: str, symbol: str, pad: str, profile: str, why: str,
           cost_usd: float, exit_usd: float | None, opened_at: float,
           market_cap_usd: float | None = None, tax_bps: int | None = None,
           recovery: float | None = None, path: str | None = None) -> None:
    """Append one closed trade. Never raises — bookkeeping must not stop a desk.

    `exit_usd` is None when the position left through some other surface, and it
    stays None rather than being inferred: a fabricated outcome would poison
    every base rate computed from this file afterwards.
    """
    try:
        pnl = None if exit_usd is None else round(exit_usd - cost_usd, 4)
        row = {
            "ts": int(time.time()), "token": token, "sym": symbol, "pad": pad,
            "profile": profile, "why": why,
            "cost_usd": round(cost_usd, 4),
            "exit_usd": None if exit_usd is None else round(exit_usd, 4),
            "pnl": pnl,
            "pnl_pct": (None if (pnl is None or not cost_usd)
                        else round(100 * pnl / cost_usd, 2)),
            "win": None if pnl is None else int(pnl > 0),
            "held_s": int(time.time() - opened_at),
            "mc": market_cap_usd, "tax_bps": tax_bps, "recovery": recovery,
        }
        with open(path or TRADES_FILE, "a", encoding="utf8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def load(path: str | None = None) -> list[dict]:
    rows = []
    try:
        with open(path or TRADES_FILE, encoding="utf8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue        # one bad line must not lose the file
    except OSError:
        return []
    return rows


def _bucket(v, edges) -> str:
    if v is None:
        return "?"
    for e in edges:
        if v < e:
            return "<%g" % e
    return ">=%g" % edges[-1]


def _agg(rs: list) -> dict:
    scored = [r for r in rs if r.get("pnl") is not None]
    if not scored:
        return {"n": len(rs), "win_pct": None, "avg_pnl": None, "total": 0.0}
    n = len(scored)
    return {"n": n,
            "win_pct": round(100 * sum(r["win"] for r in scored) / n),
            "avg_pnl": round(sum(r["pnl"] for r in scored) / n, 3),
            "total": round(sum(r["pnl"] for r in scored), 2),
            "avg_held_s": round(sum(r.get("held_s") or 0 for r in scored) / n)}


MIN_SLICE = int(os.getenv("RH_MIN_SLICE", "3"))


def base_rates(path: str | None = None) -> dict:
    """Win rate and expectancy, overall and sliced by what was knowable at entry.

    Slices under `MIN_SLICE` are dropped. A 1-trade "pattern" is noise, and
    presenting it as evidence is how a desk talks itself into a bad rule — the
    exact failure this file exists to prevent.
    """
    rows = load(path)
    if not rows:
        return {"n": 0}
    out = {"n": len(rows), "overall": _agg(rows)}
    for key, fn in (
            ("by_pad", lambda r: r.get("pad") or "?"),
            ("by_profile", lambda r: r.get("profile") or "?"),
            ("by_exit", lambda r: (r.get("why") or "?").split()[0]),
            ("by_mc", lambda r: _bucket(r.get("mc"), [25_000, 150_000])),
            ("by_hold", lambda r: _bucket(r.get("held_s"), [60, 300]))):
        groups: dict = {}
        for r in rows:
            groups.setdefault(fn(r), []).append(r)
        out[key] = {k: _agg(v) for k, v in sorted(groups.items())
                    if len([x for x in v if x.get("pnl") is not None]) >= MIN_SLICE}
    return out


def corrections(path: str | None = None) -> list[str]:
    """Concrete changes the record supports. Empty until there is evidence.

    Only states what the data says, and only where the sample carries it. This is
    the part that would otherwise be guesswork dressed up as intuition — and it
    is also exactly the brief you would hand a model later, once there is enough
    history for one to add anything.
    """
    br = base_rates(path)
    if br.get("n", 0) < 10:
        return []
    notes: list[str] = []
    overall = br["overall"]
    if overall.get("avg_pnl") is not None and overall["avg_pnl"] < 0:
        notes.append("Overall expectancy is NEGATIVE (%s/trade over %d): the edge "
                     "is not there yet — cut size before tuning anything else."
                     % (overall["avg_pnl"], overall["n"]))
    for label, key in (("profile", "by_profile"), ("pad", "by_pad"),
                       ("exit", "by_exit"), ("market cap", "by_mc")):
        group = br.get(key) or {}
        scored = {k: v for k, v in group.items() if v.get("avg_pnl") is not None}
        if len(scored) < 2:
            continue
        best = max(scored.items(), key=lambda kv: kv[1]["avg_pnl"])
        worst = min(scored.items(), key=lambda kv: kv[1]["avg_pnl"])
        if worst[1]["avg_pnl"] < 0 <= best[1]["avg_pnl"]:
            notes.append("%s: '%s' loses (%s/trade over %d) while '%s' pays "
                         "(%s over %d) — stop taking the first."
                         % (label, worst[0], worst[1]["avg_pnl"], worst[1]["n"],
                            best[0], best[1]["avg_pnl"], best[1]["n"]))
    return notes
