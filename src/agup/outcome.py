"""Outcome types.

The central design decision of this tool: a component that was *not* updated is
not the same as a component that *failed* to update. Upstream tooling collapses
both into a boolean and exits non-zero either way, which makes the exit status
useless for unattended runs -- a Hub that was simply open would page you.

Outcome separates the three cases and the exit code follows from them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

# Exit codes. Anything unattended should treat 0 and 2 as fine.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SKIPPED = 2
EXIT_USAGE = 3


class Status(enum.Enum):
    """What happened to one component."""

    UPDATED = "updated"
    CURRENT = "current"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def is_failure(self) -> bool:
        return self is Status.FAILED

    @property
    def is_change(self) -> bool:
        return self is Status.UPDATED


@dataclass
class Outcome:
    """Result of acting on a single component."""

    component: str
    status: Status
    detail: str = ""
    from_version: Optional[str] = None
    to_version: Optional[str] = None
    #: Set when the failure was a DNS resolution failure specifically.
    dns: bool = False

    @classmethod
    def updated(cls, component: str, frm: str, to: str) -> "Outcome":
        return cls(component, Status.UPDATED, f"{frm} -> {to}", frm, to)

    @classmethod
    def current(cls, component: str, version: str) -> "Outcome":
        return cls(component, Status.CURRENT, f"already at {version}", version, version)

    @classmethod
    def skipped(cls, component: str, reason: str) -> "Outcome":
        return cls(component, Status.SKIPPED, reason)

    @classmethod
    def failed(cls, component: str, reason: str, *, dns: bool = False) -> "Outcome":
        return cls(component, Status.FAILED, reason, dns=dns)


@dataclass
class RunReport:
    """All outcomes from one invocation."""

    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, outcome: Outcome) -> Outcome:
        self.outcomes.append(outcome)
        return outcome

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status.is_failure]

    @property
    def skips(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status is Status.SKIPPED]

    @property
    def changes(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status.is_change]

    @property
    def all_dns_failures(self) -> bool:
        """Whether every attempted component failed to resolve its endpoint.

        Three endpoints failing to resolve at once is not three outages -- it
        is one local resolver. Saying so turns a confusing error into an
        actionable one.
        """
        failures = self.failures
        if not failures or len(failures) != len(self.outcomes):
            return False
        return all(o.dns for o in failures)

    def exit_code(self) -> int:
        """Map the run onto a process exit code.

        1 if anything genuinely broke. 2 if nothing broke but something was
        deliberately not done (component running, no permission to touch a
        system path). 0 when every component is where it should be.
        """
        if self.failures:
            return EXIT_FAILED
        if self.skips:
            return EXIT_SKIPPED
        return EXIT_OK

    def summary_line(self) -> str:
        if not self.outcomes:
            return "Nothing selected."
        parts = []
        for label, group in (
            ("updated", self.changes),
            ("skipped", self.skips),
            ("failed", self.failures),
        ):
            if group:
                parts.append(f"{len(group)} {label}")
        current = [o for o in self.outcomes if o.status is Status.CURRENT]
        if current:
            parts.append(f"{len(current)} already current")
        return ", ".join(parts) if parts else "Nothing to do."
