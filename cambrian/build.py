"""Which build is this.

Exists because "run the new file" and "the new file is what ran" are different
claims, and telling them apart cost a round: the single-file bundle is copied by
hand into a home directory, so a stale copy runs silently and its output looks
like a bug in the new code rather than an old file. Every diagnostic command
prints this stamp, so the first question is always answered before the second is
asked.

`tools_build_bundle.py` overwrites this module with the commit it built from. A
checkout leaves it as-is, which is the honest answer there.
"""

BUILD = "checkout (unstamped)"
