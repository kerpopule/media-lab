# Music in Media Lab

Two song engines live behind the Music tab. **YuE2** is the only one with the
edit tools, but its weights are non-commercial (CC BY-NC 4.0), so it is **off
on a fresh install** and the Music tab defaults to MiniMax **Music 3**. A host
whose use fits the licence opts in with `MEDIA_LAB_PERSONAL_ENGINES=yue2` in
`config/local.env` (see [ENGINE-LICENCES.md](ENGINE-LICENCES.md)); YuE2 then
becomes the default for every new song. Music 3 always records the screenshot
songs, whose Director QA contract was measured against it.

| | YuE2 (`engine: "yue2"`) | Music 3 (`engine: "music3"`) |
|---|---|---|
| How it works | writes an ABC **score** (chain-of-thought), then sings/plays it | renders straight from a three-paragraph caption |
| Residency slot | `ENGINES["yue2"]`, unit `media-lab-yue2.service`, :8197 | `ENGINES["music"]`, unit `media-lab-comfy-music.service`, :8196 |
| Measured (DGX Spark) | ~4 s load, 16-17 GiB peak, ~200 s per 60-90 s song (AR ~39 tok/s + ~60 s VAE decode) | ~15 GiB, see `config/companion-residency-policy.json` |
| Output | 48 kHz stereo FLAC → `media/<id>.mp3` + `<id>-wave.png` | same |
| Edit tools | edit score, re-arrange, cover, stems | stems only |
| Weights licence | **CC BY-NC 4.0** | see the Music 3 terms |

## Licence notice

The YuE2 weights are licensed CC BY-NC 4.0. Every YuE2-made song carries
`license: "CC-BY-NC-4.0"` in its job record and gallery row, and the studio
shows a small `⚠︎` next to the engine name and on every YuE2 song. Hovering or
tapping it says exactly:

> Non-commercial use only (YuE2 weights are CC BY-NC 4.0)

Stems split from a YuE2 song inherit the engine and licence fields.

## Making a song

`POST /api/music` (behind the session gate, like everything under `/api`):

```json
{"vibe": "slow rainy-night jazz about missing someone",
 "lyrics": "", "length": "auto", "duration_seconds": null,
 "engine": "yue2", "style": "", "cot": "full", "abc": "",
 "reference_song_id": "", "seed": null, "instrumental": false}
```

* `engine` — `yue2` or `music3`; empty = the host's default (YuE2 where
  enabled, else Music 3). A disabled YuE2 answers 403 with the licence reason.
* `style` — one line of genre / mood / instruments / voice for YuE2. Empty means
  the studio derives it from the songwriter's caption (its *Global Metadata*
  paragraph).
* `cot` — YuE2 chain-of-thought: `full` (plan the whole score), `melody`
  (plan the melody only) or `off`.
* `abc` — an edited score to record from (max 60 000 characters).
* `reference_song_id` — make a **cover**: the library song is transcribed with
  SheetSage2 (`POST /transcribe` on the shim), its chord symbols are stripped,
  and YuE2 re-harmonises the melody under the new style (`cot` becomes
  `melody`). Works for uploaded songs too.
* `seed` — fixes the first take; Director repair takes use fresh seeds.
* `instrumental` — no vocals (`[Instrumental]` for YuE2, an empty lyric for
  Music 3).

The songwriter (Qwen, `MUSIC_SYS`) still writes the caption and tagged lyrics
for both engines; Director QA (Whisper transcript vs. lyrics, three takes) is
unchanged. Job fields added for YuE2: `engine`, `license`, `style_line`,
`cot`, `abc`, `yue2` (seconds / elapsed / seed / truncated / timing), and
`jobs/<id>/score.abc` + `result.json` on disk.

Music 3 keeps its ETA key (`music/<secs>/<warm|cold>`); YuE2 songs are tracked
under `music/yue2/<secs>/<warm|cold>`.

## Edit tools

All under `/api/music`, all behind the session gate. The library rows carry the
matching buttons (`✏️ Edit score`, `↺ Re-arrange`, `🎤 Cover`, `🎚 Stems`).

| Route | What it does |
|---|---|
| `GET /api/music/engines` | the engine list (YuE2 first), warm state, whether stems are installed |
| `POST /api/music/plan` `{style, lyrics, cot, seed}` → `{abc}` | ask YuE2 for a score without recording (synchronous; brings the engine up) |
| `GET /api/music/{id}/abc` | the score a song was recorded from (404 for uploads) |
| `POST /api/music/{id}/rearrange` `{style?, lyrics?, abc?, vibe?}` | a new job that records that song's score (or the edited `abc`) again; the songwriter is skipped when a style line is present |
| `POST /api/music/{id}/cover` `{style?, vibe?}` | a new job: transcribe → strip chords → record the melody under the new style |
| `POST /api/music/{id}/stems` | a `stems` job: Mel-Band RoFormer splits `media/{id}.mp3` into *vocals* and *instrumental*; both are filed as music assets (gallery rows with `stem`, `stem_of`) |

Stems are ordinary music assets: they show in the song library and Cut places
them on the music track like any other song (no Cut change was needed).

## Runtime layout (per host, `config/local.env`)

```
YUE2_KIT=~/runtime/yue2-iso          # .venv (torch + yue2), YuE/ checkout,
                                     # .venv-sheetsage (SheetSage2 transcription)
YUE2_MODELS_ROOT=~/.local/share/media-lab-p3-models/yue2
                                     # YuE2-3B  YuE2-Vae  SheetSage2  MERT-v2-FullSong
YUE2_PORT=8197
MELBAND_ROFORMER_ROOT=~/runtime/melband-roformer-0.1.5
                                     # .venv/bin/melband-roformer-infer, models/<model>
MELBAND_ROFORMER_MODEL=melband-roformer-kim-vocals
```

* `runner/yue2_engine_server.py` — the shim (stdlib HTTP server on
  `127.0.0.1:$YUE2_PORT`): `GET /health`, `POST /generate`, `/plan`,
  `/transcribe`, `/interrupt`. Renders go to
  `$MEDIA_LAB_HOME/pool/music-out/job-<request_id>/`; re-requesting an id
  answers from that directory (`cached: true`). It holds the inference lock
  (`/run/user/1000/media-lab-inference.lock`) itself and answers **409** when
  another engine has it — which is why `app.py` does *not* take the lock
  around YuE2 calls (it does for Sol/LTX).
* `runner/start_yue2_engine.sh` — sources `runner/local_env.sh`, exports the
  `YUE2_*` keys and execs the shim on the kit's venv. `app.py` starts it as
  the transient unit `media-lab-yue2.service` through `systemd-run`.
* `config/media-lab-yue2.service` — the same thing as a unit file for hosts
  that prefer `systemctl --user start` (not installed by default).
* Residency: `yue2` is a companion-slot member
  (`config/companion-residency-policy.json`), reaped after the same 60-minute
  idle window as Music 3, stood down for Maestro / LTX restore, and given room
  by the same memory guard (`gb: 18`, `boot_wait: 240`).

## Rolling back to Music 3 as the default

Change `engine: str = "yue2"` to `"music3"` in `MusicReq`, the fallback in
`_music_engine_for()` and the `default` in `/api/music/engines`, and move the
`sel` class to the Music 3 chip in `static/index.html`. Nothing else depends
on the default; YuE2 stays selectable.
