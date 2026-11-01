# Image: Qwen-Image-2.1

Media Lab's only image model. One opt-in host (`qwen-image-21-gpu`) serves one pinned pack on the media Spark:
the runtime interpreter, every weight file, the shared inference lock and the exact revision are all named by an
operator configuration and re-verified before anything is advertised. There is no download, no substitution and no
provider fallback.

License: the weights are **Qwen Research License** weights. Internal evaluation is permitted; commercial output
needs a separate Qwen license (`config/models.example.toml`, entry `qwen-image-21`). Because of that the host does
not start the pack, even when configured, until the operator opts in with `MEDIA_LAB_PERSONAL_ENGINES=qwen-image-21`
in `config/local.env` ([ENGINE-LICENCES.md](ENGINE-LICENCES.md)).

## Operations

Both operations go through the same queue, the same warm resident renderer and the same verified publication.
A job's `settings` shape is exact: an unknown key is a 409, not a silent extra.

### text-to-image

```json
{"engineId": "qwen-image-21-gpu", "revision": "<host revision>", "kind": "image",
 "prompt": "a sleeping cat", 
 "settings": {"operation": "text-to-image", "size": "1024*1024", "steps": 40, "seed": 7}}
```

`size` is one of `1024*1024`, `1280*768`, `768*1280`; `steps` is 4 to 50 (the published recipe is 40); `seed` is a
32-bit unsigned integer; `prompt` is at most 600 characters. Sampling is unguided (`true_cfg_scale = 1.0`) with the
prefix KV cache on, which is the reference DGX Spark configuration.

### image-edit

```json
{"engineId": "qwen-image-21-gpu", "revision": "<host revision>", "kind": "image",
 "prompt": "Recolour the jacket to the bright red of the reference swatch.",
 "settings": {"operation": "image-edit", "size": "1024*1024", "steps": 40, "seed": 1234,
              "sourceId": "<32-hex>", "sourceSha256": "<64-hex>",
              "references": [{"inputId": "<32-hex>", "inputSha256": "<64-hex>"}],
              "mask": null, "transparent": true}}
```

- `sourceId`/`sourceSha256` is the image being edited. It must already be **this device's own accepted Library
  snapshot** (`POST /api/studio/inputs/library`), named by id *and* digest, exactly as background removal requires.
- `references` is up to **ten** further accepted snapshots, each checked the same way. Zero is valid: the source
  alone conditions the edit.
- `mask` is `null` or one more accepted snapshot (see below).
- `transparent` asks for an RGBA result.

The pipeline conditions on one *set* of images: the source plus every reference is handed to the text encoder as
vision context and to the VAE as prepended latent tokens. Condition images are resized to the requested output size.

## Masks are a paste-back, not inpainting

`QwenImage21Pipeline.__call__` has no `mask_image` argument, so a mask is honoured *after* sampling, deterministically:

```
output = sampled pixels where the mask is white, source pixels where the mask is black
```

Soft mask edges blend proportionally. A mask is read as its own alpha channel when that channel varies, otherwise as
its luminance, so both a grayscale PNG mask and an alpha PNG mask work. This is a compositing guarantee: the model
never sees the mask, so prompt changes that would have been suppressed inside the mask still influence the whole
sample. The receipt records `"mask": true` so a result is never mistaken for model-level inpainting.

## Transparency is native

The checkpoint's VAE carries four channels, so the sampler emits RGB **and** an alpha channel. With
`transparent: true` the published PNG is RGBA and the decode gate fails closed if the result has no alpha channel;
with `transparent: false` the result is flattened to RGB.

## Verified publication

The renderer writes `output.png` plus `receipt.json` into a private directory that only the owned worker sees, and
the worker re-decodes the PNG and re-computes the receipt before an atomic rename publishes the job:

- text-to-image receipt: `{version: 1, revision, inputSha256, image}`
- image-edit receipt: `{version: 2, revision, inputSha256, operation, references, mask, transparent, image}`

`image` is the independent PNG decode: `{mimeType, bytes, sha256, width, height, mode}`.

Condition images never travel as caller paths. The renderer request carries counts and flags only, and the worker
stages the bytes under fixed names (`source.png`, `reference-<n>.png`, `mask.png`) behind the queue's own
owner-checked input reader, re-hashing every staged file against the digest the payload declared.

## Tests

`tests/test_image_edit.py` covers the request contract, the exact settings shapes, owner-checked staging, receipt v2,
the paste-back blend, transparency, and the fail-closed paths (opaque result for a transparent request, tampered
staging digest, foreign reference). `tests/test_image_host.py` covers the pack, the PNG gate and server routing.
