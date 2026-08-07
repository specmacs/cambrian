"""Autonomous runner sniper for RH Chain. Watches, buys HOT runners, and manages
the exit ladder — stop-loss, pull-initials at 2x, ride a free moon bag.

RUN FROM THE REPO ROOT:  python examples/rh_autotrade.py

SAFETY, READ THIS:
  * Ships in DRY-RUN. It only PRINTS trades until you set RH_LIVE=1. Watch it
    paper-trade first.
  * The private key is read from the RH_FUNDER_PK env var on THIS machine only.
    Never put it on a command line that gets logged, never paste it into a chat,
    never commit it. Use a DEDICATED burner wallet funded with only what you'll
    risk. This script never prints or transmits the key anywhere but Flash's
    signing (local).
  * An auto-sniper on fresh memecoins loses money by default. Defaults are strict
    (bankr-only, HOT-only, deep-liquidity-only, tiny size, hard caps, daily
    loss kill-switch). Loosen knowingly.
  * Do ONE tiny trade by hand through the Flash MCP first to confirm the one-time
    WETH approval and that RH routing works. Then run this with small caps.

Deps: requests, eth-account (live only). Reuses the tested package (chain read,
scoring, guardrails, exit ladder) so the money logic is the same code the unit
tests cover.
"""

import json
import os
import time

from cambrian.chain import ChainClient
from cambrian.runners.feed import scan_live
from cambrian.runners.score import flow_score
from cambrian.runners.autotrade import (TradePolicy, Wallet, record_buy,
                                        record_close, should_buy)
from cambrian.runners import config as rcfg
from cambrian.runners import flash

# ---- config (env-overridable) -----------------------------------------------
INTERVAL = int(os.getenv("RH_INTERVAL", "30"))
BLOCKS = int(os.getenv("RH_BLOCKS", "6000"))          # scan window each tick (~10 min)
WINDOW = int(os.getenv("RH_WINDOW_BLOCKS", "3000"))
WETH_USD = float(os.getenv("RH_WETH_USD", "3000"))
STATE_FILE = os.getenv("RH_TRADE_STATE", os.path.expanduser("~/rh_autotrade_state.json"))
LIVE = os.getenv("RH_LIVE", "") not in ("", "0", "false")
CONTRA = os.getenv("RH_CONTRA", rcfg.CONTRACTS["weth"])   # spend asset
FLASH_KEY = os.getenv("RH_FLASH_KEY", flash.FLASH_DEV_KEY)

POLICY = TradePolicy(
    spend_per_trade=float(os.getenv("RH_SPEND", "0.02")),
    max_total_deployed=float(os.getenv("RH_MAX_TOTAL", "0.10")),
    max_positions=int(os.getenv("RH_MAX_POS", "5")),
    min_score=float(os.getenv("RH_MIN_SCORE", "0.60")),          # HOT
    require_pad=os.getenv("RH_PAD", "bankr").lower() or None,     # bankr-only by default
    min_liquidity_usd=float(os.getenv("RH_MIN_LIQ", "20000")),
    cooldown_seconds=float(os.getenv("RH_COOLDOWN", "60")),
    daily_loss_limit=float(os.getenv("RH_DAILY_LOSS", "0.04")),
)
PLAN = flash.ExitPlan(
    stop_loss_pct=float(os.getenv("RH_STOP_PCT", "0.30")),
    tp_mult=float(os.getenv("RH_TP_MULT", "2.0")),
    moon_stop_pct=float(os.getenv("RH_MOON_STOP_PCT", "0.0")),   # breakeven
    moon_tp_mult=float(os.getenv("RH_MOON_TP_MULT", "10.0")),
)


# ---- Flash execution (live only; pure sign+POST, Flash lands the tx) ---------
def _funder_and_signer():
    from eth_account import Account
    pk = os.getenv("RH_FUNDER_PK", "")
    if not pk:
        raise SystemExit("RH_LIVE=1 but RH_FUNDER_PK is not set. Export your burner "
                         "wallet key in this shell only; never paste it anywhere.")
    acct = Account.from_key(pk)

    def sign_typed(typed_json):
        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(full_message=json.loads(typed_json))
        sig = Account.sign_message(signable, private_key=pk).signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig
    return acct.address, sign_typed


def _flash_post(path, body):
    import requests
    r = requests.post(flash.FLASH_BASE_URL + path, json=body, timeout=40,
                      headers={"content-type": "application/json",
                               "x-definitive-api-key": FLASH_KEY})
    return r.json()


def _flash_get(path):
    import requests
    r = requests.get(flash.FLASH_BASE_URL + path, timeout=40,
                     headers={"x-definitive-api-key": FLASH_KEY})
    return r.json()


def execute_buy(token, spend, funder, sign_typed):
    """quote -> sign -> order -> poll fill. Returns (entry_usd, qty_tokens) or None."""
    q = _flash_post("/quote", flash.quote_body(token, contra=CONTRA, qty=str(spend),
                                               funder=funder))
    if "error" in q:
        print(f"    quote error: {q['error'].get('code')} {q['error'].get('message')}")
        return None
    evm = q.get("evm") or {}
    if evm.get("approveTx"):
        print("    one-time WETH approval not set — do it once via the Flash MCP, then rerun")
        return None
    if evm.get("permitTypedData"):
        print("    quote wants Permit2 — set a direct WETH approval via the MCP instead")
        return None
    typed = evm.get("orderTypedData")
    if not typed:
        print("    no signable payload returned")
        return None
    o = _flash_post("/order", {**flash.quote_body(token, contra=CONTRA, qty=str(spend),
                                                  funder=funder),
                               "quoteId": q["quoteId"], "userSignature": sign_typed(typed),
                               "evmOrderTypedData": typed})
    if "error" in o or not o.get("orderId"):
        print(f"    order error: {o.get('error')}")
        return None
    oid = o["orderId"]
    for _ in range(30):                       # poll up to ~30s for the fill
        d = _flash_get(f"/orders/{oid}")
        f = (d.get("order") or d).get("filled")
        st = (d.get("order") or d).get("status", "")
        if f and f.get("averageNotionalPrice"):
            return float(f["averageNotionalPrice"]), float(f["targetAmount"])
        if st.endswith(("REJECTED", "CANCELLED", "TERMINATED")):
            print(f"    order {st}")
            return None
        time.sleep(1)
    print("    fill not confirmed in time; check the order manually")
    return None


def place_exit(body, sign_typed):
    q = _flash_post("/quote", body)
    typed = ((q.get("evm") or {}).get("orderTypedData"))
    if "error" in q or not typed:
        return False
    o = _flash_post("/order", {**body, "quoteId": q["quoteId"],
                               "userSignature": sign_typed(typed), "evmOrderTypedData": typed})
    return bool(o.get("orderId"))


# ---- state ------------------------------------------------------------------
def load_wallet():
    w = Wallet()
    try:
        with open(STATE_FILE) as fh:
            s = json.load(fh)
        w.positions = s.get("positions", {})
        w.spent = s.get("spent", 0.0)
        w.realized_pnl = s.get("realized_pnl", 0.0)
        w.last_buy_ts = s.get("last_buy_ts", 0.0)
        w.seen = set(s.get("seen", []))
    except Exception:
        pass
    return w


def save_wallet(w):
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({"positions": w.positions, "spent": w.spent,
                       "realized_pnl": w.realized_pnl, "last_buy_ts": w.last_buy_ts,
                       "seen": sorted(w.seen)}, fh)
    except Exception:
        pass


def buy_and_ladder(cand, spend, score, gross_usd, funder, sign_typed):
    """Live: buy then rest the conviction-scaled exit ladder. Returns entry_usd or None."""
    res = execute_buy(cand.token, spend, funder, sign_typed)
    if not res:
        return None
    entry_usd, qty = res
    conv = flash.conviction_from(score, gross_usd)
    L = flash.exit_ladder(entry_usd, qty, PLAN, conviction=conv)
    print(f"    filled @ ${entry_usd:.6g}/tok x{qty:.0f}  conviction {conv:.2f}")
    # stop-loss on the full position
    sl = flash.stop_loss_body(cand.token, contra=CONTRA, qty=str(L["stop_loss_qty"]),
                              stop_usd=str(L["stop_loss_usd"]))
    place_exit(sl, sign_typed) and print(f"    stop-loss @ ${L['stop_loss_usd']:.6g}")
    # laddered take-profits on the way up (rung 0 pulls initials)
    for i, r in enumerate(L["rungs"]):
        if r["qty"] <= 0:
            continue
        tp = flash.take_profit_body(cand.token, contra=CONTRA, qty=str(r["qty"]),
                                    target_usd=str(r["price_usd"]))
        label = "pull-initials" if i == 0 else "trim"
        place_exit(tp, sign_typed) and print(
            f"    TP {r['mult']}x @ ${r['price_usd']:.6g} sell {r['qty']:.0f} ({label})")
    print(f"    moon bag {L['moon_bag_qty']:.0f} rides free (stop ${L['moon_stop_usd']:.6g}) "
          f"-> ${L['moon_take_profit_usd']:.6g}")
    return entry_usd


def main():
    client = ChainClient()
    wallet = load_wallet()
    funder = sign_typed = None
    mode = "LIVE" if LIVE else "DRY-RUN"
    if LIVE:
        funder, sign_typed = _funder_and_signer()
        print(f"*** LIVE trading as {funder} — real funds. ***")
    print(f"auto-trader [{mode}] pad={POLICY.require_pad} spend={POLICY.spend_per_trade} "
          f"cap={POLICY.max_total_deployed} minLiq=${POLICY.min_liquidity_usd:,.0f} "
          f"stop={PLAN.stop_loss_pct:.0%} tp={PLAN.tp_mult}x moon->{PLAN.moon_tp_mult}x")
    print(f"exit ladder: cut -{PLAN.stop_loss_pct:.0%} | pull initials at {PLAN.tp_mult}x "
          f"| ride a free moon bag (stop breakeven) to {PLAN.moon_tp_mult}x\n")

    while True:
        try:
            cands = scan_live(client, blocks=BLOCKS, weth_usd=WETH_USD,
                              window_blocks=WINDOW, v3_factory=rcfg.CONTRACTS["v3_factory"],
                              weth=rcfg.CONTRACTS["weth"], pool_manager=rcfg.CONTRACTS["pool_manager"])
            now = time.time()
            for c in cands:
                sc = flow_score(c)
                d = should_buy(token=c.token, score=sc, pad=c.launchpad,
                               liquidity_usd=c.liquidity_usd, sniper_share=c.sniper_share,
                               fanout=c.transfer_fanout, wallet=wallet, policy=POLICY, now=now)
                if not d.ok:
                    continue
                tag = f"[{ (c.launchpad or '?') }] {c.token}  score {sc:.2f}  liq ${c.liquidity_usd:,.0f}  net ${c.net_flow_usd or 0:,.0f}"
                if not LIVE:
                    conv = flash.conviction_from(sc, c.volume_5m_usd)
                    L = flash.exit_ladder(1.0, 1000.0, PLAN, conviction=conv)  # unit preview
                    rung = " ".join(f"{r['mult']}x:{r['qty']/10:.0f}%" for r in L["rungs"] if r["qty"] > 0)
                    print(f"WOULD BUY {d.spend} {CONTRA[:8]}.. -> {tag}")
                    print(f"    conviction {conv:.2f} | cut -{PLAN.stop_loss_pct:.0%} | "
                          f"TP {rung} | moon {L['moon_bag_qty']/10:.0f}% -> {PLAN.moon_tp_mult}x")
                    wallet.seen.add(c.token)          # don't repeat the paper alert
                else:
                    print(f"BUY {d.spend} -> {tag}")
                    entry = buy_and_ladder(c, d.spend, sc, c.volume_5m_usd,
                                           funder, sign_typed)
                    if entry is not None:
                        record_buy(wallet, c.token, d.spend, now, entry_usd=entry)
            save_wallet(wallet)
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {mode}  open {len(wallet.positions)}  deployed {wallet.spent:.3f}  "
                  f"pnl {wallet.realized_pnl:+.3f}  seen {len(wallet.seen)}")
        except Exception as e:
            print(f"tick error ({e}) — retrying next interval")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
