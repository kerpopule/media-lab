# Engine licences and the public defaults

Media Lab drives other people's models. Their licences decide who may run
them and what the results may be used for, so a fresh install only turns on
engines anyone may use, including commercially. The rest stay installed-but-off
until the studio's owner decides their own use fits the licence.

This page summarises; the linked licence texts are what binds. Whoever enables
an engine accepts its licence themselves. Last checked: 2026-09-25.

## Off by default: "personal / non-commercial"

The studio refuses jobs for these engines (HTTP 403 with a plain explanation),
shows their buttons greyed out with a "personal / non-commercial" badge, and
never loads their weights, until the host lists them in its gitignored
`config/local.env`:

```sh
MEDIA_LAB_PERSONAL_ENGINES=h3,yue2        # comma list, or: all
```

| Engine id | Model | Licence | Why it is off by default |
|---|---|---|---|
| `h3` (also `h3-ltx25`) | MiniMax H3, run locally (Sol-H3-Spark) | [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI) | Restricts where and how the weights may be used (published terms exclude some countries) and adds attribution and revenue conditions. |
| `h3-singularity` (also `h3-real`) | Real / Long: the community MiniMax H3 Singularity fine-tune with community LoRAs, run locally | [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI) plus the fine-tune's and LoRAs' own terms | Everything that limits `h3`, and nothing about the fine-tune is cleared for public, customer or commercial use. Needs `h3` enabled too. |
| `yue2` | YuE2 music | [CC BY-NC 4.0 weights](https://github.com/multimodal-art-projection/YuE) (code Apache-2.0) | Non-commercial use only, including the songs it makes. |
| `kontext` | FLUX.1 Kontext [dev] | [FLUX.1 [dev] Non-Commercial License](https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev) | The model may only be run for non-commercial purposes (its pictures may be used commercially, but not to train a competing model). |
| `qwen-image-21` | Qwen-Image-2.1 (the studio host's image engine) | [Qwen Research License](https://huggingface.co/Qwen) | Research and evaluation only; commercial use needs a separate licence. |
| `hunyuan-avatar` | HunyuanVideo-Avatar | [Tencent Hunyuan Community License](https://github.com/Tencent-Hunyuan/HunyuanVideo-Avatar) | Does not apply in the EU, the UK or South Korea; conditions for very large services. |

Where the switch is enforced: video jobs (`/api/generate`, music videos,
talking-head takes) refuse `h3`/`h3-ltx25`, and Real / Long (`h3-real`) needs
both `h3` and `h3-singularity`; `/api/music` and the score, cover
and re-arrange tools refuse `yue2`, and the default music engine becomes
MiniMax Music 3; the Kontext painter disappears from the image and character
pickers; the avatar finisher reports "not installed"; the studio host will not
start the Qwen-Image-2.1 pack; and, whatever route queued the work, the engine
loader will not boot a disabled engine.

## On by default (commercial use allowed, some with conditions)

| Engine id | Model | Licence | Notes |
|---|---|---|---|
| `ltx25` | LTX-2 / LTX-2.5 video | [LTX-2 Community License](https://github.com/Lightricks/LTX-2) | Free commercial use for organisations under US$10M annual revenue; larger ones need a paid licence. The public installer is still blocked (private Maestro runtime). |
| `music3` | MiniMax Music 3 | MiniMax Music 3 Community License | Commercial use below US$20M yearly revenue; credit "MiniMax Music 3". |
| `qwen` | Qwen-Image / Qwen-Image-Edit | [Apache-2.0](https://github.com/QwenLM/Qwen-Image) | The "Precise" painter. |
| `fal` | fal.ai cloud (FLUX, Recraft, MiniMax H3 Max, Veo 3, Kling, ...) | [your fal.ai account terms](https://fal.ai/terms) | Runs on your own account; each hosted model's terms apply to you. |
| `acestep` | ACE-Step (studio host music) | [Apache-2.0](https://github.com/ace-step/ACE-Step) | |
| `wan22` | Wan2.2 TI2V-5B (studio host video) | [Apache-2.0](https://github.com/Wan-Video/Wan2.2) | |
| `chatterbox` | Chatterbox (speech) | [MIT](https://github.com/resemble-ai/chatterbox) | |
| `triposr` | TripoSR (3D) | [MIT](https://github.com/VAST-AI-Research/TripoSR) | |
| `birefnet` | BiRefNet (background removal) | [MIT](https://github.com/ZhengPeng7/BiRefNet) | |

Not yet reviewed here (check before commercial use): LatentSync and MuseTalk
lip-sync, SAM 3 selection, Voicebox's voice models, the Mel-Band RoFormer
stem separator. They are only offered where the owner installed them by hand.

## For developers

The table lives in `media_lab_core/engine_licences.py` (`LICENCES`,
`ALIASES`); `GET /api/engines/licences` serves it to the page. Adding an
engine means adding a row there with its licence class, and — if it is
`personal` — calling `engine_licences.enabled()` wherever its jobs enter.
