# cambrian

Automated trading + LP on Robinhood Chain (chain 4663), split across two desks
that share one config and one rule: **fail closed.** An unset address or an
unknown fact stops a trade rather than guessing at one, `DRY_RUN` is the default,
and the live signing path is deliberately unbuilt — nothing here can move real
funds until that seam is wired on purpose.

> This trades real money and most degen positions go to zero by design. The
> limits in `config.py` are what make that survivable. Nothing in this repo is
> financial advice, and none of it is safe to run live as-is.

## The two desks

- **Degen desk** — snipes freshly-launched tokens at tiny size ($25/trade). Hard
  caps on per-trade / daily notional, open positions, and trades-per-hour, plus
  gates on pool liquidity, top-holder concentration, expected slippage, a
  *trusted factory* allowlist, and a *reviewed v4 hook* allowlist.
- **LP desk** — provides Uniswap v3/v4 liquidity against tokenized equities. TVL
  floor, position caps, market-hours windows (stay out near the close, wait after
  the open), an earnings blackout, and a fee-APR-vs-impermanent-loss stress test.

The two desks hold **separate wallets** and no code path moves funds between
them: if the degen desk blows up, LP capital is untouched because the degen desk
never held its key.

## Architecture

The risky logic is pure and tested; the chain I/O is thin and best-effort.

```
cambrian/
  config.py          single source of truth (fill the placeholders before live)
  decision.py        Gate / Decision — fail-closed accept-reject vocabulary
  snapshots.py       frozen "facts" the desks reason over (None = unknown = reject)
  desks/degen.py     DegenDesk.evaluate(candidate, state) -> Decision
  desks/lp.py        LPDesk.evaluate(pool, ctx, state) -> Decision
  execution.py       DRY_RUN executor (works) + the live seam (raises, by design)
  journal.py         append-only JSONL order journal
  chain.py           read-only JSON-RPC client (no key ever touches it)
  feeds/             market-hours + earnings feeds, and the LPContext assembler
  tools/hook_review.py   optional Claude-assisted first pass over a v4 hook
  cli.py             python -m cambrian ...
tests/               the guardrails, proven one rejection at a time
```

The desks never touch the chain. They take frozen snapshots of on-chain/market
facts and return a `Decision`, so the whole risk engine is unit-tested with
fixtures and no network. Fetching those facts, and executing trades, are
separate concerns.

## Quick start (safe, works today)

```bash
pip install -r requirements.txt          # runtime deps
pip install -r requirements-dev.txt      # + pytest, tzdata (for tests)

python -m cambrian status                # what config is blocking each desk
python -m cambrian evaluate-degen examples/degen_candidate.json --execute
python -m cambrian evaluate-lp   examples/lp_pool.json --execute
python -m cambrian tail -n 20            # read the order journal

pytest -q                                # the guardrails
```

Everything above runs in `DRY_RUN` with no live addresses. The `evaluate-*`
commands run a real decision through the real desk logic against a JSON fixture
(see `examples/`), journal it, and — with `--execute` — route an approved
decision through the dry-run executor so you can watch the whole path end to end.

## Going live is a deliberate project, not a flag

1. Copy `.env.example` to `.env` and fill it in from the block explorer — **do
   not guess addresses.** `python -m cambrian status` lists exactly what's
   missing; each gap fails the relevant desk closed.
2. Populate the allowlists in `config.py`: `TRUSTED_FACTORIES`, the `UNISWAP`
   deployment addresses, `STOCK_TOKENS`, `QUOTE_TOKENS`. Add a hook to
   `REVIEWED_HOOKS` only after you have personally read its source (the
   `review-hook` command is a first pass, not a substitute — it never edits the
   set).
3. Wire a real, key-segregated transaction signer into `LiveExecutor` in
   `execution.py`. It is intentionally unimplemented; a fake signer is worse than
   none. Until then, and until `DRY_RUN=false`, the executor refuses to broadcast.
4. Watch the journal for weeks before you flip `DRY_RUN`.

## Base pivot (live on Cambrian today)

The RH/tokenized-equity strategy above is parked: Cambrian only indexes **Base
(8453) + Uniswap v3** today — not Robinhood Chain, not Uniswap v4. So the *same*
LP engine is pointed at Base DEX pools, which Cambrian does cover. Config lives
in `base_config.py`; the RH `config.py` stays untouched for when coverage lands.

```bash
python -m cambrian probe-cambrian                 # auth-check, list indexed chains
python -m cambrian scan-base --min-tvl 250000     # rank Base pools by fee APR (read-only)
python -m cambrian field                          # best APY across the WHOLE field (LP + lending)
python -m cambrian scan-base --raw                # dump real column names (see note below)
python -m cambrian evaluate-base-lp --position 250   # scan + run each pool through the desk
python -m cambrian allocate 0x<token>             # LP vs lending: which yields more
python -m cambrian plan 10000                     # turn a deposit into a target portfolio (the brain)
python -m cambrian rebalance 10000 --apply        # plan moves current -> target, record it (the babysitter)
python -m cambrian positions                      # show current paper positions
python -m cambrian monitor                        # watch positions; fire rotations
python -m cambrian monitor --market-drop 0.18     # test the flight-to-stables breaker
python -m cambrian run 10000 --apply              # ONE full autonomous cycle (paper)
```

### `run` — the whole loop in one command

`run <capital>` is the manager: it scans the field, sizes a fresh target,
consults the monitor on what you hold, reconciles (rotate the decayers, or in a
dump exit everything to stables), and executes the resulting ENTER/EXIT/RESIZE
moves — journaling each and persisting the new positions. It's `scan + plan +
monitor + rebalance + execute` as a single cycle. Run it on a schedule (cron /
Task Scheduler) and it manages the book continuously. Still paper: the executor
journals intent and updates state but signs nothing (`base/execution.py`
`LiveBaseExecutor` is the one gated seam).

### The manager (deposit → portfolio → rebalance, all dry-run)

`plan <capital>` is the brain: it scores every pool (discounting emissions,
subtracting an IL cost sized to the pair's volatility, rejecting thin/yieldless
traps), sorts survivors into a **barbell** — a low-risk `core` sleeve and a
capped high-yield `satellite` sleeve — sizes them under per-pool and per-token
concentration caps, and holds the rest as a stable reserve. Any deposit scales
the weights. `rebalance` diffs your current paper positions against a fresh
target and emits the minimal ENTER/EXIT/RESIZE moves, journaling each. Policy
(sleeve budgets, caps, emission discount, IL costs) lives in `base_config.py`.

`monitor` is the defense — the piece that makes active rotation actually work.
Two layers: **per-position triggers** (a pool's APR decaying below a fraction of
entry, its TVL draining, or the volatile leg breaking a stop-loss → rotate out),
and a **flight-to-stables circuit breaker** (a market-wide dump or a book-level
max-drawdown → pull *everything* non-stable to the reserve, because in a cascade
there's no green farm to rotate into). Thresholds live in `MonitorPolicy` in
`base_config.py`.

**Everything above is simulation.** It decides, sizes, rotates, and logs every
move, but moves no funds — placing/pulling actual liquidity (signing) is the one
deliberate seam left unbuilt in `execution.py`, so you can trust the brain's
picks before a cent is at risk.

- **`scan-base`** — pulls pools across Aerodrome / Uniswap-v3 / Pancake / Sushi /
  AlienBase / Clones (every pool, paginated — not just the first page), computes
  fee APR, filters by TVL, ranks. Read-only.
- **`field`** — the whole yield field in one ranked list: every LP pool *and*
  every lending market (Aave / Euler / Morpho), best APY first, each LP row
  tagged with how much of its APR is durable swap fees vs. emissions.
- **`evaluate-base-lp`** — same LP guardrails (venue allowlist, TVL floor,
  fee-APR-vs-IL stress test, exposure caps), minus the equity gates (crypto is
  24/7). Journals every decision, dry-run.
- **`allocate`** — compares a token's best LP fee APR (IL-haircut) against its
  best lending supply APR (Aave/Euler/Morpho) and recommends one.

> **One thing to confirm on first run:** the client is built against Cambrian's
> confirmed auth (`x-api-key`) and columnar response format, but the exact
> *column names* per pools endpoint were written without live API access. If
> `scan-base` loads pools but ranks none, run `scan-base --raw` to see the real
> column names and add them to `ALIASES` in `cambrian/base/pools.py` (and
> `base/lending.py`) — a one-line fix, not a rewrite.

## Honest limitations

- **Live execution is not built.** The read client and decision logic are real;
  signing/broadcasting is a documented seam.
- **The market-hours calendar bundles NYSE holidays for 2025–2026 only** and
  fails closed (reports the market closed) for any date it can't vouch for.
  Verify it against the official calendar and extend it; half-days aren't modeled.
- **The earnings calendar is manual** by default (a ticker→date JSON you
  maintain); an unknown ticker is treated as inside the blackout.
- **The fee-APR stress test is a floor, not a model** — it compares fee APR to
  full-range impermanent loss at the configured stress ratio and understates the
  loss for a concentrated position. Calibrate it once you have real fee data.
