# Experimental H3 scene keyframes (API only)

`h3_scene_keyframes` is an **optional, strict boolean, default `false`** for
H3 Real / Long. It adds sparse time-indexed image conditioning from the same
supplied starting scene at the video's midpoint and last frame. The existing
opening-frame guide must also be explicitly enabled.

Example request fragment (all media must already exist and be authorized):

```json
{
  "prompt": "A continuous shared scene with both performers visible",
  "model": "h3-real",
  "duration": "8",
  "orientation": "landscape",
  "source": "/media/scene.png",
  "h3_anchor_first_frame": true,
  "h3_scene_keyframes": true,
  "audio_references": [
    {"source": "/media/original-excerpt.wav", "start_sec": 0.0, "role": "original musical passage"}
  ],
  "h3_preserve_audio": true
}
```

For a 192-frame take, the installed `MiniMaxH3AddGuide` nodes resolve the image
anchors at frames 0, 96 and 191. The last index is expressed as `-1`, using the
native node's documented negative-index contract. Both denoising passes encode
the scene against their own spatial latent grid. The midpoint/end nodes chain
after the existing opening image/audio guide and carry **images only**: the
original full-timeline audio is not restarted at either later anchor.

- No supplied scene, wrong engine, absent explicit opening anchor, and loose
  non-boolean values fail closed before staging/GPU use.
- Disabling or omitting the option retains the existing graph exactly.
- This does not change checkpoints, adapters, sampler defaults, source-reference
  order, GPU leases, residency, audio output selection or the delivery master.
- This is temporal conditioning, **not a guaranteed pixel lock**. It can
  overconstrain pose or facial motion. A real decoded canary must establish
  composition, natural motion, identity and mouth-to-word performance before
  any production batch. CPU graph/tensor checks cannot establish those results.
- Existing model-use, likeness, audio and publication permissions still apply.

There is no UI toggle in this change. API clients can carry the opt-in through
the typed request and must not silently enable it. Native Storyboard refilm and
end-to-end export retention require separate runtime qualification; the CPU
checks described here do not establish that workflow.
