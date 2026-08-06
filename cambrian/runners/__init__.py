"""Runner tracker — watch the RH Chain launchpads from above and catch fresh
tokens that are starting to run, before they're obvious.

Zero Cambrian (which doesn't index RH anyway): this reads the chain directly —
launchpad factory events, DEX swaps, holder counts, and a smart-money watchlist —
via raw RPC + the Blockscout explorer. It's the discovery front-end that feeds
the degen desk: it finds the runner, the desk (config.py: trusted factory, top-
holder cap, liquidity floor, hook review) decides whether it's safe to touch.
"""
