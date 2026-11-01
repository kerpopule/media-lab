"""The per-studio overlay: a studio owner's own looks and material, kept out of git.

The public repository ships neutral defaults only. A studio that wants more --
its own brand themes, private prompt templates, a known-character catalog,
notes for the Sparky assistant -- drops files into the gitignored folder
``config/local/`` under its data root ($MEDIA_LAB_HOME/config/local/, which on a
deployed host is the same tree the code runs from) or under a dev checkout.
Nothing there is ever committed, deployed over, or deleted by a deploy.

Files (all optional; docs/LOCAL-OVERLAY.md has the formats):

  themes.json               extra looks for the theme sheet
  templates.json            extra template groups for the Video tab
  prompt-templates/<id>.json long attributed prompts the templates point at
  templates/<name>.gif      their preview animations (served at /local/templates/)
  known-characters.json     a prompt-only known-character catalog
  sparky.md                 private notes appended to Sparky's system prompt

A malformed file is skipped (and named in the server log); it never stops the
studio from starting. Stdlib only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import local_config

THEME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
CSS_VAR = re.compile(r"^--[a-z0-9-]{1,40}$")
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(gif|webp|png|jpg|jpeg|mp4)$")
# A CSS value may not break out of its declaration or load anything.
_CSS_VALUE_BAD = re.compile(r"[;{}<>\\]|url\s*\(|expression\s*\(|@import", re.I)

_warned: set[str] = set()


def _warn(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"[media-lab] local overlay: {msg}", flush=True)


def dirs() -> list[Path]:
    return local_config.overlay_dirs()


def find(name: str) -> Path | None:
    """The highest-precedence overlay file with this relative name."""
    for d in dirs():
        p = d / name
        if p.is_file():
            return p
    return None


def load_json(name: str, default):
    p = find(name)
    if p is None:
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(f"{p} is not valid JSON ({exc}); ignored")
        return default


# ---------------------------------------------------------------- themes
def themes() -> list[dict]:
    """Validated extra looks: [{id, label, accent, ink, vars:{--name: value}}]."""
    raw = load_json("themes.json", [])
    rows = raw.get("themes", []) if isinstance(raw, dict) else raw
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "")
        accent, ink = str(row.get("accent") or ""), str(row.get("ink") or "")
        if not THEME_ID.match(tid) or tid in seen or not COLOR.match(accent) or not COLOR.match(ink):
            _warn(f"theme {tid or '?'} needs an id, an #accent and an #ink colour; skipped")
            continue
        variables = {}
        given = row.get("vars")
        for key, value in (given.items() if isinstance(given, dict) else ()):
            key, value = str(key), str(value)
            if CSS_VAR.match(key) and value and len(value) <= 300 and not _CSS_VALUE_BAD.search(value):
                variables[key] = value
        variables.setdefault("--ink", ink)
        variables.setdefault("--gold", accent)
        seen.add(tid)
        out.append({"id": tid, "label": str(row.get("label") or tid)[:60],
                    "accent": accent, "ink": ink, "vars": variables,
                    "light": bool(row.get("light"))})
    return out


def themes_css() -> str:
    """One CSS block per overlay theme, in the same shape as the built-in ones."""
    blocks = []
    for t in themes():
        decls = ";".join(f"{k}:{v}" for k, v in t["vars"].items())
        blocks.append(f':root[data-theme="{t["id"]}"]{{{decls}}}')
        if t["light"]:
            # light grounds: the dark theme's white hairlines vanish, so repaint
            # them and give text fields a light surface (as the built-in Paper look)
            blocks.append(
                f':root[data-theme="{t["id"]}"] :is(.chip,.btn2,.qbtn,.card,.songrow,textarea,'
                f'input[type=text],.togglechip,.beat,.gal .it,#lib_grid .it,.edit){{border-color:var(--line)}}')
            blocks.append(
                f':root[data-theme="{t["id"]}"] :is(textarea,input[type=text])'
                f'{{background:rgba(255,255,255,.7);color:var(--t-1)}}')
    return "\n".join(blocks) + ("\n" if blocks else "")


def theme_ink(theme_id: str) -> str | None:
    for t in themes():
        if t["id"] == theme_id:
            return t["ink"]
    return None


# ---------------------------------------------------------------- templates
def prompt_template_path(template_id: str) -> Path | None:
    if not TEMPLATE_ID.match(template_id or ""):
        return None
    return find(f"prompt-templates/{template_id}.json")


def asset_path(name: str) -> Path | None:
    """A template preview file, by bare name (no directories)."""
    if not ASSET_NAME.match(name or ""):
        return None
    return find(f"templates/{name}")


def template_groups(load_prompt) -> list[tuple[str, list[tuple]]]:
    """Extra Video-tab template groups in TEMPLATE_LIB's shape:
    [(group, [(id, emoji, label, prompt_prefix, gif, description), ...])].

    Each template gives either "prompt" (inline) or "prompt_template" (the id of
    an overlay prompt-templates/<id>.json, read through ``load_prompt``). "gif"
    is a bare file name in the overlay's templates/ folder or under
    static/templates/."""
    raw = load_json("templates.json", [])
    groups = raw.get("groups", []) if isinstance(raw, dict) else raw
    out: list[tuple[str, list[tuple]]] = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict) or not str(g.get("group") or "").strip():
            continue
        entries = []
        listed = g.get("templates")
        for t in listed if isinstance(listed, list) else []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not TEMPLATE_ID.match(tid):
                _warn(f"template id {tid!r} is not a slug; skipped")
                continue
            prompt = t.get("prompt")
            if not isinstance(prompt, str) and t.get("prompt_template"):
                try:
                    prompt = load_prompt(str(t["prompt_template"]))
                except Exception as exc:  # malformed or missing prompt file
                    _warn(f"template {tid}: {exc}; skipped")
                    continue
            if not isinstance(prompt, str) or not prompt.strip():
                _warn(f"template {tid} has no prompt; skipped")
                continue
            gif = str(t.get("gif") or "")
            if gif and not ASSET_NAME.match(gif):
                gif = ""
            entries.append((tid, str(t.get("emoji") or "🎞")[:4], str(t.get("label") or tid)[:80],
                            prompt, gif, str(t.get("description") or "")[:400]))
        if entries:
            out.append((str(g["group"])[:80], entries))
    return out


# ---------------------------------------------------------------- characters / Sparky
def known_characters_file() -> Path | None:
    return find("known-characters.json")


def sparky_notes() -> str:
    p = find("sparky.md")
    if p is None:
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
