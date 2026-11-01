# The local overlay: a studio's own material, outside git

The public repository ships neutral defaults: a handful of looks, the style
shelves, generic templates, no people. Whatever belongs to one studio — its
brand looks, its private prompt templates and their previews, a catalog of
characters, what the assistant should know about the household or the team —
goes in the gitignored folder `config/local/`:

- under the data root (`$MEDIA_LAB_HOME/config/local/`, which on a deployed
  host is the same tree the code runs from), and/or
- under a development checkout (`media-lab/config/local/`).

When both exist, the data root's file wins. Nothing in this folder is ever
committed (`.gitignore`), scanned by the identity guard, written, or deleted
by `tools/deploy-spark.sh` (it is on the deploy's protect list). Per-host
*settings* (addresses, paths, which licence-restricted engines are on) stay in
`config/local.env`; see `config/local.env.example`.

Every file is optional. A malformed file is skipped with one line in the
server log; it never stops the studio from starting.

## `themes.json` — extra looks

```json
{"themes": [
  {"id": "harbour", "label": "Harbour — Teal", "accent": "#43CECA", "ink": "#0F0F11",
   "vars": {"--ink-2": "#141417", "--gold-hi": "#7EE3E0", "--gold-2": "#168180",
            "--on-accent": "#04211F", "--bg-a": "#0A2E2E", "--bg-b": "#12181C"}},
  {"id": "linen", "label": "Linen — Light", "accent": "#0A7A83", "ink": "#FFF8E7", "light": true,
   "vars": {"--t-1": "#063447", "--line": "rgba(6,52,71,.2)", "--panel": "rgba(255,248,231,.96)"}}
]}
```

- `id`: lower-case letters, digits and dashes. `accent` and `ink`: `#hex`
  colours (the swatch, the page ground and the installed app's status bar).
- `vars`: any of the studio's CSS custom properties (`--ink`, `--ink-2`,
  `--gold`, `--gold-hi`, `--gold-2`, `--on-accent`, `--on-accent-dim`,
  `--t-1`..`--t-3`, `--bg-a`, `--bg-b`, `--glass`, `--glass-strong`,
  `--sheen`, `--line`, `--lift1`, `--lift2`, `--edge`, `--nav-bg`, `--panel`,
  `--field`, `--field-strong`, ...). Values that could break out of CSS or load
  anything (`;`, braces, `url(`, `@import`) are dropped.
- `light: true` repaints hairline borders and text fields for a light ground.

The looks appear in the 🎨 sheet of the studio and in Cut, served from
`/local/themes.css` and `/api/local/themes`.

## `templates.json`, `prompt-templates/`, `templates/` — your own templates

```json
{"groups": [
  {"group": "Our templates", "templates": [
    {"id": "rainy-night-mv", "emoji": "🌧️", "label": "Rainy-night music video",
     "prompt": "Cinematic narrative music video, rain-streaked glass, ...",
     "gif": "rainy-night-mv.gif",
     "description": "One performer, one car, one rainy night."},
    {"id": "paper-explainer", "emoji": "✂️", "label": "Paper-motion explainer",
     "prompt_template": "paper-explainer",
     "gif": "paper-explainer.gif",
     "description": "A 32-beat cut-paper explainer."}
  ]}
]}
```

- `prompt` is the prefix the template adds; or `prompt_template` names a long
  prompt kept as `prompt-templates/<id>.json` in the same folder, in the
  `media_lab.prompt_template.v1` shape (`{"template_id": "<id>", "prompt": "..."}`
  plus any provenance and usage notes you keep with it).
- `gif` is a bare file name, looked up in `static/templates/` and then in this
  folder's `templates/` (served at `/local/templates/<name>`). Without one the
  page shows the emoji tile.

The groups are appended to the Video tab's template shelves at start-up
(restart the studio after editing).

## `known-characters.json` — a prompt-only character catalog

Same shape the studio has always read:

```json
{"schema_version": 1,
 "source": {"dataset": "...", "license": "..."},
 "characters": [
   {"id": "known:0001", "name": "Captain Example", "actor": "A. Performer",
    "franchise": "Example Saga", "status": "good"}]}
```

`status` is `good`, `onthefence` or `bad`; ids start with `known:`. Entries
appear after the studio's own characters in every cast picker, as prompt-only
presets (no identity sheet). Mind likeness and trademark rights: this is why
no catalog ships with the public repository. Optional face thumbnails go in
`media/known-thumbs/<id-without-prefix>.jpg`.

## `sparky.md` — notes for the assistant

Plain text appended to Sparky's system prompt under "THIS STUDIO": who the
regular performers are, house rules, what hardware the studio runs on. At most
8,000 characters are used.
