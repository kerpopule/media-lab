# Independent speech candidate

Development review only: no engine is installed, registered or enabled by this
record. The existing `runner/run_tts_speak.sh` invokes a Maestro image, and
`runner/tts_chatterbox_oneshot.py` imports its TTS pipeline. Those paths therefore
do not satisfy independent speech execution.

The [candidate record](chatterbox-candidate.json) pins a direct upstream
Chatterbox source revision and the original English model repository revision.
This is a compatibility candidate, not a recommendation that it is the best
speech model. Multilingual, Turbo and Nano are distinct variants and must not be
substituted under the same identity.

The selected three SafeTensor files and tokenizer total 3,191,859,618 bytes,
excluding runtime dependencies and working memory. Weight hashes currently come
from upstream LFS metadata; the tokenizer was downloaded and hashed. No weight
load, RAM estimate, speed or audio-quality claim is supported yet.

The upstream local loader reads the three SafeTensor files, optionally reads a
pickle-based voice preset, and loads the vocoder with non-strict state matching.
Its convenience loader fetches mutable defaults and may change MPS execution to
CPU. The dependency list includes an unpinned Perth Git branch. These behaviors
need an explicit offline integration and qualification rather than calling the
convenience API unchanged.

Next implementation requirements:

1. Check source/model compatibility first. The reviewed S3 tokenizer wrapper
   constructs `S3TokenizerV2`; compare actual state keys with the selected model
   before resolving an environment. Record expected missing buffers explicitly;
   do not accept arbitrary missing/unexpected state keys.
2. Resolve and pin every runtime artifact, including the external tokenizer and
   Perth constructor behavior. Retain upstream watermark behavior. Review the
   embedded third-party source notices as well as the top-level MIT declarations.
3. Keep `conds.pt` excluded until a separate format review or a verified portable
   conversion exists. Alternatively, use an explicitly authorized reference
   recording. No voice recording is selected or authorized by this document.
4. Prepare a local-only loader with no automatic downloads, variant changes or
   device fallback. Qualify an isolated CPU tracer before touching shared GPU
   services, then measure each supported platform separately.
5. Connect qualified speech to owned durable jobs, decoded WAV results and Library
   reuse. Test disconnect/reconnect, cancellation, retry and host shutdown before
   exposing Generate speech in Create.

Sources inspected on 2026-09-05:

- [Pinned upstream source](https://github.com/resemble-ai/chatterbox/tree/5de7a54aa4e5e2baadb0182dde554908b48b85c2)
- [Pinned model files](https://huggingface.co/ResembleAI/chatterbox/tree/5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18)
- [Kokoro upstream](https://github.com/hexgrad/kokoro) was also inspected; its
  documented pipeline includes separate phonemizer/runtime dependencies. It has
  not been installed, qualified or ruled out by this Chatterbox review.

## Bounded tensor-header inspection

[Layout evidence](chatterbox-layout-review.json) records HTTP range reads of the
three pinned SafeTensor headers. Each request required a partial response
starting at byte zero and read at most 1 MiB. The headers describe 2,489 vocoder,
292 text-model and 16 voice-encoder tensors. No tensor payload was loaded and
these range reads do not verify full artifact hashes.

The vocoder contains tokenizer convolution shape `[1280,128,3]`, six encoder
blocks, and projection shape `[8,1280]`. These match the inspected external
S3TokenizerV2 defaults in the hash-verified s3tokenizer 0.3.0 wheel. Its constructor
builds the encoder/quantizer without calling a pretrained loader. The wheel was
inspected as a ZIP and not installed or imported. This removes the initial
basic-dimension concern, not the need to compare every state key, review runtime
imports and dependencies, or execute a full load and audio tracer.

## Dependency resolution and watermark checkpoint

The [Linux ARM64 dependency plan](speech-runtime/resolution.json) resolves 107
packages for Python 3.12 with wheel-only metadata and hashes, preserving the
upstream Torch/Torchaudio 2.6.0 constraints. It contains dependencies of the two
source projects, not built Chatterbox or Perth packages. Nothing was installed.
Target installation, ABI and source-build checks remain necessary.

Perth is pinned for review to ff1c8ac55a976971245cdd53c18d6131ca00d993. Its default
watermarker loads a package-local 37,429,684-byte pickle checkpoint, in addition
to the Chatterbox model payload already listed. The checkpoint was subsequently converted as recorded below. Integration must
retain watermark behavior; disabling the watermarker is not a substitute for
packaging its dependencies.

## Restricted watermark conversion

The exact Git blob was downloaded and its object hash verified, followed by
SHA-256 a15bce457ebc53ce5e6c9c3f11df78cf7ee2bf9cdab0a798902135b4c4027670.
Static pickle disassembly found no dynamic globals. The development converter
accepts only that file and its expected model/step envelope.

A Spark CPU process with configured memory/time/CPU limits and network syscall
filtering completed explicit weights-only loading with an empty added-globals
allowlist. It checked finite dense float32 tensors and compared every tensor
after SafeTensor encoding/decoding. [Conversion evidence](speech-runtime/perth-conversion.json)
records the 37,413,196-byte output and its hash. Repeat-to-existing-output and
invalid-source checks refused the operation and preserved the output.

This proves tensor preservation, not watermark detection or speech quality.
An offline Perth loader for the converted file, matching configuration, real
watermark encode/detect checks and independent speech inference remain required.
The converter is a development tool, not an enabled installer path.

## Explicit CPU loader

`media_lab_core.perth_cpu.load_watermarker` validates the converted artifact bytes
before importing runtime modules, decodes SafeTensors directly, constructs the
model using the exact pinned configuration, requires strict state matching,
and enters CPU evaluation mode. It passes the model directly to the upstream
watermarker, bypassing the checkpoint manager and its pickle preset lookup.
It assumes a separately pinned runtime; it does not install or qualify that runtime.

Four focused tests cover malformed paths/size, same-size corruption, the strict
CPU construction contract and state-mismatch rejection. Runtime classes are
substituted in those contract tests: real architecture loading and watermark
encode/detect acceptance remain unfinished.

## Isolated Spark watermark smoke test

The dependency lock installed successfully into a new Python 3.12.3 Linux
aarch64 environment. `pip check` found no broken requirements. Pinned Perth
source was extracted without the pretrained directory and imported directly;
source wheels and redistribution closure remain unfinished. Pip 24.0 reported
a deprecated OmegaConf 2.0.6 dependency specifier, which needs resolution before
a portable installer is qualified.

The actual strict loader accepted the converted state under Torch 2.6.0+cpu.
A three-second 32 kHz synthetic waveform passed watermark encoding and detection:
96,000 finite samples, detector mean 0.299 before and 0.688 after, 2.513 seconds
for load and operations. [Tracer evidence](speech-runtime/watermark-tracer.json)
records the environment and scope. It does not establish speech quality or
watermark robustness on real recordings.

The first transient process terminated with SIGSYS because the network filter
used its default termination action. The second returned EPERM for blocked
syscalls and passed an explicit network-denial probe. Both are terminal; the
existing Media Lab services remained active. No GPU inference or public endpoint
was used.

## Installed speech package and complete state-shape comparison

A source-only import failed because Chatterbox requires package distribution
metadata. Built the pinned source into a wheel offline after replacing its moving
Perth dependency reference with the reviewed commit. Installed that exact wheel
by hash with dependency resolution disabled, then successfully imported the real
English TTS package under network syscall denial. This is an isolated development
build, not a reproducible source-package release.

[Shape comparison](speech-runtime/chatterbox-shapes.json) constructs the three
architectures on Torch's meta device. All 292 text-model and 16 voice-encoder
keys and shapes match. The vocoder has 2,489 matching checkpoint tensors and one
additional expected buffer, `tokenizer.window`, generated by the tokenizer's
400-sample Hann-window constructor. There are no unexpected keys or differing
shapes. The offline loader must handle only that known buffer explicitly rather
than allowing arbitrary missing keys. No speech weights were loaded in this check.

## Real offline speech weight load

All selected model files were downloaded to isolated Spark storage and their full
bytes and SHA-256 verified. `chatterbox_cpu.load_cpu` validates those bytes, checks
finite tensors, requires strict state keys, reconstructs only the known Hann
window, and supplies the converted watermark model directly. It selects neither
a voice preset nor another device.

The first load found nonpersistent positional buffers left on the meta device.
The corrected loader initializes architecture buffers on CPU before assigning
verified weights. The [real load receipt](speech-runtime/chatterbox-load.json)
records success in 13.136 seconds with network denial, a 24 kHz output contract,
and all parameters on CPU. Six focused byte/loader tests also pass. This proves
loading, not speech generation, quality or a supported installation.

## First independent speech tracer

The pinned upstream `conds.pt` preset was statically inspected, then converted
in a restricted CPU process. Its exact nested fields, seven tensors, shapes,
dtypes and empty fields were checked; SafeTensor roundtrip preserved every value.
The original pickle preset remains excluded from runtime loading.

An explicit default-voice selection reconstructed the reviewed conditioning
fields from the converted file. The real offline model generated the prompt
“Welcome to your creative studio.” under seed 7 and a two-core CPU limit. The
[tracer receipt](speech-runtime/speech-tracer.json) records a decoded 24 kHz mono
WAV of 37,440 samples (1.56 seconds), peak 0.712, and elapsed 85.38 seconds including
loading. Networking was denied; no Maestro runtime or personal voice input was
used. Seven focused loader/preset tests passed.

This proves that generation executes and produces decodable audio. Spoken words,
voice quality, robustness and creative approval are not verified. CPU throughput
is not yet suitable to advertise as fast. The implementation remains experimental
and is not connected to the public Create interface or installer catalog.

## Installed Perth package

Perth now runs from a built wheel rather than a loose source folder in the
isolated Spark environment. The build used the pinned uv_build 0.12.7 ARM64
artifact, with networking blocked. The first build could not find `uv-build` on
PATH; an explicit isolated-environment PATH fixed that error.

The [wheel receipt](speech-runtime/perth-wheel.json) records 29 archive entries
and no pretrained directory or pickle checkpoints. It therefore requires the
explicit converted-model loader; upstream default checkpoint discovery is not
a supported entry point. Hash-required installation succeeded and `pip check`
found no broken requirements with Chatterbox installed. The installed-package
watermark tracer reproduced the prior detector scores exactly, with finite
audio and confirmed network denial. OmegaConf's deprecated metadata and complete
redistribution/install lifecycle work remain unresolved.

## Current pip metadata compatibility

Replaced OmegaConf 2.0.6 with 2.3.1 and added its required ANTLR 4.9.3 wheel.
All other resolved dependency versions remain unchanged. The ANTLR wheel matches
the previously reviewed reproducible build recipe and hash; the resolver requires
that wheel directory via `--find-links`. The dependency lock now contains 108
packages.

The isolated environment installed these exact hashes and pip 26.2.1. Dependency
checks and a hash-required offline dry run against the installed packages passed
without the invalid-metadata warning. Actual network-denied CPU model loading
also passed in 12.926 seconds. [Evidence](speech-runtime/installer-metadata-check.json)
distinguishes this compatibility check from a fresh-machine installer test. No
new speech clip or quality qualification was produced by this stage.

## Durable speech queue adapter

`speech_jobs.run_next` now uses the shared owned CPU job lifecycle with explicit
text preparation, an exact caller-supplied revision, the reviewed default voice
and an explicit seed. The CPU claim path accepts audio while retaining engine
and owner-scoped request isolation. Existing image snapshot hashes remain
mandatory on the image-input path.

Twenty-four queue/store tests pass, including cancellation winning over a result,
expired-claim recovery waiting for the process lock, reopening the database with
text/voice intact, wrong-revision rejection, and existing background jobs. Speech
queue tests use a substituted executor: a bounded real speech subprocess and
verified audio artifact publication still need integration. No capability is
automatically registered or advertised by this adapter.

## Independent audio artifact gate

`speech_artifact.inspect_wav` independently parses and decodes the prospective
WAV with the Python standard library. It requires a bounded RIFF container,
complete chunks, exactly one format/audio chunk, 24 kHz mono PCM16, a duration
between 0.1 and 120 seconds, and a non-silent signal. It returns the hash, bytes,
frames, duration, peak and RMS. These limits are output validation limits, not
a claim that CPU generation supports 120 seconds within its execution budget.

Nine focused tests cover accepted signal, format and duration rejection, silence,
truncation, oversized input and duplicate audio chunks. The existing real Spark
clip also passes: its hash matches the generation receipt and independently
decoded peak/RMS are 23,336/3,677.57 in PCM16 units. This gate is ready for worker
integration but is not yet wired to publication; it does not verify spoken words
or replace creative review.

## Real queued worker and verified publication

`speech_render` now runs inside the owned subprocess with a temporary home and
explicit model/preset paths. `speech_worker` stages output, independently checks
the WAV and exact input/revision receipt, checks cancellation before promotion,
and atomically renames the accepted directory. Recovery verifies the existing
result instead of generating again. A cancelled queue result is never served as
a successful job, even if cancellation races after disk promotion.

Thirty-six focused worker, queue, artifact and background tests pass. A real
Spark queued job completed in 83.87 seconds through this executor and preserved
its result after reopening SQLite. Its 74,924-byte WAV hash matches the earlier
tracer exactly. [Receipt](speech-runtime/queue-tracer.json) records that scope.
The outer development unit denied networking; the worker itself sets offline
library flags but is not a standalone network sandbox. Public admission, host
qualification, output retrieval through the app and creative review remain.
