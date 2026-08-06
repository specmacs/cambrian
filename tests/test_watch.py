import os

from cambrian.runners.watch import load_seen, save_seen, select_new


class _S:
    def __init__(self, token):
        self.candidate = type("C", (), {"token": token})()


def test_select_new_filters_seen_case_insensitive():
    ranked = [_S("0xAAA"), _S("0xBBB"), _S("0xCCC")]
    seen = {"0xaaa", "0xccc"}
    fresh = select_new(ranked, seen)
    assert [s.candidate.token for s in fresh] == ["0xBBB"]


def test_seen_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "seen.json")
    assert load_seen(path) == set()          # missing file -> empty
    save_seen(path, {"0xAAA", "0xBbB"})
    assert load_seen(path) == {"0xaaa", "0xbbb"}  # normalized lowercase


def test_select_new_all_seen_returns_empty():
    ranked = [_S("0x1"), _S("0x2")]
    assert select_new(ranked, {"0x1", "0x2"}) == []
