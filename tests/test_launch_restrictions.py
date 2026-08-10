"""Pons launch restrictions — why a legitimate token can look like a honeypot.

Pons holds sells for a window after launch (anti-sniper, not a scam). Inside it
the token is real, the pad is on the verified roster, the tax is 0%, and a sell
still reverts. Buying at t=0 therefore buys a position that cannot be exited,
which is exactly what happened to the first live scanner entry.

`restrictionsEndBlock` is in the launch event and was documented in config and
read by nothing.
"""

from cambrian.runners import scanner as SC


def test_a_launch_inside_its_restriction_window_is_restricted():
    hit = {"restrictions_end_block": 31_000_500}
    assert SC._restricted(hit, 31_000_000) == 500


def test_a_launch_past_its_window_is_free():
    assert SC._restricted({"restrictions_end_block": 31_000_000}, 31_000_400) == 0


def test_a_launch_with_no_restriction_field_is_free():
    # Absent means unrestricted, not "restricted by an unknown amount" — pads
    # other than Pons do not emit this at all.
    assert SC._restricted({}, 31_000_000) == 0
    assert SC._restricted({"restrictions_end_block": 0}, 31_000_000) == 0


def test_the_restriction_is_decoded_from_the_real_event_layout():
    # TokenLaunched(token, deployer, dexFactory | pairToken, pool, dexId,
    #               launchConfigId, positionId, restrictionsEndBlock,
    #               initialBuyAmount) — restrictionsEndBlock is data word 5.
    words = [
        "11" * 32,                                    # 0 pairToken
        "%064x" % 0xABCD,                             # 1 pool
        "%064x" % 3,                                  # 2 dexId
        "%064x" % 7,                                  # 3 launchConfigId
        "%064x" % 99,                                 # 4 positionId
        "%064x" % 31_002_222,                         # 5 restrictionsEndBlock
        "%064x" % (2 * 10 ** 17),                     # 6 initialBuyAmount
    ]
    data = "0x" + "".join(words)

    def word(i):
        return int(data[2:][i * 64:(i + 1) * 64], 16)

    assert word(5) == 31_002_222
    assert word(6) == 2 * 10 ** 17
