"""cambrian — a two-desk automated trading + LP system for Robinhood Chain.

Design in one breath: the risky logic is pure and tested, the chain I/O is thin
and best-effort, and everything fails closed — an unset address or an unknown
fact stops a trade rather than guessing at one. `DRY_RUN` is the default and the
live signing path is deliberately unbuilt, so nothing here can move real funds
until you wire that seam on purpose.

Start at `cambrian.config` (the single source of truth) and the two
`cambrian.desks`. Run `python -m cambrian status` to see what's blocking.
"""

__version__ = "0.1.0"
