"""Verified, provenance-labelled local agent-harness catalog + adapter interface.

Selection is entirely OPTIONAL, source-verified, isolated, and separately
approved.  Names from speech transcription are never treated as verified
products; the exact upstream source is required before an entry is even listed.

The ``HarnessAdapter`` interface is what a real installer would implement; the
catalog here only declares the reviewed contract and refuses anything not
source-pinned.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_SHA_RE = r"^[0-9a-fA-F]{40}|^[0-9a-fA-F]{64}$"
import re


@dataclass(frozen=True)
class HarnessOption:
    id: str
    name: str
    # exact upstream source, not a speech-transcription guess
    source_url: str
    docs_url: str
    immutable_release: str
    sha256: str
    license_name: str
    permissions: tuple[str, ...]
    isolation: str
    tool_access: tuple[str, ...]
    network_policy: str
    maintenance_model: str
    compatibility: str
    resource_footprint_gb: float
    review_receipt: str
    terms_acceptance_required: bool
    verified: bool = field(default=True)

    def selectable(self) -> bool:
        return (self.verified
                and bool(self.source_url) and bool(self.docs_url)
                and bool(re.fullmatch(_SHA_RE, self.sha256))
                and bool(self.review_receipt))


def _harness(sid: str, name: str, source: str, docs: str, rel: str, sha: str,
             lic: str, perms: Iterable[str], isol: str, tools: Iterable[str],
             net: str, maint: str, comp: str, fp: float, receipt: str,
             terms: bool = False) -> HarnessOption:
    return HarnessOption(sid, name, source, docs, rel, sha, lic, tuple(perms),
                         isol, tuple(tools), net, maint, comp, fp, receipt, terms)


class HarnessCatalog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._entries: dict[str, HarnessOption] = {}
        if path is not None:
            self.load(path)

    def load(self, path: Path) -> None:
        data = json.loads(Path(path).read_text())
        for row in data.get("harnesses", []):
            opt = _harness(
                row["id"], row["name"], row["source_url"], row["docs_url"],
                row["immutable_release"], row["sha256"], row["license_name"],
                row.get("permissions", []), row.get("isolation", "container"),
                row.get("tool_access", []), row.get("network_policy", "default-deny"),
                row.get("maintenance_model", "manual"), row.get("compatibility", "spark"),
                float(row.get("resource_footprint_gb", 0)), row.get("review_receipt", ""),
                bool(row.get("terms_acceptance_required", False)),
            )
            self._entries[opt.id] = opt

    def list(self, modalities: str = "") -> list[HarnessOption]:
        opts = [o for o in self._entries.values() if o.selectable()]
        if modalities:
            opts = [o for o in opts if o.id.lower().startswith(modalities.lower())]
        return sorted(opts, key=lambda o: o.id)

    def get(self, sid: str) -> HarnessOption:
        return self._entries[sid]

    def resolve_selection(self, sel: str) -> dict[str, Any]:
        if not self._entries:
            raise ValueError("no verified harness catalog loaded")
        opt = self._entries.get(sel)
        if opt is None:
            raise ValueError(f"harness {sel!r} not in verified catalog — refuse unverified name")
        if not opt.selectable():
            raise ValueError(f"harness {sel!r} is not source-verified/selectable")
        return {
            "id": opt.id, "name": opt.name, "source_url": opt.source_url,
            "docs_url": opt.docs_url, "immutable_release": opt.immutable_release,
            "license_name": opt.license_name, "permissions": list(opt.permissions),
            "isolation": opt.isolation, "network_policy": opt.network_policy,
            "resource_footprint_gb": opt.resource_footprint_gb,
            "apply_required": True,   # separate explicit apply gate
            "installed": False,
        }


class HarnessAdapter(ABC):
    """What a real, source-verified installer would implement. Catalog does not
    execute installers during onboarding — only after the explicit apply gate."""

    @abstractmethod
    def manifest(self) -> dict[str, Any]: ...

    @abstractmethod
    def install(self, opt: HarnessOption) -> dict[str, Any]: ...

    @abstractmethod
    def verify(self) -> dict[str, Any]: ...

    @abstractmethod
    def uninstall(self) -> dict[str, Any]: ...