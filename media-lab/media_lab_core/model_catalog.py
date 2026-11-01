from __future__ import annotations

import tomllib
import re
from dataclasses import dataclass
from pathlib import Path

VALID_STATUS = {"qualified", "experimental", "planned", "blocked"}
VALID_CATEGORIES = {"video", "image", "audio", "llm", "enhancement", "model"}


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    category: str
    status: str
    recommended: bool
    quality: str
    speed: str
    summary: str
    disk_gb: float
    memory_floor_gb: float
    license_name: str
    license_url: str
    terms_acceptance_required: bool
    source_url: str
    immutable_revision: str
    sha256: str
    requires: tuple[str, ...]
    exclusive_group: str
    hardware: tuple[str, ...]

    @property
    def selectable(self) -> bool:
        return (
            self.status == "qualified"
            and bool(self.source_url)
            and bool(self.immutable_revision)
            and bool(self.sha256)
            and re.fullmatch(r'[0-9a-fA-F]{64}', self.sha256) is not None
            and self.license_name not in {"", "unresolved", "unknown"}
        )

    def refusal_reason(self) -> str:
        reasons = []
        if self.status != "qualified":
            reasons.append(f"status={self.status}")
        if not self.source_url:
            reasons.append("source URL missing")
        if not self.immutable_revision:
            reasons.append("immutable revision missing")
        if re.fullmatch(r'[0-9a-fA-F]{64}', self.sha256) is None:
            reasons.append("SHA-256 missing or invalid")
        if self.license_name in {"", "unresolved", "unknown"}:
            reasons.append("license unresolved")
        return "; ".join(reasons)


def load_model_catalog(path: str | Path) -> dict[str, ModelOption]:
    source = Path(path)
    with source.open("rb") as handle:
        data = tomllib.load(handle)
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError("catalog schema_version must be 1")
    rows = data.get("models")
    if not isinstance(rows, list) or not rows:
        raise ValueError("catalog must contain at least one [[models]] entry")
    result: dict[str, ModelOption] = {}
    for row in rows:
        model_id = str(row.get("id", "")).strip()
        if not model_id or model_id in result:
            raise ValueError(f"model id is missing or duplicated: {model_id!r}")
        category = str(row.get("category", ""))
        status = str(row.get("status", "planned"))
        if category not in VALID_CATEGORIES:
            raise ValueError(f"{model_id}: invalid category {category!r}")
        if status not in VALID_STATUS:
            raise ValueError(f"{model_id}: invalid status {status!r}")
        result[model_id] = ModelOption(
            id=model_id,
            name=str(row.get("name", model_id)),
            category=category,
            status=status,
            recommended=bool(row.get("recommended", False)),
            quality=str(row.get("quality", "unrated")),
            speed=str(row.get("speed", "unrated")),
            summary=str(row.get("summary", "")),
            disk_gb=float(row.get("disk_gb", 0)),
            memory_floor_gb=float(row.get("memory_floor_gb", 0)),
            license_name=str(row.get("license_name", "unresolved")),
            license_url=str(row.get("license_url", "")),
            terms_acceptance_required=bool(row.get("terms_acceptance_required", False)),
            source_url=str(row.get("source_url", "")),
            immutable_revision=str(row.get("immutable_revision", "")),
            sha256=str(row.get("sha256", "")),
            requires=tuple(str(x) for x in row.get("requires", [])),
            exclusive_group=str(row.get("exclusive_group", "")),
            hardware=tuple(str(x) for x in row.get("hardware", ["any"])),
        )
    for model in result.values():
        missing = [dependency for dependency in model.requires if dependency not in result]
        if missing:
            raise ValueError(f"{model.id}: unknown dependencies {missing}")
    return result


def resolve_selection(catalog: dict[str, ModelOption], selected: list[str]) -> list[ModelOption]:
    unknown = sorted(set(selected) - set(catalog))
    if unknown:
        raise ValueError(f"unknown model ids: {', '.join(unknown)}")
    resolved: set[str] = set()
    visiting: list[str] = []

    def add(model_id: str) -> None:
        if model_id in resolved:
            return
        if model_id in visiting:
            raise ValueError("model dependency cycle: " + " -> ".join([*visiting, model_id]))
        model = catalog[model_id]
        if not model.selectable:
            raise ValueError(f"{model_id} is not selectable: {model.refusal_reason()}")
        visiting.append(model_id)
        for dependency in model.requires:
            add(dependency)
        visiting.pop()
        resolved.add(model_id)

    for model_id in selected:
        add(model_id)
    return [catalog[model_id] for model_id in catalog if model_id in resolved]
