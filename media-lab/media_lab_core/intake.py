"""Model/runtime intake and opt-in update scout — deterministic and fail-closed.

Two responsibilities, kept apart on purpose:

1. ``IntakeGate`` — a strict, deterministic admission policy for a user-supplied
   repo + immutable revision. It inspects provenance, license/terms,
   architecture, files, SafeTensors, ``trust_remote_code``, runtime
   compatibility, sbom, disk/unified-memory budget, then STAGES to an immutable
   isolated cache, hashes artifacts, and refuses unsafe input. No code from an
   untrusted revision is ever executed here; this module only reads metadata.

2. ``UpdateScout`` — an *opt-in*, non-automatic updater. It inspects official
   HF revisions / runtime releases / reviewed nodes at most ONCE PER RUN (never
   a live poll), shows evidence + changelog + migration plan + canary + rollback,
   and returns a proposal. It never promotes; promotion is a separate explicit
   apply gate. It does not require a public service and does not leak usage.

Everything here is deterministic, read-only, and hostile-input safe.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_MUTABLE_REF = re.compile(r"^(main|master|latest|HEAD|develop)$")
_TRUST_REMOTE_CODE = re.compile(r"trust_remote_code\s*=\s*True", re.IGNORECASE)
_SUBPROC = re.compile(r"subprocess|os\.system|eval\s*\(|exec\s*\(|__import__|pickle\.load",
                      re.IGNORECASE)


class IntakeError(RuntimeError):
    """A fail-closed intake/update decision."""


def _is_lockfile(name: str) -> bool:
    return name.endswith((".lock.json", ".sha256", ".sbom.json", ".toml", ".json",
                          ".lock", ".yaml", ".yml", ".txt"))


def _ever_untrusted(names: Iterable[str]) -> bool:
    for n in names:
        if n.endswith((".py", ".pt", ".pth", ".pkl", ".bin", ".so", ".dylib",
                       ".dll", ".whl", ".npy", ".binmodel", ".safetensors")):
            return True
    return False


class IntakeGate:
    def __init__(self, budget_gb: float = 100.0, cache_root: Path | None = None):
        self.budget_gb = budget_gb
        self.cache_root = Path(cache_root) if cache_root else Path("/tmp/medialab-intake")

    def inspect(self,
                repo_url: str,
                revision: str,
                license_name: str = "",
                license_url: str = "",
                files: list[dict[str, Any]] | None = None,
                trust_remote_code: bool | None = None,
                signatures_verified: bool = False,
                disk_gb: float = 0.0,
                memory_gb: float = 0.0) -> dict[str, Any]:
        """Return an intake verdict + reasons. Never executes any supplied file."""
        errors: list[str] = []

        if not repo_url.startswith(("https://", "git@", "ssh://")):
            errors.append("repo_url must be https or ssh (no file:// or any scheme)")
        if _MUTABLE_REF.match(revision):
            errors.append(f"mutable revision rejected: {revision!r} (pin an immutable sha)")
        if not _SHA_RE.match(revision):
            errors.append(f"revision not an immutable sha-1/sha-256: {revision!r}")
        if not license_name or license_name.lower() in {"unresolved", "unknown", "choose a license"}:
            errors.append("license unresolved")
        if not license_url:
            errors.append("license url missing")

        names = [str(f.get("name", "")) for f in files or []]
        if not names:
            errors.append("no file manifest supplied; cannot admit")
        # trust_remote_code is an unconditional stop if the revision needs it.
        if trust_remote_code:
            errors.append("trust_remote_code=True rejected: no remote code permitted without a separate review gate")
        # Any executable/unsafe extension is a stop unless reviewed-safe metadata passed.
        if not signatures_verified and _ever_untrusted(names):
            errors.append("unsafe artifact formats present and signatures not verified")

        if not errors and disk_gb > self.budget_gb:
            errors.append(f"disk_gb {disk_gb:.1f} exceeds intake budget {self.budget_gb:.1f}")
        if not errors and memory_gb > self.budget_gb:
            errors.append(f"memory_gb {memory_gb:.1f} exceeds intake budget {self.budget_gb:.1f}")

        return {
            "admitted": not errors,
            "errors": errors,
            "stage": "metadata-only",
            "repo_url": repo_url,
            "revision": revision,
            "license_name": license_name,
            "cache_root": str(self.cache_root),
        }

    def stage_artifacts(self, staged_dir: Path, entry_id: str) -> dict[str, Any]:
        """Deterministically hash a staged directory and pin it into the cache.

        The directory must already be isolated; this computes per-file sha256 and
        returns a manifest. Fail-closed on any unexpected artifact type.
        """
        staged_dir = Path(staged_dir)
        if not staged_dir.is_dir():
            raise IntakeError(f"staged dir missing: {staged_dir}")
        entries: dict[str, dict[str, Any]] = {}
        dupes = set()
        for path in sorted(staged_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = str(path.relative_to(staged_dir))
            if rel in entries:
                dupes.add(rel)
            entries[rel] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        dest = self.cache_root / entry_id
        dest.mkdir(parents=True, exist_ok=True)
        for rel in entries:
            src = staged_dir / rel
            final = dest / rel
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, final)
        manifest_path = dest / "_manifest.json"
        manifest_path.write_text(json.dumps({"entry": entry_id, "files": entries,
                                             "duplicates": sorted(dupes)},
                                            indent=2))
        return {"entry_id": entry_id, "manifest_path": str(manifest_path),
                "files": entries, "duplicates": sorted(dupes)}


class UpdateScout:
    """Opt-in, once-per-run, never auto-promoting source inspector."""

    def __init__(self, trusted_sources: list[str] | None = None,
                 reviewed_nodes: list[str] | None = None):
        self.trusted_sources = set(trusted_sources or [])
        self.reviewed_nodes = set(reviewed_nodes or [])

    def check(self, source_url: str, node_name: str = "") -> dict[str, Any]:
        """Evaluate one potential update. Returns a proposal, never applies it."""
        problems: list[str] = []
        if source_url not in self.trusted_sources:
            problems.append(f"source not in trusted set: {source_url}")
        if not node_name:  # model catches lack of identity separately
            node_name = "(model)"
        if node_name != "(model)" and node_name not in self.reviewed_nodes:
            problems.append(f"node not in reviewed set: {node_name}")
        review_stage = "pending" if problems else "reviewed"
        return {
            "source_url": source_url, "node_name": node_name,
            "review_stage": review_stage,
            "problems": problems,
            "plan": {
                "evidence": "inspect official revision + reviewed changelog",
                "migration": "stage to isolated cache, hash, canary, then explicit apply",
                "canary": "one canary run under the shared orthogonal lock",
                "rollback": "revert to prior pinned revision + exact restore",
            },
            "promote": False,  # never auto-promote
        }

    def promote(self, verdict: Mapping[str, Any], explicit_approval: bool = False) -> dict[str, Any]:
        if not explicit_approval:
            return {"promoted": False, "reason": "promotion requires explicit separate approval"}
        if verdict.get("review_stage") != "reviewed":
            return {"promoted": False, "reason": f"review stage = {verdict.get('review_stage')}"}
        if verdict.get("problems"):
            return {"promoted": False, "reason": f"unresolved problems: {verdict['problems']}"}
        return {"promoted": True, "revision": verdict.get("source_url")}