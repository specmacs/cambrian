"""Optional LLM judgment layer — the runner decision by a model, not heuristics.

The deterministic scorer is a cheap PRE-FILTER (so we don't spend a model call on
every dead pool); this asks a model to make the actual buy/skip call on each
survivor, given the on-chain facts. OpenAI-compatible, so it works with Surplus
Intelligence (an inference marketplace) or any OpenAI-style endpoint — set the
base URL and key.

Pure parts (prompt build, verdict parse) are unit-tested. The HTTP call is a thin
seam. FAIL CLOSED: an error, a timeout, or an unparseable reply is treated as
SKIP — the model must clearly say buy, or no buy. The key is read from the
environment by the caller; it never lives in this module or a transcript.
"""

from __future__ import annotations

import json

SURPLUS_BASE_URL = "https://api.surplusintelligence.ai/v1"

SYSTEM = (
    "You are a risk judge for sniping brand-new memecoins on Robinhood Chain. "
    "You are given on-chain facts about a fresh pool. Decide BUY only when real "
    "money is genuinely flowing in and the launch is not a bot-sniped or farmed "
    "rug. Be strict: most fresh launches are skips. Never invent facts not given. "
    'Reply with ONLY compact JSON: {"decision":"buy"|"skip","confidence":0..1,'
    '"reason":"<=12 words"}.'
)


def judge_messages(facts: dict) -> list[dict]:
    """Build the chat messages for a candidate. `facts` is plain on-chain data."""
    lines = [f"{k}: {v}" for k, v in facts.items()]
    user = "Fresh RH pool. Buy or skip?\n" + "\n".join(lines)
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


def parse_verdict(text: str) -> dict:
    """Extract {decision, confidence, reason} from a model reply. Fail closed:
    anything unparseable or not an explicit 'buy' becomes a skip."""
    default = {"decision": "skip", "confidence": 0.0, "reason": "unparseable"}
    if not text:
        return default
    s = text.strip()
    # tolerate code fences / prose around the JSON
    if "{" in s and "}" in s:
        s = s[s.index("{"): s.rindex("}") + 1]
    try:
        d = json.loads(s)
    except Exception:
        return default
    decision = str(d.get("decision", "skip")).lower().strip()
    if decision != "buy":
        decision = "skip"
    try:
        conf = max(0.0, min(float(d.get("confidence", 0.0)), 1.0))
    except Exception:
        conf = 0.0
    return {"decision": decision, "confidence": conf,
            "reason": str(d.get("reason", ""))[:120]}


def llm_judge(facts: dict, *, base_url: str, api_key: str, model: str,
              timeout: float = 20.0, min_confidence: float = 0.0) -> dict:
    """Ask the model to judge a candidate. Returns a parsed verdict (fail-closed to
    skip on any error). Network seam — the caller supplies base_url/key/model."""
    import requests
    try:
        r = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "content-type": "application/json"},
            json={"model": model, "messages": judge_messages(facts),
                  "temperature": 0, "max_tokens": 120},
            timeout=timeout)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return {"decision": "skip", "confidence": 0.0, "reason": f"llm error: {e}"[:120]}
    v = parse_verdict(text)
    if v["decision"] == "buy" and v["confidence"] < min_confidence:
        return {**v, "decision": "skip", "reason": f"low conf {v['confidence']:.2f}"}
    return v
