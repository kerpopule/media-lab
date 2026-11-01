# Audio-driven duet: reusable direction, not offline parity

The Music video catalog includes **Duet direction (planning)**. It carries a generic directing prompt and an authored schematic GIF. No private performance, identities, soundtrack, model weights or host settings ship with this preset. The machine-readable recipe is `config/workflows/duet-music-video.json`. It is deliberately **planning-only** and does not install engines, submit jobs, replace the original audio, or authorize a cloud fallback.

## Direction learned from a reviewed full-song candidate

Keep a shared location as the anchor for hooks and rapid handoffs. Depart to another location for a sustained solo, use occasional non-singing community or scenery coverage, then return. Use single-view identity inputs with an ordinary resting expression; reserve broad smiles for brief reactions. Preserve wardrobe and age. Vary static alternate angles, slow dolly in/out and occasional faster moves instead of repeated lateral oscillation. Keep the uninterrupted original soundtrack. When the correct mouth performance has a constant observed delay, try bounded picture-only retiming before generating another take. Replace only failed footage, never restart accepted scenes by default.

Do not label coarse verse timestamps as verified singer assignments. Before trusting a local duet, qualify a solo, a shared-frame handoff, and overlapping voices on the exact local runtime and settings. Full decode and unchanged audio do not prove lip sync or likeness. Owner playback remains a creative gate. Public H3 and LTX setup entries remain blocked while independent installers are unavailable; this preset must not bypass that boundary or import private runtime code.

## CQ LTX-2.5 enhancer candidate

Source: https://huggingface.co/CQdesign/LTX-2.5-CQ-Video-and-Image-Enhancer-LoRAs/tree/main

Pinned revision: `8b284cb76b77bd2ad788e233be35da8ed7fe1598`.

Selected artifact: `ltx2.5-CQ-enhancer-lora-V2.safetensors`, 1,340,583,888 bytes; SHA-256 `bc0924477007509db63a5a1a6c51c83194e724e8db6f04bc1fc3ba8225a5b730`. The acquired artifact matches upstream's LFS digest. Safetensors header inspection found 3,744 F32 tensors. This is acquisition evidence, **not inference or enhancement evidence**. The upstream model card declares no license at this revision, so redistribution permission is unresolved and no weights are committed.

The upstream V2 workflow (`Video-and-Image-Enhancer-V2.json`) requires LTX-2.5, an appropriate text encoder, a convolutional video VAE, and ComfyUI/custom-node dependencies. It is a generative refinement path, not a conventional resize filter or an H3 adapter. The upstream notes require processing video at 30 fps and then restoring the input frame rate. H3's native frame rate should otherwise remain unchanged. Do not import or execute an unreviewed workflow or auto-install its custom nodes.

### Acceptance test before any full-video application

1. Keep the accepted master immutable. Extract a four-second, two-lead copy and separate its original audio.
2. Inspect the workflow, verify local node/model availability and licenses, and obtain the required GPU reservation without interrupting another owner. Start no public listener or hosted fallback.
3. Make one conservative enhancer A/B at the same intended output dimensions. Preserve duration and reattach the original audio. Record exact settings, node/runtime revisions, hashes and runtime cost.
4. Compare the actual moving outputs at equal display scale. Check face shape, lips, teeth, wardrobe, flicker, handoff timing, repeated frames and motion cadence. An attractive still does not prove a better video.
5. Reject refinement if it changes likeness, mouth timing or accepted performance. Promote only after playback and technical gates pass; otherwise keep the original master and leave this candidate disabled.

No enhancer inference or visual improvement has been verified for this recipe yet. The initial test could not acquire a GPU because a protected paired language-model workload owned the available media hardware. Hardware availability is not permission to clear another workload's lease.

## CPU checks

```bash
uv run --extra dev --extra image pytest -q tests/test_duet_planning_template.py
uv run --extra dev ruff check tools/build_duet_planning_preview.py tests/test_duet_planning_template.py
uv run --extra dev python tools/identity_guard.py
```

Regenerate the schematic with `uv run --extra dev python tools/build_duet_planning_preview.py`. It is a planning diagram, not a sample of generated or enhanced performance.
