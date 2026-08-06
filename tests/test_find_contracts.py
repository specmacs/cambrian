from cambrian.runners.feed import find_contracts
from cambrian.runners.uniswap_v3 import POOLCREATED_TOPIC0
from cambrian.runners.uniswap_v4 import INITIALIZE_TOPIC0

FACTORY = "0xFACt0000000000000000000000000000000000f3"
PM = "0xP001Manager000000000000000000000000000004"


class _Fake:
    def get_logs(self, *, topics, from_block, to_block="latest"):
        if topics[0] == POOLCREATED_TOPIC0:
            return [{"address": FACTORY}, {"address": FACTORY},
                    {"address": "0xImposter00000000000000000000000000000001"}]
        if topics[0] == INITIALIZE_TOPIC0:
            return [{"address": PM}, {"address": PM}, {"address": PM}]
        return []


def test_find_contracts_tallies_emitters():
    out = find_contracts(_Fake(), from_block="0x0")
    # most frequent v3 emitter is the real factory
    v3 = out["v3_factory"]
    assert max(v3, key=v3.get) == FACTORY.lower()
    # PoolManager surfaces from Initialize emitters
    assert list(out["pool_manager"]) == [PM.lower()]
    assert out["pool_manager"][PM.lower()] == 3
