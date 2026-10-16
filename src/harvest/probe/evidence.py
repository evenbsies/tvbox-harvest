"""Evidence accumulation for probe decisions.

The probe's output is only useful if it is *auditable*: when a site lands on Tier 4 you
should be able to read exactly which observations led there, and when an upstream site
changes shape you should be able to see which signal flipped. Everything the probe learns
goes into an :class:`Evidence` object and is persisted to ``sites/<key>.yaml:detect.evidence``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    stages: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str, **data: Any) -> None:
        entry: dict[str, Any] = {"stage": name}
        entry.update({k: v for k, v in data.items() if v not in (None, [], {})})
        self.stages.append(entry)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def fact(self, **kwargs: Any) -> None:
        self.facts.update(kwargs)

    def as_dict(self) -> dict[str, Any]:
        return {"stages": self.stages, "notes": self.notes, "facts": self.facts}

    def summarise(self) -> str:
        """One-line human summary, used in CLI output and Issue comments."""
        if not self.stages:
            return "no observations"
        return " -> ".join(s["stage"] for s in self.stages)
