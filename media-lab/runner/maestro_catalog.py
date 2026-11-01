#!/usr/bin/env python3
"""Emit Maestro's live defaults catalog as JSON without loading models."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path("/opt/maestro/app/app")
DEFAULTS = ROOT / "defaults"
CHECKPOINT_ROOTS = [ROOT / "ckpts", Path("/data/ckpts")]

AUDIO_PREFIXES = (
    "ace_step", "chatterbox", "dramabox_audio", "heartmula", "index_tts",
    "kugel", "qwen3_tts", "scenema_audio", "yue", "mmaudio", "stable_audio",
)
IMAGE_PREFIXES = (
    "flux", "pi_flux2", "qwen_image", "z_image", "krea2", "hidream",
    "kiwi_edit", "chrono_edit",
)


def flatten_urls(value):
    out = []
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(flatten_urls(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(flatten_urls(item))
    return out


def symbolic_ids(value):
    out = []
    if isinstance(value, str):
        if not value.startswith(("http://", "https://")):
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(symbolic_ids(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(symbolic_ids(item))
    return out


def output_kind(model_id, architecture):
    low = f"{model_id} {architecture}".lower()
    if low.startswith(AUDIO_PREFIXES) or any(x in low for x in ("_tts", "audio_tts")):
        return "audio"
    if low.startswith(IMAGE_PREFIXES):
        return "image"
    return "video"


def family_for(model_id, architecture, explicit_group, kind):
    if explicit_group:
        return str(explicit_group)
    low = f"{model_id} {architecture}".lower()
    for token, label in (
        ("pi_flux2", "FLUX.2"), ("flux2", "FLUX.2"), ("flux", "FLUX"),
        ("minimax_h3", "MiniMax H3"), ("ltx", "LTX"),
        ("qwen", "Qwen"), ("kandinsky", "Kandinsky"), ("k5_", "Kandinsky"),
        ("hunyuan", "Hunyuan"), ("vace", "VACE"), ("wan", "Wan"),
        ("ace_step", "ACE-Step"), ("chatterbox", "Chatterbox"),
        ("z_image", "Z-Image"), ("krea", "Krea"), ("longcat", "LongCat"),
        ("hidream", "HiDream"), ("kiwi", "Kiwi"),
    ):
        if token in low:
            return label
    return kind.title()


def main():
    installed_index = {}
    for root in CHECKPOINT_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() or path.is_symlink():
                    installed_index.setdefault(path.name, []).append(str(path))
            except OSError:
                pass

    raw = {}
    for filename in sorted(DEFAULTS.glob("*.json")):
        try:
            data = json.loads(filename.read_text(encoding="utf-8"))
        except Exception:
            continue
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        raw[filename.stem] = {"file": filename, "data": data, "model": model}

    def collect_assets(model_id, seen=None):
        seen = set(seen or ())
        if model_id in seen or model_id not in raw:
            return []
        seen.add(model_id)
        model = raw[model_id]["model"]
        urls = []
        for key in ("URLs", "modules", "preload_URLs"):
            value = model.get(key)
            urls.extend(flatten_urls(value))
            for other_id in symbolic_ids(value):
                urls.extend(collect_assets(other_id, seen))
        return list(dict.fromkeys(urls))

    models = []
    for model_id, record in raw.items():
        filename, data, model = record["file"], record["data"], record["model"]
        architecture = str(model.get("architecture") or model_id)
        urls = collect_assets(model_id)
        required_names = [Path(urlparse(url).path).name for url in urls if Path(urlparse(url).path).name]
        present = [name for name in required_names if name in installed_index]
        settings = {k: v for k, v in data.items() if k != "model"}
        kind = output_kind(model_id, architecture)
        fully_installed = bool(required_names) and len(present) == len(required_names)
        models.append({
            "id": model_id,
            "name": str(model.get("name") or model_id.replace("_", " ").title()),
            "description": str(model.get("description") or ""),
            "architecture": architecture,
            "family": family_for(model_id, architecture, model.get("group"), kind),
            "output_kind": kind,
            "defaults": settings,
            "assets": urls,
            "required_asset_names": required_names,
            "installed_asset_names": present,
            "installed": fully_installed,
            "partially_installed": bool(present) and not fully_installed,
            "lazy_download": not fully_installed,
            "source_file": filename.name,
        })

    models.sort(key=lambda item: item["id"])
    groups = Counter(model["family"] for model in models)
    kinds = Counter(model["output_kind"] for model in models)
    payload = {
        "ok": True,
        "count": len(models),
        "installed_count": sum(1 for model in models if model["installed"]),
        "partial_count": sum(1 for model in models if model["partially_installed"]),
        "groups": dict(sorted(groups.items())),
        "output_kinds": dict(sorted(kinds.items())),
        "models": models,
    }
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
