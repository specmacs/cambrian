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


def test_uniswap_launcher_is_never_labeled_as_a_pad():
    # 0x0000ffff.. is Uniswap's Liquidity Launcher on RH — shared infrastructure
    # every pad routes through. We used to call it "pons", which stamped a trusted
    # name on launches from any pad at all. It must stay an unnamed fingerprint.
    got = pad_of("0xtok", None,
                 deployer="0x0000ffffbe8efe702c8703ae3477ff5de3d319c0")
    assert got != "pons"
    assert got.startswith("dep:")


def test_strategy_outranks_a_spoofable_suffix():
    # A token whose address was ground to end in 'ba3' still gets identified by
    # the strategy the launcher actually distributed through, not the vanity suffix.
    assert pad_of("0xdeadbeef00000000000000000000000000000ba3", None,
                  strategy="0x05d552391067389ee44fec3924157ed33f976000") == "lbp"


def test_unnamed_strategy_is_still_a_stable_verified_label():
    assert pad_of("0xtok", None, strategy="0xabc1230000000000000000000000000000000000") \
        == "strat:abc123"


def test_pools_trade_identified_by_its_strategy():
    # Chain-verified: FRONG, the pools.trade flagship, was distributed through this
    # strategy on the v3.0.0 launcher. 0x58daec.. used to carry the "pools-trade"
    # label on a guess about who deployed FRONG's pools; reading the launch tx
    # showed it is just a token in that tx, so it must no longer name a pad.
    assert pad_of("0xtok", None,
                  strategy="0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491") == "pools-trade"
    assert pad_of("0xtok", None,
                  deployer="0x58daec3116aae6d93017baaea7749052e8a04fa7") != "pools-trade"


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


# --- Independent pads: Pons and flap run their own factories -----------------
# These pads never appear in Uniswap's TokenCreated/TokenDistributed, so they are
# discovered via their own factory's emitter+topic0. Only the pad can emit at the
# pad's own address, so the pair is unforgeable.

def test_independent_pads_have_a_launch_event_so_discover_fresh_runs_them():
    # discover_fresh SKIPS any pad with a blank created_topic0 (fail closed). A
    # blank here silently drops the pad's entire launch flow, which is how Pons
    # and flap went unwatched.
    from cambrian.runners.config import LAUNCHPADS
    for name in ("pons", "flap"):
        pad = LAUNCHPADS[name]
        assert pad["address"], name
        topic0 = pad["created_topic0"]
        assert topic0.startswith("0x") and len(topic0) == 66, name


def test_pons_token_deployed_topic0_matches_keccak_of_its_signature():
    # The topic0 is not copied from a block explorer — it is the keccak256 of the
    # recovered signature, so a typo cannot survive this test.
    from cambrian.runners.config import EVT_PONS_TOKEN_DEPLOYED
    sig = b"TokenDeployed(address,address,address,address,uint256,uint256)"
    assert EVT_PONS_TOKEN_DEPLOYED == "0x" + _keccak(sig).hex()


def test_pons_emits_a_literal_token_launched_event():
    # Guards a correction: an earlier session concluded "there is no TokenLaunched
    # event, do not go hunting for it again". True for Uniswap's launcher, false in
    # general — Pons's own factory emits one, and this pins its exact signature.
    from cambrian.runners.config import EVT_PONS_TOKEN_LAUNCHED
    sig = (b"TokenLaunched(address,address,address,address,address,"
           b"uint256,uint256,uint256,uint256,uint256)")
    assert EVT_PONS_TOKEN_LAUNCHED == "0x" + _keccak(sig).hex()


def test_pons_and_flap_factories_are_distinct_from_the_uniswap_launcher():
    # Labelling shared Uniswap infrastructure as a pad is the bug that let
    # unverified contracts through the gate; these must never collide with it.
    from cambrian.runners.config import LAUNCHPADS, UNI_LAUNCHERS
    infra = {a.lower() for a in UNI_LAUNCHERS.values()}
    for name in ("pons", "flap"):
        assert LAUNCHPADS[name]["address"].lower() not in infra, name


def _keccak(data: bytes) -> bytes:
    """Pure-python keccak256, borrowed from examples/rh_codehash.py.

    No dependency in this project provides it, and the point of these tests is to
    prove the topic0s independently rather than trust a transcribed constant.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "examples" / "rh_codehash.py"
    text = src.read_text()
    ns: dict = {}
    exec(text[text.index("_M = (1 << 64)"):text.index("def rpc(")], ns)
    return ns["kec"](data)
