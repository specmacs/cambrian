from cambrian.feeds.cambrian_api import paginate


def test_pages_until_short_page():
    # 250 rows, page_size 100 -> pages of 100,100,50 then stop.
    data = [{"poolId": str(i)} for i in range(250)]

    def fetch(offset):
        return data[offset:offset + 100]

    out = paginate(fetch, page_size=100, max_pages=50, key="poolId")
    assert len(out) == 250


def test_stops_when_offset_ignored():
    # A broken API that ignores offset and returns the same page every time.
    page = [{"poolId": str(i)} for i in range(100)]

    calls = {"n": 0}

    def fetch(offset):
        calls["n"] += 1
        return page  # same 100 rows regardless of offset

    out = paginate(fetch, page_size=100, max_pages=50, key="poolId")
    assert len(out) == 100          # deduped to one page
    assert calls["n"] == 2          # stopped after the second (all-dupes) page


def test_empty_first_page():
    assert paginate(lambda o: [], page_size=100, max_pages=5, key="poolId") == []


def test_respects_max_pages():
    def fetch(offset):
        # always a full unique page -> would loop forever without the cap
        base = offset
        return [{"poolId": str(base + i)} for i in range(100)]

    out = paginate(fetch, page_size=100, max_pages=3, key="poolId")
    assert len(out) == 300          # exactly max_pages * page_size
