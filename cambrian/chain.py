"""Read-only JSON-RPC client for Robinhood Chain.

Deliberately thin and dependency-light: raw JSON-RPC over `requests`, no web3.
It only *reads* — no key ever touches this module. Writing (signing and
broadcasting transactions) is the live-execution seam in `execution.py` and is
not built here.

Why hand-rolled instead of web3: the read surface the desks need is tiny
(chainId, balances, a couple of ERC-20 view calls), and keeping it dependency-
light means `status` and the fact-fetchers run anywhere with nothing but
`requests`. Anything needing arbitrary ABI encoding (which needs keccak) is
out of scope here on purpose — see the note by the selector table.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from . import config

# Function selectors are the first 4 bytes of keccak256(signature). Computing
# them needs a keccak implementation, which we intentionally don't pull in.
# These three are the canonical, universally-known ERC-20 view selectors — safe
# to hardcode. If you need a selector that isn't here, add keccak (e.g.
# eth-utils) rather than guessing a value.
SELECTOR = {
    "totalSupply()": "0x18160ddd",
    "balanceOf(address)": "0x70a08231",
    "decimals()": "0x313ce567",
}


class RpcError(RuntimeError):
    pass


class ChainClient:
    def __init__(self, url: str | None = None, *, timeout: float = 5.0,
                 max_retries: int = 4):
        self.url = url or config.RPC_URL
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        if not self.url:
            raise RpcError("RPC_URL is not set (fail closed: no chain access)")

    def _call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(self.url, json=body, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    # An RPC-level error is not a transient network fault; don't
                    # retry it, surface it.
                    raise RpcError(f"{method}: {data['error']}")
                return data["result"]
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s, ...
        raise RpcError(f"{method} failed after {self.max_retries} attempts: {last_exc}")

    # --- Basic reads --------------------------------------------------------

    def chain_id(self) -> int:
        return int(self._call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self._call("eth_blockNumber", []), 16)

    def get_code(self, address: str) -> str:
        return self._call("eth_getCode", [address, "latest"])

    def is_contract(self, address: str) -> bool:
        code = self.get_code(address)
        return code not in ("0x", "0x0", "")

    def eth_call(self, to: str, data: str) -> str:
        return self._call("eth_call", [{"to": to, "data": data}, "latest"])

    # --- ERC-20 view helpers -----------------------------------------------

    def erc20_decimals(self, token: str) -> int:
        return int(self.eth_call(token, SELECTOR["decimals()"]), 16)

    def erc20_total_supply(self, token: str) -> int:
        return int(self.eth_call(token, SELECTOR["totalSupply()"]), 16)

    def erc20_balance_of(self, token: str, holder: str) -> int:
        # balanceOf(address): selector + 32-byte, left-padded holder address.
        padded = holder.lower().replace("0x", "").rjust(64, "0")
        return int(self.eth_call(token, SELECTOR["balanceOf(address)"] + padded), 16)

    # --- Health -------------------------------------------------------------

    def verify_chain(self) -> None:
        """Fail closed if the RPC is pointed at the wrong network."""
        seen = self.chain_id()
        if seen != config.CHAIN_ID:
            raise RpcError(
                f"connected to chain {seen}, expected {config.CHAIN_ID} "
                f"({config.CHAIN_NAME}). Refusing to proceed."
            )
