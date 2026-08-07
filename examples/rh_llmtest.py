"""One-shot Surplus Intelligence connection test. Confirms the endpoint, key, and
model before you run the auto-trader with the LLM decider.

  set RH_LLM_KEY (your inf_... key) in the shell, then:  python examples/rh_llmtest.py

Reads RH_LLM_KEY / RH_LLM_BASE / RH_LLM_MODEL from the env (same vars the
auto-trader uses), sends a trivial prompt, and prints the raw response so any
error is visible. Only dependency is requests. The key is read from the env — it
is never printed.
"""

import os
import requests

KEY = os.getenv("RH_LLM_KEY", "")
BASE = os.getenv("RH_LLM_BASE", "https://api.surplusintelligence.ai/min30/v1")
MODEL = os.getenv("RH_LLM_MODEL", "claude-opus-4.7")

if not KEY:
    raise SystemExit("set RH_LLM_KEY in your shell first (your inf_... key)")

url = BASE.rstrip("/") + "/chat/completions"
print(f"POST {url}  model={MODEL}")
try:
    r = requests.post(
        url,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        json={"model": MODEL,
              "messages": [{"role": "user", "content": "reply with one word: ok"}],
              "max_tokens": 20, "stream": False},
        timeout=40)
except Exception as e:
    raise SystemExit(f"request failed: {e}")

print("HTTP", r.status_code)
print(r.text[:2000])
if r.status_code == 200:
    try:
        print("\nparsed reply:", r.json()["choices"][0]["message"]["content"])
        print("connection OK — you can run the auto-trader with RH_LLM=1")
    except Exception:
        print("\n(200 but unexpected JSON shape — paste the body above to me)")
