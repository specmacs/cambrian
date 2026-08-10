"""Provenance — the check whose absence bought a honeypot from a "verified pad".

pools.trade launches were being recognised as "a hookless Uniswap v4 pool paired
with WETH", and anyone can create one of those. flap was "any V2 pair with
WETH", same problem. Both labels were inferences dressed up as provenance, and
the gate trusted them.

`config.py` already stated the invariant: only the launcher can emit logs at the
launcher's own address, so membership in TokenCreated cannot be forged the way a
pool shape or an address suffix can. UNI_LAUNCHER_ADDRESSES and FLAP_PORTAL were
defined for exactly this and read by nothing.
"""

from cambrian.runners import config as rcfg
from cambrian.runners import scanner as SC

REAL = "0x1111111111111111111111111111111111111111"
FAKE = "0x2222222222222222222222222222222222222222"


class FakeClient:
    """Serves the launcher's TokenCreated for REAL only."""

    def __init__(self, indexed=True):
        self.indexed = indexed

    def get_logs(self, *, address, topics, from_block, to_block):
        if address == rcfg.UNI_LAUNCHER_ADDRESSES[0] and topics == [rcfg.EVT_TOKEN_CREATED]:
            if self.indexed:
                return [{"topics": [rcfg.EVT_TOKEN_CREATED, "0x" + "0" * 24 + REAL[2:]],
                         "data": "0x"}]
            return [{"topics": [rcfg.EVT_TOKEN_CREATED],
                     "data": "0x" + "0" * 24 + REAL[2:]}]
        if address == rcfg.FLAP_PORTAL:
            return []
        return []


def test_a_launcher_token_is_verified():
    verified, _f = SC._provenance(FakeClient(), 1000, 2000)
    assert REAL.lower() in verified["pools-trade"]


def test_a_pool_nobody_launched_is_not_verified():
    # The honeypot's exact shape: a real v4 pool, no launch event anywhere.
    verified, _f = SC._provenance(FakeClient(), 1000, 2000)
    assert FAKE.lower() not in verified["pools-trade"]


def test_the_token_is_read_whether_or_not_it_is_indexed():
    # The four launcher generations are not guaranteed to agree on this, and
    # reading only the topic would silently verify nothing on the others.
    for indexed in (True, False):
        verified, _f = SC._provenance(FakeClient(indexed=indexed), 1000, 2000)
        assert REAL.lower() in verified["pools-trade"], indexed


def test_provenance_looks_further_back_than_the_sweep():
    # A pool can be initialised well after its token was created, and a launch
    # that scrolled out of the discovery window is still a real launch.
    seen = {}

    class Recorder(FakeClient):
        def get_logs(self, *, address, topics, from_block, to_block):
            seen.setdefault("lo", int(from_block, 16))
            return []

    SC._provenance(Recorder(), 100_000, 100_400)
    assert seen["lo"] < 100_000


def test_every_pad_gets_a_provenance_bucket_it_can_be_checked_against():
    verified, _f = SC._provenance(FakeClient(), 1000, 2000)
    assert set(verified) == {"pools-trade", "flap"}
