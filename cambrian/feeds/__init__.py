"""Fact sources for time- and calendar-dependent inputs (market hours, earnings).

These are best-effort and pluggable. Where a source is unavailable, the feed
returns `None` and the desks fail closed on it — an unknown is never a pass.
"""
