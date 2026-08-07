# Watch → Act: executing runners via Definitive Flash

The runner tracker is read-only: it watches RH Chain and scores fresh pools. To
*act* on a HOT runner, hand it to an execution venue. **Definitive Flash**
(`flash.definitive.fi`) is a non-custodial best-execution API that supports
**Robinhood Chain (chainId 4663)** directly, so the handoff is clean.

## Why Flash fits

- **RH is a first-class chain.** `targetChain`/`contraChain` = `"robinhood"`,
  RPC `https://rpc.mainnet.chain.robinhood.com`.
- **QuickTrade** (`"quickTrade": true` on a market order) is purpose-built for
  sniping newly launched tokens — low latency, pre-warmed quotes, prioritized
  gas. That is exactly the runner-buy path.
- **Stop-loss / take-profit** order types give the "in a dump, go stable" exit as
  a native resting order: buy the runner, then rest a stop-loss that market-sells
  back to the contra asset if price draws down to a trigger.
- **Non-custodial.** Funds stay in your wallet; a one-time ERC-20 approval to the
  Flash settlement contract (`0x5d00000873b6BF41539e6f5365B0Ff7d3c368f78`, same on
  every EVM chain) plus a per-order signature is all it needs.

## The three steps

`POST /quote` → sign the returned `evm.orderTypedData` (EIP-712) with the funder
wallet → `POST /order`. Base URL `https://flash.definitive.fi/v1`, auth via the
`x-definitive-api-key` header. Flash's public dev key
(`dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b`) enables **quoting only** and cannot
move funds — every trade also needs the wallet signature.

## Keys never touch this repo

Signing needs the funder wallet's private key. **Do not** put a key in a script,
an env var you paste, or the chat. Use the **Flash MCP server**, which stores keys
in the OS keychain and out of any transcript:

```
claude mcp add definitive-flash -- npx -y @definitive-fi/flash-mcp
npx -y @definitive-fi/flash-mcp setup    # hidden prompts -> keychain
```

Then execution is a tool call: `flash_quote` to price, `flash_submit_order` to
trade (it handles wrap/approve/sign/submit/poll).

## How this repo helps

- `examples/rh_flash_quote.py` — read-only: live Flash quote (price, fee, route)
  for buying a token on RH. Moves nothing.
- `examples/rh_watch.py` with `RH_ACT=1` — on a HOT alert, prints a ready
  `flash_submit_order` intent (params only) you can hand to the MCP. Pair with
  `RH_PAD=bankr` to only act on one pad.
- `cambrian/runners/flash.py` — pure builders: `quote_body`, `stop_loss_body`,
  `intent_from_score`. No network, no signing.

The deliberate seam: this repo decides *what* to trade; the Flash MCP holds the
key and does the signing. Watch here, sign there.
