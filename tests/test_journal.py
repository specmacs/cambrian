import os

from cambrian.journal import Journal


def test_record_appends_and_reads_back(tmp_path):
    path = os.path.join(tmp_path, "journal.jsonl")
    j = Journal(path)
    j.record("decision", {"desk": "degen", "approved": True})
    j.record("order", {"desk": "degen", "dry_run": True})

    all_recs = j.read_all()
    assert len(all_recs) == 2
    assert all_recs[0]["kind"] == "decision"
    assert all_recs[0]["approved"] is True
    assert "ts" in all_recs[0]           # timestamp stamped on every record
    assert all_recs[1]["kind"] == "order"


def test_record_is_append_only(tmp_path):
    path = os.path.join(tmp_path, "journal.jsonl")
    j = Journal(path)
    j.record("decision", {"n": 1})
    j2 = Journal(path)                    # fresh handle, same file
    j2.record("decision", {"n": 2})
    assert [r["n"] for r in Journal(path).read_all()] == [1, 2]


def test_tail_and_filter(tmp_path):
    path = os.path.join(tmp_path, "journal.jsonl")
    j = Journal(path)
    for i in range(5):
        j.record("decision", {"desk": "lp" if i % 2 else "degen", "n": i})
    assert [r["n"] for r in j.tail(2)] == [3, 4]
    assert [r["n"] for r in j.filter(desk="lp")] == [1, 3]


def test_read_missing_file_is_empty(tmp_path):
    assert Journal(os.path.join(tmp_path, "nope.jsonl")).read_all() == []
