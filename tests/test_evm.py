from cambrian.evm import keccak256, mapping_slot, selector, topic0
from cambrian.runners.uniswap_v3 import SWAP_TOPIC0, POOLCREATED_TOPIC0
from cambrian.runners import uniswap_v4 as v4


def test_keccak_known_vectors():
    assert keccak256(b"").hex() == \
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == \
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


def test_selector():
    # transfer(address,uint256) -> 0xa9059cbb is the textbook ERC-20 selector
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    assert selector("extsload(bytes32)") == "0x1e2eaeaf"


def test_hardcoded_topics_are_correct():
    # Every topic0 in the codebase must equal keccak of its event signature.
    assert topic0("Swap(address,address,int256,int256,uint160,uint128,int24)") == SWAP_TOPIC0
    assert topic0("PoolCreated(address,address,uint24,int24,address)") == POOLCREATED_TOPIC0
    assert topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)") \
        == v4.INITIALIZE_TOPIC0
    assert topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)") \
        == v4.SWAP_TOPIC0


def test_mapping_slot_matches_solidity():
    # keccak256(abi.encode(key, slot)) for a bytes32 key.
    key = (7).to_bytes(32, "big")
    got = mapping_slot(key, 6)
    want = int.from_bytes(keccak256(key + (6).to_bytes(32, "big")), "big")
    assert got == want


class _FakeClient:
    """Returns preset extsload words keyed by the storage slot in the calldata."""
    def __init__(self, by_slot):
        self.by_slot = by_slot
    def eth_call(self, to, data):
        slot = data[-64:]  # last 32 bytes of calldata = the slot
        return "0x" + self.by_slot.get(slot, format(0, "064x"))


def test_v4_liquidity_read_and_usd():
    pid = "0x" + "ab" * 32
    base = v4._state_base_slot(pid)
    slot0_key = base.to_bytes(32, "big").hex()
    liq_key = (base + 3).to_bytes(32, "big").hex()
    sqrt = 1 << 96                      # price parity
    liq = 10 * 10**18                   # 10 units of L
    client = _FakeClient({
        slot0_key: format(sqrt, "064x"),           # slot0 low 160 bits = sqrtPrice
        liq_key: format(liq, "064x"),
    })
    L, s = v4.read_pool_liquidity_sqrt(client, "0xpm", pid)
    assert L == liq and s == sqrt
    # quote is token1: reserve = L*sqrt/2^96 = L; usd = L/1e18 * price * 2
    usd = v4.liquidity_usd_from(L, s, quote_is_token0=False, quote_price_usd=3000.0)
    assert abs(usd - (10 * 3000.0 * 2)) < 1e-6


def test_v4_liquidity_none_when_empty():
    assert v4.liquidity_usd_from(0, 1 << 96, False, 3000.0) is None
