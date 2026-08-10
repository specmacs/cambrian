# Going live on a $20 vault

Every command is a **single line** — PowerShell chokes on multi-line pastes and
here-strings. Paste keys **without quotes around them**.

---

## Setup — get the code first

`python -m cambrian` only works from **inside the repo folder**. Running it from
your home directory gives `No module named cambrian`, which is what that error
means: the package is not installed system-wide, it is run from the checkout.

```
cd C:\Users\JohnHouk
git clone https://github.com/specmacs/cambrian.git
cd cambrian
git checkout claude/handoff-docs-review-h2ru4f
pip install requests
```

`requests` is the only dependency any of this needs. `anthropic` and
`python-dotenv` are listed in requirements.txt but are optional — nothing in the
trading path imports them.

**Every command below assumes you are in that `cambrian` folder.** If you open a
new PowerShell window, `cd C:\Users\JohnHouk\cambrian` again first — and
re-set the environment variables, because they do not survive a new window.

Check it works:

```
python -m cambrian --help
```

---

## Which API you are using

Definitive has two, and your dev was pointing you at the second one:

| | Flash API | **Client API** |
| --- | --- | --- |
| Wallet | yours, you sign every order | **Definitive vault** |
| Per-order signing | EIP-712 | **none** |
| Approve tx | yes | **no** |
| Built for | third parties | your own account |

Your `dpka_` / `dpks_` pair is the **Client API** key and secret. That is the
simpler path and it is what everything below uses: no wallet, no signing, no
approve step. Quote, then execute.

⚠️ **The key IS the authorization.** On this API there is no wallet signature
standing between an API call and a fill. That is why `--yes` is mandatory and
`--max-usd` is a hard cap — they are the only things between a command and your
money.

## 0. What this first trade is for

Not profit. At $20 the goal is to prove one loop end to end:

> fund → quote → execute → the desk marks it → an exit fires

Measured live at this size, a round trip costs **~2.3%** (≈$0.18 on $8). So the
token has to move ~2.3% just to break even. A flat trade is a small loss, and
that is the expected outcome rather than a bug.

## 1. Keys — put them in a file, not in the shell

Setting a secret with `$env:X=value` is where this went wrong repeatedly: the
shell has opinions about quotes, and a prompt collects whatever you paste next.
A text file has no grammar. Open it:

```
notepad $env:USERPROFILE\cambrian.env
```

Two lines, nothing else, no quotes:

```
DEFINITIVE_API_KEY=dpka_your_key_here
DEFINITIVE_API_SECRET=dpks_your_secret_here
```

Save. Then check:

```
python -m cambrian keys
```

It prints where each value came from and the first few characters only — never
the secret. `dpka_` on the key line and `dpks_` on the secret line means you are
ready.

You do not have to be careful about *which* line each value lands on. `dpka_` and
`dpks_` are unambiguous, so the loader searches the whole file for each prefix: a
swap, or a whole terminal line pasted around the credential, both self-correct.
What it cannot do is invent a credential that is not in the file anywhere — that
is the one thing `keys` will tell you it found.

The RPC is not a secret and can stay in the shell:

```
$env:RH_RPC_URL="https://rpc.mainnet.chain.robinhood.com"
```

## 2. Find the vault and fund it

```
python -m cambrian vault --wallet 0xYourOwnWalletAddress
```

`--wallet` is **your** address — the one you will send the deposit from, not the
vault's. The API requires it and returns a 400 naming `walletAddress` without it.
Set it once instead if you prefer: `$env:RH_WALLET="0xYourOwnWalletAddress"`.

Prints your Robinhood Chain vault address (created on demand) and any positions.
Send **$20 of ETH** to it.

ETH rather than USDG for trade #1: an ETH-quoted launch is a single hop, while
USDG into an ETH- or stock-quoted launch is two hops, pays slippage twice, and
leaves you holding an intermediate between legs. Fewer moving parts.

## 3. Settings for a $20 book

⚠️ **Without these the desk refuses to trade.** Defaults risk 5% of bankroll per
ticket — $1 on a $20 wallet, below the $10 minimum — so every candidate sizes to
**zero**. That is the fail-safe working, not a bug, but nothing happens until you
widen it.

```
$env:RH_BANKROLL_USD="20"
$env:RH_RISK_BPS="4000"
$env:RH_MIN_BUY_USD="4"
$env:RH_MAX_BUY_USD="8"
```

That gives an $8 ticket. At this size the bankroll cap dominates and market cap
barely matters — every candidate sizes to $8 — which is right for a test wallet
and **not** how it behaves at a real bankroll.

## 4. Watch first

```
python examples\cambrian_paper.py
```

http://127.0.0.1:8788 — paper, no key used, no execution path at all. Let it run
until you have seen a position open, get marked, and an exit fire. If the marks
look wrong to you, stop and say so before funding.

## 5. Quote a real trade — this does NOT execute

```
python -m cambrian trade-vault --max-usd 8
```

Picks the best candidate that cleared the gate, sizes it, and quotes it against
your vault. Prints the token, market cap, tax, what you spend, what you receive,
price impact, fee and minOut. **Nothing is submitted without `--yes`.**

To aim at one token: add `--token 0xTheToken`. To spend USDG instead of WETH: add
`--contra 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`.

## 6. Execute

```
python -m cambrian trade-vault --max-usd 8 --token 0xTheToken --yes
```

It re-quotes, re-checks the loss limit, and executes. The trade is refused if the
leg loses more than `--max-loss-pct` (default 10%) even with `--yes`.

## 7. After the fill

```
python -m cambrian vault
```

Confirms the position. Then let the desk manage it: stop → trailing stop → rungs
→ liquidity collapse → time stop, with the rug check outranking all of them.

---

## What is guarding you, and what is not

Guarding you:

- `--max-usd` hard-caps every trade regardless of what the sizer wants
- the tax gate blocks >3% (>5% stock-paired) before anything is quoted
- the quote's real loss is re-checked against `--max-loss-pct` before execution
- `--yes` is required; nothing auto-executes, one trade per invocation
- `watch` and the paper terminal have **no** execution path at all

Not covered — read these before funding:

- **No position has ever been opened by this system.** Every exit rule is tested
  against synthetic marks. This is the first real test of the whole loop.
- ~~My signature implementation is unverified.~~ **The signature is confirmed
  working** against live keys. A 400 `ZodError` naming `walletAddress` came back
  from the address endpoint, which only happens after auth passes and the request
  reaches body validation. HMAC, prehash, compact-JSON body and header ordering
  are all correct. If you ever do see a 401, `vault --debug` prints the exact
  signed string with the key redacted and the secret untouched.
- **`~spot` marks.** pons-v1 and pools.trade have no local sell math, so between
  Flash refreshes (30s) they mark at spot, ignoring exit slippage. Slightly
  optimistic. Curve and V2 venues are exact.
- There is also an official `@definitive-fi/mcp` server if you would rather drive
  the vault conversationally from Claude.
