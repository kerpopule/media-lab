from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

from .model_catalog import ModelOption, load_model_catalog, resolve_selection

CATEGORY_TITLES = {
    "video": "Video generation",
    "image": "Images and likeness preparation",
    "audio": "Speech and audio",
    "model": "3D game assets",
    "llm": "Local setup/chat model",
    "enhancement": "Optional enhancement",
}

DEFAULT_CATALOG = Path(__file__).with_name("data") / "models.example.toml"


def _label(model: ModelOption) -> str:
    recommended = " · recommended" if model.recommended else ""
    unavailable = " · not yet qualified" if not model.selectable else ""
    disk = f"{model.disk_gb:g} GB" if model.disk_gb > 0 else "not measured"
    memory = f"{model.memory_floor_gb:g} GB" if model.memory_floor_gb > 0 else "not measured"
    return (
        f"{model.name}{recommended}{unavailable}\n"
        f"    Speed: {model.speed} · Quality: {model.quality} · "
        f"Disk: {disk} · Memory floor: {memory}\n"
        f"    {model.summary}"
    )


def choose_interactively(catalog: dict[str, ModelOption]) -> list[str]:
    chosen: list[str] = []
    print("\nMedia Lab capability setup")
    print("Choose any combination. Unqualified entries are shown for transparency but cannot install.\n")
    for category, title in CATEGORY_TITLES.items():
        models = [model for model in catalog.values() if model.category == category]
        if not models:
            continue
        print(f"== {title} ==")
        for model in models:
            print(f"[{model.id}] {_label(model)}")
            if not model.selectable:
                print(f"    Blocked: {model.refusal_reason()}")
                continue
            answer = input("    Include this capability? [y/N] ").strip().lower()
            if answer in {"y", "yes"}:
                chosen.append(model.id)
        print()
    return chosen


def make_plan(catalog_path: Path, models: list[ModelOption], hardware_profile: str) -> dict:
    for model in models:
        if not model.selectable:
            raise ValueError(f"{model.id} is not selectable: {model.refusal_reason()}")
        if any(not math.isfinite(value) or value < 0 for value in (model.disk_gb, model.memory_floor_gb)):
            raise ValueError(f"{model.id}: disk and memory estimates must be finite nonnegative values")
    licenses = [model for model in models if model.terms_acceptance_required]
    exclusive_groups: dict[str, list[str]] = {}
    for model in models:
        if model.exclusive_group:
            exclusive_groups.setdefault(model.exclusive_group, []).append(model.id)
    return {
        "schema_version": 1,
        "catalog": str(catalog_path.resolve()),
        "hardware_profile": hardware_profile,
        "hardware_verified": False,
        "hardware_requirements": {model.id: list(model.hardware) for model in models},
        "memory_floor_gb": max((model.memory_floor_gb for model in models), default=0),
        "resource_estimates_complete": all(model.disk_gb > 0 and model.memory_floor_gb > 0 for model in models) if models else False,
        "resource_scope": "Catalog estimates only. Verify available memory, disk, drivers and platform on the execution host before downloading.",
        "selected": [model.id for model in models],
        "download_disk_gb": round(sum(model.disk_gb for model in models), 3),
        "terms_acceptance_required": [model.id for model in licenses],
        "runtime_swap_groups": exclusive_groups,
        "models": [asdict(model) for model in models],
        "next_stage": "choose-capabilities" if not models else "license-review" if licenses else "hardware-preflight",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan a modular Media Lab installation")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--select", action="append", default=[], metavar="MODEL_ID")
    parser.add_argument("--hardware-profile", default="auto-detect")
    parser.add_argument("--output-plan", type=Path)
    parser.add_argument("--inspect-host", action="store_true", help="Read RAM and disk on this execution host; does not qualify hardware")
    parser.add_argument("--storage-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="Emit JSON only; never prompt or select capabilities implicitly")
    parser.add_argument("--list", action="store_true", help="show the catalog and exit")
    args = parser.parse_args(argv)
    try:
        catalog = load_model_catalog(args.catalog)
        if args.list:
            if args.json:
                print(json.dumps({"schema_version": 1, "models": [
                    {**asdict(model), "selectable": model.selectable,
                     "refusal_reason": model.refusal_reason() if not model.selectable else None}
                    for model in catalog.values()
                ]}, indent=2, sort_keys=True))
                return 0
            for category, title in CATEGORY_TITLES.items():
                models = [model for model in catalog.values() if model.category == category]
                if models:
                    print(f"\n== {title} ==")
                    for model in models:
                        print(f"[{model.id}] {_label(model)}")
                        if not model.selectable:
                            print(f"    Blocked: {model.refusal_reason()}")
            return 0
        selected = args.select if args.select or args.json else choose_interactively(catalog)
        models = resolve_selection(catalog, selected)
        plan = make_plan(args.catalog, models, args.hardware_profile)
        if args.inspect_host:
            from .host_resources import inspect_host_resources
            plan["host_resources"] = inspect_host_resources(models,args.storage_root)
        payload = json.dumps(plan, indent=2, sort_keys=True)
        if args.output_plan:
            args.output_plan.parent.mkdir(parents=True, exist_ok=True)
            args.output_plan.write_text(payload + "\n")
            if not args.json:
                print(f"Install plan written to {args.output_plan}")
        if args.json or not args.output_plan:
            print(payload)
        if not args.json:
            print("\nNo models were downloaded. Review and accept required terms before the download stage.")
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}) if args.json else f"Setup blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
