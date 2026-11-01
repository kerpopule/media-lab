from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

VALID_STATUSES = {"qualified", "experimental", "quarantined", "unconfigured"}


@dataclass(frozen=True)
class EngineCapability:
    name: str
    adapter: str
    status: str
    enabled: bool
    distribution: str
    license_class: str
    modes: tuple[str, ...]
    required_kernels: tuple[str, ...]
    reason: str = ""
    immutable_revision: str = ""

    @property
    def available(self) -> bool:
        return self.enabled and self.status == "qualified" and bool(self.immutable_revision)

    def refusal_reason(self) -> str:
        if self.available:
            return ""
        problems = []
        if not self.enabled:
            problems.append("disabled")
        if self.status != "qualified":
            problems.append(f"status={self.status}")
        if not self.immutable_revision:
            problems.append("immutable revision missing")
        if self.reason:
            problems.append(self.reason)
        return "; ".join(problems)


def load_engine_registry(path: str | Path) -> dict[str, EngineCapability]:
    source = Path(path)
    with source.open("rb") as handle:
        data = tomllib.load(handle)
    rows = data.get("engines")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("engine registry must contain a nonempty [engines.*] table")
    result: dict[str, EngineCapability] = {}
    for name, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError(f"engines.{name} must be a table")  # noqa: TRY004
        status = str(row.get("status", "unconfigured"))
        if status not in VALID_STATUSES:
            raise ValueError(f"engines.{name}.status must be one of {sorted(VALID_STATUSES)}")
        capability = EngineCapability(
            name=name,
            adapter=str(row.get("adapter", "")),
            status=status,
            enabled=bool(row.get("enabled", False)),
            distribution=str(row.get("distribution", "external")),
            license_class=str(row.get("license_class", "unknown")),
            modes=tuple(str(x) for x in row.get("modes", [])),
            required_kernels=tuple(str(x) for x in row.get("required_kernels", [])),
            reason=str(row.get("reason", "")),
            immutable_revision=str(row.get("immutable_revision", "")),
        )
        if not capability.adapter:
            raise ValueError(f"engines.{name}.adapter is required")
        result[name] = capability
    return result
