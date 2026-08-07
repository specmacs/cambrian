"""Standalone: get a live Definitive Flash quote to buy a runner on RH Chain.

READ-ONLY. It calls POST /quote — pricing, fees, route quality — and moves no
funds. Submitting a trade needs a per-order wallet signature, which this script
never touches; use the Flash MCP (@definitive-fi/flash-mcp) for that (keys stay
in your OS keychain, never in a transcript). See docs/flash-execution.md.

  python rh_flash_quote.py 0x<token> [qtyWETH]

Env: RH_FLASH_KEY (defaults to Flash's public dev key — quoting only, can't move
funds), RH_CONTRA (spend asset, default RH WETH), RH_FUNDER (optional; without it
Flash returns pricing but an empty signing payload). Only dependency is requests.
"""

import os
import sys
import requests

BASE = "https://flash.definitive.fi/v1"
KEY = os.getenv("RH_FLASH_KEY", "dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b")
CONTRA = os.getenv("RH_CONTRA", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")  # RH WETH
FUNDER = os.getenv("RH_FUNDER", "")

token = sys.argv[1] if len(sys.argv) > 1 else os.getenv("RH_TOKEN", "")
qty = sys.argv[2] if len(sys.argv) > 2 else os.getenv("RH_QTY", "0.02")
if not token.startswith("0x") or len(token) != 42:
    raise SystemExit("usage: python rh_flash_quote.py 0x<token> [qtyWETH]")

body = {
    "targetChain": "robinhood", "contraChain": "robinhood",
    "targetAsset": token, "contraAsset": CONTRA,
    "side": "buy", "qty": str(qty), "orderType": "market",
    "maxSlippage": "0.05", "quickTrade": True,
}
if FUNDER:
    body["funderAddress"] = FUNDER

print(f"quoting BUY {token} with {qty} of {CONTRA} on robinhood (QuickTrade)...\n")
r = requests.post(f"{BASE}/quote", json=body,
                  headers={"content-type": "application/json",
                           "x-definitive-api-key": KEY}, timeout=40)
try:
    d = r.json()
except Exception:
    raise SystemExit(f"HTTP {r.status_code}: {r.text[:300]}")

if "error" in d:
    e = d["error"]
    print(f"Flash error {e.get('code')}: {e.get('message')}")
    print("(NOT_FOUND usually means no route/liquidity for this pair on RH yet.)")
    raise SystemExit

frm, to = d.get("from", {}), d.get("to", {})
fees = d.get("fees", {})
print("quote:")
print(f"  spend  {frm.get('amount')}  (${frm.get('notional')})")
print(f"  get    {to.get('amount')}  (${to.get('notional')})")
print(f"  fee    ${fees.get('estimatedFeeNotional')}   quoteId {d.get('quoteId')}")
signable = bool((d.get('evm') or {}).get('orderTypedData'))
print(f"  signable payload: {'yes' if signable else 'no (pass RH_FUNDER to get one)'}")
print("\nto EXECUTE, don't sign here — use the Flash MCP: flash_submit_order with this body.")
