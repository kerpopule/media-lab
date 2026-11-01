from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine_registry import load_engine_registry


def inspect_registry(path: Path) -> dict:
    engines = load_engine_registry(path)
    rows = {}
    for name, engine in engines.items():
        rows[name] = {
            "adapter": engine.adapter,
            "status": engine.status,
            "enabled": engine.enabled,
            "available": engine.available,
            "immutable_revision": engine.immutable_revision,
            "modes": list(engine.modes),
            "required_kernels": list(engine.required_kernels),
            "refusal_reason": engine.refusal_reason(),
        }
    return {
        "ok": all(row["available"] for row in rows.values()),
        "configured_engines": sum(row["available"] for row in rows.values()),
        "engines": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Media Lab engine registry")
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--require", action="append", default=[], metavar="ENGINE",
        help="fail unless this engine is qualified and enabled (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        result = inspect_registry(args.config)
    except Exception as exc:  # noqa: BLE001 - CLI must convert malformed registries to a receipt.
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    missing = [
        name for name in args.require
        if name not in result["engines"] or not result["engines"][name]["available"]
    ]
    result["required"] = args.require
    result["missing_required"] = missing
    print(json.dumps(result, indent=2, sort_keys=True))
    return 3 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
