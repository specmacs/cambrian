"""The feedback loop — the thing whose absence made LLM trading look attractive.

The owner asked whether to move to LLM trading because "there is nothing back
testing our trading performance and making corrections to it". The diagnosis was
right; the missing piece is the record, not a different decider.
"""

import os
import tempfile
import time

from cambrian.runners import memory as MEM


def _path():
    return os.path.join(tempfile.mkdtemp(), "trades.jsonl")


def _fill(path, n, *, exit_usd, pad="pons-v1", profile="micro", why="stop -20%",
          mc=9_000):
    for i in range(n):
        MEM.record(token="0x%d" % i, symbol="S", pad=pad, profile=profile,
                   why=why, cost_usd=8.0, exit_usd=exit_usd,
                   opened_at=time.time() - 60, market_cap_usd=mc, path=path)


def test_a_closed_trade_records_its_outcome_and_its_features():
    p = _path()
    _fill(p, 1, exit_usd=9.0)
    row = MEM.load(p)[0]
    assert row["pnl"] == 1.0 and row["win"] == 1
    assert row["profile"] == "micro" and row["pad"] == "pons-v1"
    assert row["mc"] == 9_000 and row["held_s"] >= 59


def test_an_unknown_outcome_stays_unknown():
    # A position sold outside the desk has no knowable proceeds. Inventing them
    # would poison every base rate computed from this file afterwards.
    p = _path()
    MEM.record(token="0x1", symbol="S", pad="?", profile="standard",
               why="closed outside the desk", cost_usd=8.0, exit_usd=None,
               opened_at=time.time(), path=p)
    row = MEM.load(p)[0]
    assert row["pnl"] is None and row["win"] is None
    assert MEM.base_rates(p)["overall"]["win_pct"] is None


def test_thin_slices_are_dropped_because_they_are_noise():
    # A 1-trade "pattern" is how a desk talks itself into a bad rule.
    p = _path()
    _fill(p, 1, exit_usd=20.0, pad="flap")
    _fill(p, MEM.MIN_SLICE, exit_usd=9.0, pad="pons-v1")
    by_pad = MEM.base_rates(p)["by_pad"]
    assert "flap" not in by_pad
    assert "pons-v1" in by_pad


def test_the_record_names_the_losing_slice():
    p = _path()
    _fill(p, 6, exit_usd=7.2, profile="micro", pad="pons-v1")
    _fill(p, 6, exit_usd=9.1, profile="standard", pad="pools-trade", mc=90_000)
    notes = " | ".join(MEM.corrections(p))
    assert "micro" in notes and "standard" in notes


def test_no_corrections_are_offered_before_there_is_evidence():
    p = _path()
    _fill(p, 4, exit_usd=1.0)
    assert MEM.corrections(p) == []


def test_a_negative_expectancy_is_called_out_first():
    p = _path()
    _fill(p, 12, exit_usd=6.0)
    assert "NEGATIVE" in MEM.corrections(p)[0]


def test_a_corrupt_line_does_not_lose_the_file():
    p = _path()
    _fill(p, 2, exit_usd=9.0)
    with open(p, "a", encoding="utf8") as fh:
        fh.write("{not json\n")
    _fill(p, 1, exit_usd=9.0)
    assert len(MEM.load(p)) == 3


def test_bookkeeping_never_raises():
    # A trade record that can stop the desk is worse than no trade record.
    MEM.record(token=None, symbol=None, pad=None, profile=None, why=None,
               cost_usd="not a number", exit_usd=None, opened_at=0,
               path="/nonexistent/dir/x.jsonl")
