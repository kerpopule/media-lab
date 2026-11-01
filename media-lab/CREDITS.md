# Credits

Media Lab stands on a lot of great work. Some of this attribution is
required by licenses; most of it is here because we believe in crediting
what we build on, whether required or not.

## Engines & models (referenced, not bundled)

No model weights ship in this repository. The studio orchestrates engines the
owner installs and licenses themselves. Engines whose licence is personal,
non-commercial, research-only or territory-restricted are **off by default**
and marked "personal / non-commercial" in the studio; a host opts in per engine
(`MEDIA_LAB_PERSONAL_ENGINES` in `config/local.env`). The full table, with
links to each licence, is [docs/ENGINE-LICENCES.md](docs/ENGINE-LICENCES.md).

- **Maestro / WanGP** by deepbeepmeep — the video-engine runtime our H3
  engine container is a pinned fork of.
- **MiniMax H3** — video generation model, under MiniMax's own H3 licence
  (restricted; off by default). This repo contains integration code, never
  the model.
- **LTX-2 / LTX-2.5** by Lightricks — fast video drafts, under the LTX-2
  Community License (commercial use free below a revenue threshold).
- **YuE2** by M-A-P — music with editable scores. Code Apache-2.0; weights
  CC BY-NC 4.0 (non-commercial; off by default).
- **MiniMax Music 3** — music, under the MiniMax Music 3 Community License.
- **FLUX.1 Kontext [dev]** by Black Forest Labs — likeness-keeping image
  edits, under the FLUX.1 [dev] Non-Commercial License (off by default).
- **Qwen-Image / Qwen-Image-Edit** by Qwen (Alibaba) — image generation and
  editing, Apache-2.0.
- **Qwen-Image-2.1** by Qwen (Alibaba) — the studio host's image engine
  (generation, editing, transparency). Released under the Qwen Research
  License: research or evaluation use only, and commercial use requires a
  separate license from Qwen (off by default).
- **HunyuanVideo-Avatar** by Tencent — audio-driven avatars, under the
  Tencent Hunyuan Community License (territory limits; off by default).
- **ACE-Step**, **Wan2.2**, **Chatterbox**, **TripoSR**, **BiRefNet** — the
  studio host's music, video, speech, 3D and background-removal engines
  (Apache-2.0 / MIT).
- **ComfyUI** and its node ecosystem — image and music pipelines.
- **fal.ai** — optional cloud rendering provider (bring your own key).

## Ideas & prior art

- **minimax-h3-prompt-composer** by BMB12d3 — camera-path planning and H3
  prompt-composition ideas informed our tooling. That repo has no license,
  so we adapted ideas only, never code.
- **MiniMax H3's style skills** (github.com/MiniMax-AI/MiniMax-H3) inspired
  the "Official H3 templates" shelf. The prompt prefixes are our own words;
  the skills' example animations are not redistributed here (their repository
  carries no licence), so a fresh studio shows an emoji tile instead.
- The **malcolmrey** community character index informed the known-characters
  picker. No character catalog ships here: a studio may keep its own in its
  gitignored local overlay (docs/LOCAL-OVERLAY.md). Mind likeness and
  trademark rights before you do.
- Countless prompt techniques came from the open AI-video community on X.

## Image template collection

`static/template-library/` holds prompt records and example images from
[freestylefly/awesome-gpt-image-2](https://github.com/freestylefly/awesome-gpt-image-2)
(MIT for the project's code and data). The examples are other people's work:
as that project itself says, its MIT licence does not grant commercial rights
to each third-party prompt or image. Treat them as learning references and ask
the original author before commercial use. Cases showing franchise characters or
branding, real people, or real brands' logos and products, or marked private or
non-redistributable, have been removed; the provenance rules for what may ship
are in `static/template-library/NOTICE.md`.

## Design & type

- **Space Grotesk** by Florian Karsten and **Barlow** by Jeremy Tribby —
  both under the SIL Open Font License, served via Google Fonts.
- The Co-Agent NOIR design language is our own (co-agent.us).

## Foundation

- **FastAPI**, **Uvicorn**, **Pydantic** — the server.
- The original `media-lab-simple` was built for a single NVIDIA DGX Spark,
  and this studio keeps that heritage.

If you see your work here uncredited, open an issue — we'll fix it.
