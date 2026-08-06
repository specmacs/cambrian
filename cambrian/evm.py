"""Minimal EVM crypto: a pure-Python keccak256 and the helpers built on it.

No dependency provides keccak here, and Ethereum needs the original Keccak (0x01
padding), not SHA3-256. This unblocks computing function selectors, event topic0s,
and mapping storage slots ourselves — so constants are *derived and verified*, not
copied from a web search. Correctness is pinned by known test vectors in the tests.
"""

from __future__ import annotations

_MASK = (1 << 64) - 1
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]


def _rol(a: int, b: int) -> int:
    b %= 64
    return ((a << b) | (a >> (64 - b))) & _MASK


def _keccak_f(state: list[int]) -> list[int]:
    lanes = [[state[x + 5 * y] for y in range(5)] for x in range(5)]
    for rnd in range(24):
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4]
             for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                lanes[x][y] ^= d[x]
        x, y = 1, 0
        cur = lanes[x][y]
        for t in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            cur, lanes[x][y] = lanes[x][y], _rol(cur, (t + 1) * (t + 2) // 2)
        for yy in range(5):
            row = [lanes[xx][yy] for xx in range(5)]
            for xx in range(5):
                lanes[xx][yy] = row[xx] ^ ((~row[(xx + 1) % 5]) & row[(xx + 2) % 5])
        lanes[0][0] ^= _RC[rnd]
    return [lanes[x][y] for y in range(5) for x in range(5)]


def keccak256(data: bytes) -> bytes:
    rate = 136  # bytes (1088-bit rate, 512-bit capacity)
    s = [0] * 25
    m = bytearray(data)
    m.append(0x01)
    while len(m) % rate:
        m.append(0x00)
    m[-1] ^= 0x80
    for off in range(0, len(m), rate):
        for i in range(rate // 8):
            s[i] ^= int.from_bytes(m[off + i * 8:off + i * 8 + 8], "little")
        s = _keccak_f(s)
    return b"".join(lane.to_bytes(8, "little") for lane in s)[:32]


def selector(signature: str) -> str:
    """4-byte function selector, e.g. selector('extsload(bytes32)') -> '0x1e2eaeaf'."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def topic0(signature: str) -> str:
    """Event topic0, e.g. topic0('Swap(address,address,int256,...)')."""
    return "0x" + keccak256(signature.encode()).hex()


def mapping_slot(key32: bytes, slot: int) -> int:
    """Storage slot of mapping[key] where the mapping is declared at `slot`:
    keccak256(abi.encode(key, slot)). `key32` must be 32 bytes."""
    return int.from_bytes(keccak256(key32 + slot.to_bytes(32, "big")), "big")
