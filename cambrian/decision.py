"""The accept/reject vocabulary every desk speaks.

A `Gate` accumulates the reasons a candidate should be *rejected*. If none
accumulate, the candidate is approved. This inverts the usual "return True on
success" so that adding a new safety check can only ever make the system more
conservative, never less: you add a `require(...)`, and the default outcome for
anything you forgot to think about stays "rejected".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Decision:
    approved: bool
    desk: str
    subject: str
    #: Why it was rejected. Empty iff approved.
    reasons: tuple[str, ...] = ()
    #: Informational context, recorded whether approved or not.
    notes: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        return not self.approved

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "desk": self.desk,
            "subject": self.subject,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
        }

    def __str__(self) -> str:
        head = f"[{self.desk}] {self.subject}: {'APPROVED' if self.approved else 'REJECTED'}"
        if self.reasons:
            head += "\n  - " + "\n  - ".join(self.reasons)
        return head


class Gate:
    """Fluent builder for a `Decision`. Fail-closed by construction.

        g = Gate("degen", token)
        g.require(liq >= floor, f"liquidity {liq} below floor {floor}")
        g.require(not limits.halted, "desk halted")
        return g.decide()
    """

    def __init__(self, desk: str, subject: str):
        self.desk = desk
        self.subject = subject
        self._reasons: list[str] = []
        self._notes: list[str] = []

    def require(self, condition: bool, reason: str) -> "Gate":
        """If `condition` is falsey, record `reason` as a rejection."""
        if not condition:
            self._reasons.append(reason)
        return self

    def reject(self, reason: str) -> "Gate":
        """Unconditionally record a rejection."""
        self._reasons.append(reason)
        return self

    def note(self, message: str) -> "Gate":
        self._notes.append(message)
        return self

    def decide(self) -> Decision:
        return Decision(
            approved=not self._reasons,
            desk=self.desk,
            subject=self.subject,
            reasons=tuple(self._reasons),
            notes=tuple(self._notes),
        )
