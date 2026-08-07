from cambrian.runners.config import pad_of

BANKR_HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"


def test_bankr_identified_by_hook():
    # hook match is authoritative, regardless of token suffix
    assert pad_of("0x1111111111111111111111111111111111111111", BANKR_HOOK) == "bankr"


def test_bankr_identified_by_suffix():
    # no hook (or a different one), but the vanity 'ba3' suffix still tags it
    assert pad_of("0xa3b6aee90017b72c0812dc1e013de70eb2917ba3", None) == "bankr"
    assert pad_of("0xa3b6aee90017b72c0812dc1e013de70eb2917BA3", None) == "bankr"


def test_unknown_pad_is_none():
    assert pad_of("0xabc0000000000000000000000000000000000abc",
                  "0x0000000000000000000000000000000000000000") is None
    assert pad_of(None, None) is None
