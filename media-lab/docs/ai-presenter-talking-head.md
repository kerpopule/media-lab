# Local AI presenter / talking head

The Character section exposes the existing local voice-conditioned video path as an explicit AI Presenter workflow.

## Local path

1. Pick or create a saved character.
2. Attach an authorized cloned or preset voice.
3. Enter one or two sentences and an optional scene.
4. Choose LTX Fast or H3 Higher Fidelity.
5. Submit through the existing Media Lab job queue.

The voice is generated first and remains the timing master. The video job uses the saved character reference and selected voice. Both engines run on the private DGX Spark and obey `/run/user/1000/spark-gpu.lock`; the UI does not start a second worker or bypass an active render.

## Source inspiration

The workflow shape was evaluated from `cclank/lanshu-create-ai-presenter-video` at revision `04f6bceab888ad923e192fb02542eda06d1fdda8` (MIT). That repository is a provider-neutral production skill, not an offline model or runtime. No code, installer, provider integration, or remote-upload path was imported. Media Lab reuses its already-installed local character, voice, LTX, H3, queue, and QA capabilities.

## Boundaries

- This first surface creates bounded presenter takes, not a complete long-form edited program.
- Real-person images and cloned voices still require authorization.
- Public/customer use and actor voice cloning remain separately gated.
- Rendering, captioning, multi-shot composition, and delivery QA continue through their existing Media Lab workflows.