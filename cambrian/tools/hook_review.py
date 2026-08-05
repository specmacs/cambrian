"""Claude-assisted first pass over a Uniswap v4 hook's source.

`config.REVIEWED_HOOKS` is the degen desk's hard gate: a pool whose hook you have
not personally read is rejected. Reading hook Solidity is slow and easy to do
carelessly, so this tool gives you a structured first pass — Claude reads the
source and flags the ways a hook can hurt you (blocking or taxing swaps,
reentrancy, owner backdoors, upgradeability, unbounded approvals, callback
abuse). It exists so your own review starts from a map instead of a blank file.

It does NOT add anything to REVIEWED_HOOKS, and it never will. A model summary is
an aid to human review, not a substitute for it — the whole point of that set is
that *you* read the source. Treat a clean report as "nothing jumped out", not as
"safe".

This is the one place the `anthropic` dependency is used. If it isn't installed,
or no API key is configured, the desk is unaffected — this is an offline aid.
"""

from __future__ import annotations

import os

import requests

from .. import config

MODEL = "claude-opus-5"

_SYSTEM = """\
You are a smart-contract security reviewer helping a solo trader decide whether a \
Uniswap v4 hook is safe enough to provide liquidity or trade against. You are the \
FIRST pass before a human reads the source line by line — your job is to surface \
risks precisely, not to bless the contract.

Given the Solidity source of a v4 hook, report:

1. What lifecycle callbacks it implements (beforeSwap, afterSwap, beforeAddLiquidity,
   etc.) and what each one actually does.
2. Ways the hook can harm a counterparty, each rated high / medium / low:
   - blocking, reverting, or arbitrarily taxing swaps or withdrawals
   - taking a fee or skimming value on swaps / liquidity events
   - reentrancy or external calls into untrusted addresses
   - owner/admin privileges, pausability, or upgradeability (proxy, delegatecall)
   - unbounded token approvals or transfers it can trigger
   - anything that lets the deployer change the rules after you're in
3. A short, blunt bottom line: the specific things the human MUST verify by hand
   before trusting this hook.

Be concrete and cite function names. If the source is truncated, partial, or looks
like it's missing imported logic, say so — do not guess in the trader's favor. End \
with the exact line: "This is an assist, not an approval — read the source yourself."\
"""


def fetch_verified_source(address: str) -> str | None:
    """Best-effort fetch of verified source from the Blockscout explorer. Returns
    None if unavailable — an unverified contract is itself a finding worth a
    hard no."""
    try:
        resp = requests.get(
            f"{config.EXPLORER}/api",
            params={"module": "contract", "action": "getsourcecode", "address": address},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        if not result:
            return None
        entry = result[0] if isinstance(result, list) else result
        source = entry.get("SourceCode") or entry.get("source_code")
        return source or None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def review_hook_source(source: str, *, label: str = "hook") -> str:
    """Stream a structured risk review of `source` from Claude and return it.

    Requires the `anthropic` package and a configured API key. Raises a clear
    RuntimeError otherwise, so the caller can degrade gracefully — this is never
    on the trading path.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "the `anthropic` package is required for hook review "
            "(`pip install anthropic`)"
        ) from exc

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / an `ant` profile
    prompt = (
        f"Review this Uniswap v4 hook ({label}). Solidity source follows.\n\n"
        f"```solidity\n{source}\n```"
    )
    try:
        # Stream: a thorough review can be long, and streaming avoids the SDK's
        # long-request timeout. Adaptive thinking lets the model reason about the
        # callbacks before writing.
        with client.messages.stream(
            model=MODEL,
            max_tokens=8000,
            system=_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:  # pragma: no cover - network path
        raise RuntimeError(f"hook review API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:  # pragma: no cover - network path
        raise RuntimeError(f"hook review connection error: {exc}") from exc

    return "".join(block.text for block in message.content if block.type == "text")


def review_hook(target: str) -> str:
    """Resolve `target` (a local .sol path or an on-chain address) to source and
    review it."""
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as fh:
            source = fh.read()
        label = os.path.basename(target)
    else:
        source = fetch_verified_source(target)
        if not source:
            raise RuntimeError(
                f"no verified source for {target} on {config.EXPLORER}. "
                f"An unverified hook is a hard no on its own — do not trade against it."
            )
        label = target
    return review_hook_source(source, label=label)
