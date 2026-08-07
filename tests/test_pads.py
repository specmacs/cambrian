from cambrian.runners.config import pad_of

BANKR_HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"


def test_bankr_identified_by_hook():
    # hook match is authoritative, regardless of token suffix
    assert pad_of("0x1111111111111111111111111111111111111111", BANKR_HOOK) == "bankr"


def test_bankr_identified_by_suffix():
    # no hook (or a different one), but the vanity 'ba3' suffix still tags it
    assert pad_of("0xa3b6aee90017b72c0812dc1e013de70eb2917ba3", None) == "bankr"
    assert pad_of("0xa3b6aee90017b72c0812dc1e013de70eb2917BA3", None) == "bankr"


def test_unknown_hook_gets_a_stable_fingerprint_label():
    # a launch through an unnamed hook is still labeled by that hook, so it groups
    lbl = pad_of("0xabc0000000000000000000000000000000000abc",
                 "0x9999abcd000000000000000000000000000000ff")
    assert lbl == "hook:9999ab"


def test_unknown_deployer_gets_a_stable_fingerprint_label():
    lbl = pad_of("0xabc0000000000000000000000000000000000abc", None,
                 deployer="0x1234dead000000000000000000000000000000ff")
    assert lbl == "dep:1234de"


def test_named_deployer_wins_over_fingerprint():
    from cambrian.runners.config import PAD_BY_DEPLOYER
    PAD_BY_DEPLOYER["0xfeed000000000000000000000000000000000001"] = "noxa"
    try:
        assert pad_of("0xtok", None, deployer="0xFEED000000000000000000000000000000000001") == "noxa"
    finally:
        del PAD_BY_DEPLOYER["0xfeed000000000000000000000000000000000001"]


def test_flap_identified_by_7777_suffix():
    # Flap grinds 7777 token addresses (hookless) -> the '777' 3-char cluster
    assert pad_of("0x20024e485c0b22b42855589700721b28320a7777", None) == "flap"


def test_pons_identified_by_deployer():
    assert pad_of("0xtok", None,
                  deployer="0x0000ffffbe8efe702c8703ae3477ff5de3d319c0") == "pons"


def test_pools_trade_identified_by_deployer():
    assert pad_of("0xtok", None,
                  deployer="0x58daec3116aae6d93017baaea7749052e8a04fa7") == "pools-trade"


def test_infra_deployers_are_not_labeled_as_pads():
    # EntryPoint / Multicall3 / PoolManager route many pads' launches — not a pad.
    for infra in ("0x0000000071727de22e5e9d8baf0edac6f37da032",
                  "0xca11bde05977b3631167028862be2a173976ca11",
                  "0x8366a39cc670b4001a1121b8f6a443a643e40951"):
        assert pad_of("0xtok", None, deployer=infra) is None


def test_truly_unfingerprintable_is_none():
    # hookless, no deployer, no suffix -> nothing to group on
    assert pad_of("0xabc0000000000000000000000000000000000abc",
                  "0x0000000000000000000000000000000000000000") is None
    assert pad_of(None, None) is None
