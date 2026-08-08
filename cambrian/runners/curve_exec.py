"""Direct execution against a Pons v2 bonding curve.

Flash cannot route these. It aggregates AMMs and prices only assets it has
notional rates for, so a fresh v2 curve comes back `FailedPrecondition ...
missing notional rates for assets` — verified live. Pre-graduation, the only way
to trade a v2 token is to call its curve, which is also where every v2 token
spends the window a sniping desk cares about.

The curve's trade ABI (docs.ponsfamily.com/v2, both selectors verified present in
live curve bytecode):

    buy(uint256 quoteIn, uint256 minTokensOut, address recipient) payable
    sell(uint256 tokensIn, uint256 minQuoteOut, address recipient)

Same safety model as the Flash path: **nothing here signs and nothing takes a
private key.** These functions build unsigned transactions. Whatever holds the
key — the Flash MCP for Flash orders, a local signer for raw calls — signs them
somewhere this process cannot see.

Two mechanics that differ by launch and will silently cost money if assumed:

**Native vs ERC-20 quote.** On a native-ETH launch `quoteIn` must EQUAL
`msg.value`. On a custom-pair launch (a tokenized stock, ~60% of v2) you approve
the curve first and send zero value. Sending value on an ERC-20 launch strands
ETH in the call; approving on a native launch is a wasted transaction.

**minOut is mandatory.** `minTokensOut` / `minQuoteOut` bound the price, and
passing 0 is a standing invitation to be sandwiched — on a curve with virtual
reserves, a fresh launch is exactly where that is cheapest to do. Every builder
here computes minOut from our own quote and refuses to emit a zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import venues as V

# keccak256(signature)[:4] — pinned by tests, present in live curve bytecode.
SEL_BUY = "0x59a87bc1"
SEL_SELL = "0xd04c6983"
SEL_APPROVE = "0x095ea7b3"
SEL_ALLOWANCE = "0xdd62ed3e"

BPS = 10_000
DEFAULT_SLIPPAGE_BPS = 300      # 3%, matching the sizing cap


class CurveExecError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnsignedTx:
    """A transaction to be signed elsewhere. Deliberately carries no key."""
    to: str
    data: str
    value: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {"to": self.to, "data": self.data, "value": hex(self.value)}


def _word(x: int | str) -> str:
    if isinstance(x, str):
        return x.lower().replace("0x", "").rjust(64, "0")
    if x < 0:
        raise CurveExecError("negative value in calldata")
    return f"{x:064x}"


def encode_buy(quote_in: int, min_tokens_out: int, recipient: str) -> str:
    return SEL_BUY + _word(quote_in) + _word(min_tokens_out) + _word(recipient)


def encode_sell(tokens_in: int, min_quote_out: int, recipient: str) -> str:
    return SEL_SELL + _word(tokens_in) + _word(min_quote_out) + _word(recipient)


def encode_approve(spender: str, amount: int) -> str:
    return SEL_APPROVE + _word(spender) + _word(amount)


def allowance(client, token: str, owner: str, spender: str) -> int | None:
    try:
        raw = client.eth_call(token, SEL_ALLOWANCE + _word(owner) + _word(spender))
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def _min_out(expected: int | None, slippage_bps: int) -> int:
    """Floor an expected output by a slippage tolerance, never returning 0.

    A zero minOut removes every protection the parameter exists for. If we cannot
    quote the trade we refuse to build it rather than emitting an unbounded one.
    """
    if not expected or expected <= 0:
        raise CurveExecError(
            "cannot price this curve — refusing to build a trade with no minOut")
    out = expected * (BPS - max(slippage_bps, 0)) // BPS
    if out <= 0:
        raise CurveExecError("slippage tolerance consumes the entire output")
    return out


def is_native_quote(venue: V.Venue) -> bool:
    return (venue.quote_token or "").lower() == V.NATIVE_ETH


def build_buy(venue: V.Venue, *, quote_in: int, recipient: str,
              slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
              client=None, owner: str | None = None) -> list[UnsignedTx]:
    """Transactions to buy `quote_in` worth of a v2 token on its curve.

    Returns a list because an ERC-20-quoted launch needs an approval first. The
    caller sends them in order; on a native launch there is exactly one.
    """
    if venue.kind != V.PONS_V2:
        raise CurveExecError("curve execution only applies to Pons v2 curves")
    if venue.graduated:
        raise CurveExecError("curve has graduated — route through Flash instead")
    min_out = _min_out(V.quote_buy(venue, quote_in), slippage_bps)
    txs: list[UnsignedTx] = []
    if is_native_quote(venue):
        # quoteIn must equal msg.value; approving here would be a wasted tx.
        txs.append(UnsignedTx(to=venue.pool,
                              data=encode_buy(quote_in, min_out, recipient),
                              value=quote_in, note="buy (native ETH quote)"))
        return txs
    # ERC-20 quote (a tokenized stock on ~60% of v2 launches): approve, send no value.
    if client is not None and owner:
        have = allowance(client, venue.quote_token, owner, venue.pool)
        if have is not None and have >= quote_in:
            return [UnsignedTx(to=venue.pool,
                               data=encode_buy(quote_in, min_out, recipient),
                               value=0, note="buy (ERC-20 quote, already approved)")]
    txs.append(UnsignedTx(to=venue.quote_token,
                          data=encode_approve(venue.pool, quote_in),
                          value=0, note="approve quote asset for the curve"))
    txs.append(UnsignedTx(to=venue.pool,
                          data=encode_buy(quote_in, min_out, recipient),
                          value=0, note="buy (ERC-20 quote)"))
    return txs


def build_sell(venue: V.Venue, *, tokens_in: int, recipient: str,
               slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
               client=None, owner: str | None = None) -> list[UnsignedTx]:
    """Transactions to sell `tokens_in` of a v2 token back to its curve.

    Selling always needs the token approved to the curve, regardless of what the
    quote asset is — the curve pulls the tokens from you.
    """
    if venue.kind != V.PONS_V2:
        raise CurveExecError("curve execution only applies to Pons v2 curves")
    min_out = _min_out(V.quote_sell(venue, tokens_in), slippage_bps)
    txs: list[UnsignedTx] = []
    if client is not None and owner:
        have = allowance(client, venue.token, owner, venue.pool)
        if have is not None and have >= tokens_in:
            return [UnsignedTx(to=venue.pool,
                               data=encode_sell(tokens_in, min_out, recipient),
                               value=0, note="sell (already approved)")]
    txs.append(UnsignedTx(to=venue.token,
                          data=encode_approve(venue.pool, tokens_in),
                          value=0, note="approve token for the curve"))
    txs.append(UnsignedTx(to=venue.pool,
                          data=encode_sell(tokens_in, min_out, recipient),
                          value=0, note="sell"))
    return txs


def plan_to_txs(venue: V.Venue, plan: dict, *, recipient: str,
                slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                client=None, owner: str | None = None) -> list[UnsignedTx]:
    """Turn a `sizing.plan_entry` result into the transactions that execute it.

    Refuses a plan the gate rejected. The gate and the builder are separate
    checks on purpose, but a blocked plan must never reach calldata — that would
    make the gate advisory rather than binding.
    """
    if not plan.get("ok"):
        raise CurveExecError("plan is blocked: %s"
                             % "; ".join(plan.get("reasons") or ["unknown"]))
    quote_in = plan.get("quote_in")
    if not quote_in:
        raise CurveExecError("plan carries no sized input")
    return build_buy(venue, quote_in=quote_in, recipient=recipient,
                     slippage_bps=slippage_bps, client=client, owner=owner)
