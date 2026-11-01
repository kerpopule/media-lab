"""Model licences of the engines Media Lab can drive, and the public defaults.

A fresh install must only offer engines anyone may use, including commercially.
Engines whose weights carry a personal, non-commercial, research-only or
territory-restricted licence are therefore OFF by default: the studio refuses
their jobs with a plain message and the page shows them greyed out with a
"personal / non-commercial" badge. A host whose use fits the licence opts in
per engine in its gitignored config/local.env:

    MEDIA_LAB_PERSONAL_ENGINES=yue2,kontext      # or: all

The licence facts here are summaries for the UI; the licence texts linked from
docs/ENGINE-LICENCES.md are what binds. Whoever enables an engine accepts its
licence themselves. Stdlib only (the runner scripts import local_config).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import local_config

OPEN = "open"            # commercial use allowed (possibly with conditions)
PERSONAL = "personal"    # personal / non-commercial / restricted: off by default

PERSONAL_BADGE = "personal / non-commercial"


@dataclass(frozen=True)
class EngineLicence:
    id: str
    name: str
    licence: str
    url: str
    kind: str
    notice: str

    @property
    def personal(self) -> bool:
        return self.kind == PERSONAL


LICENCES: dict[str, EngineLicence] = {e.id: e for e in (
    # ---- personal / non-commercial / restricted: off unless the host opts in
    EngineLicence(
        "h3", "MiniMax H3 (local)", "MiniMax H3 Community License",
        "https://huggingface.co/MiniMaxAI", PERSONAL,
        "MiniMax H3 weights carry MiniMax's own licence, which limits where and how they "
        "may be used (it excludes some countries and sets attribution and revenue "
        "conditions). Enable it only if your use and location fit those terms."),
    EngineLicence(
        "h3-singularity", "Real / Long (MiniMax H3 Singularity)",
        "MiniMax H3 Community License + community fine-tune and LoRA terms",
        "https://huggingface.co/MiniMaxAI", PERSONAL,
        "A community fine-tune of MiniMax H3 with community LoRAs, run for personal and "
        "internal testing only: MiniMax's H3 licence applies to it, and nothing about it is "
        "cleared for public, customer or commercial use."),
    EngineLicence(
        "yue2", "YuE2", "CC BY-NC 4.0 (weights)",
        "https://github.com/multimodal-art-projection/YuE", PERSONAL,
        "Non-commercial use only (YuE2 weights are CC BY-NC 4.0)"),
    EngineLicence(
        "kontext", "FLUX.1 Kontext [dev]", "FLUX.1 [dev] Non-Commercial License",
        "https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev", PERSONAL,
        "FLUX.1 Kontext [dev] may only be run for non-commercial purposes (its pictures may "
        "be used commercially, but not to train a competing model)."),
    EngineLicence(
        "qwen-image-21", "Qwen-Image-2.1", "Qwen Research License",
        "https://huggingface.co/Qwen", PERSONAL,
        "Qwen-Image-2.1 is licensed for research and evaluation only; commercial use needs "
        "a separate licence from Qwen."),
    EngineLicence(
        "hunyuan-avatar", "HunyuanVideo-Avatar", "Tencent Hunyuan Community License",
        "https://github.com/Tencent-Hunyuan/HunyuanVideo-Avatar", PERSONAL,
        "The Tencent Hunyuan Community License does not apply in the EU, the UK or South "
        "Korea and adds conditions for very large services."),
    # ---- commercially usable (some with conditions): on by default
    EngineLicence(
        "ltx25", "LTX-2.5", "LTX-2 Community License",
        "https://github.com/Lightricks/LTX-2", OPEN,
        "Free for commercial use by organisations under US$10M annual revenue; larger "
        "organisations need a paid licence from Lightricks."),
    EngineLicence(
        "music3", "MiniMax Music 3", "MiniMax Music 3 Community License",
        "https://huggingface.co/MiniMaxAI", OPEN,
        "Commercial use is allowed below US$20M yearly revenue, crediting MiniMax Music 3."),
    EngineLicence(
        "qwen", "Qwen-Image / Qwen-Image-Edit", "Apache-2.0",
        "https://github.com/QwenLM/Qwen-Image", OPEN, ""),
    EngineLicence(
        "fal", "fal.ai cloud", "your fal.ai account terms",
        "https://fal.ai/terms", OPEN,
        "Runs on your own fal.ai account; each hosted model's terms apply to you."),
    EngineLicence("acestep", "ACE-Step", "Apache-2.0",
                  "https://github.com/ace-step/ACE-Step", OPEN, ""),
    EngineLicence("wan22", "Wan2.2 TI2V-5B", "Apache-2.0",
                  "https://github.com/Wan-Video/Wan2.2", OPEN, ""),
    EngineLicence("chatterbox", "Chatterbox", "MIT",
                  "https://github.com/resemble-ai/chatterbox", OPEN, ""),
    EngineLicence("triposr", "TripoSR", "MIT",
                  "https://github.com/VAST-AI-Research/TripoSR", OPEN, ""),
    EngineLicence("birefnet", "BiRefNet", "MIT",
                  "https://github.com/ZhengPeng7/BiRefNet", OPEN, ""),
)}

# Request- and host-level names that run on one of the licensed engines above.
ALIASES = {
    "h3-ltx25": "h3", "sol-h3": "h3", "minimax-h3": "h3",
    "h3-real": "h3-singularity", "singularity": "h3-singularity",
    "flux-kontext": "kontext",
    "qwen-image-21-gpu": "qwen-image-21",
    "hunyuan_avatar": "hunyuan-avatar",
    "ltx": "ltx25", "music": "music3",
    "fal-video": "fal", "fal-image": "fal",
    "acestep-gpu": "acestep", "wan22-ti2v-5b-gpu": "wan22",
    "chatterbox-english-cpu": "chatterbox", "triposr-cpu": "triposr", "birefnet-cpu": "birefnet",
}


def canonical(engine: str) -> str:
    key = str(engine or "").strip().lower()
    return ALIASES.get(key, key)


def licence(engine: str) -> EngineLicence | None:
    return LICENCES.get(canonical(engine))


def is_personal(engine: str) -> bool:
    lic = licence(engine)
    return bool(lic and lic.personal)


def enabled(engine: str, opted_in: set[str] | None = None) -> bool:
    """True unless the engine is personal-licensed and this host has not opted in."""
    lic = licence(engine)
    if lic is None or not lic.personal:
        return True
    opted_in = local_config.personal_engines() if opted_in is None else opted_in
    return "all" in opted_in or lic.id in {canonical(e) for e in opted_in}


def refusal(engine: str) -> str:
    """The plain-language reason a personal engine is not available here."""
    lic = licence(engine)
    if lic is None:
        return f"{engine} is not available on this studio."
    notice = lic.notice if lic.notice.endswith((".", "!", "?")) else lic.notice + "."
    return (f"{lic.name} is off on this studio: its licence is {PERSONAL_BADGE} "
            f"({lic.licence}). {notice} The studio's owner can switch it on in "
            f"config/local.env with MEDIA_LAB_PERSONAL_ENGINES={lic.id}.")


def public_view() -> dict:
    """What the page needs to badge and gate engine choices."""
    opted_in = local_config.personal_engines()
    return {
        "badge": PERSONAL_BADGE,
        "engines": {
            lic.id: {"name": lic.name, "licence": lic.licence, "url": lic.url,
                     "personal": lic.personal, "notice": lic.notice,
                     "enabled": enabled(lic.id, opted_in)}
            for lic in LICENCES.values()
        },
        "aliases": dict(ALIASES),
    }
