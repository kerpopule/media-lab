#!/usr/bin/env bash
# Start the on-demand warm MiniMax H3 engine container (60-min keep-warm).
#
# H3 ships two checkpoints and they do DIFFERENT jobs:
#   fl2va  — first/last-frame to video+audio. Conditions on a start FRAME.
#   ref2va — reference-to-video+audio: "actor cloning". Takes reference PICTURES
#            of a person and preserves their identity through the shot. This is
#            the one that holds a likeness.
# Pass H3_VARIANT=ref2va (default) or fl2va. Both weights are already on the box.
set -Eeuo pipefail
# Default stays fl2va until the reference-passing path is proven end to end:
# booting ref2va without it would load the actor-cloning model and then give it
# no actors, which is worse than what we have.
VARIANT=${H3_VARIANT:-fl2va}
[[ "$VARIANT" == "fl2va" || "$VARIANT" == "ref2va" || "$VARIANT" == "fused_r1024" ]] || {
  echo "Unsupported H3 variant: $VARIANT" >&2; exit 2;
}
TURBO_PRESET=${H3_TURBO_PRESET:-}
NAME=media-lab-h3-engine
A=$HOME/.local/share/h3-authorized/models
S=$HOME/maestro-h3-native-proof/t_596ce2dd/assets/minimax_h3
C=$HOME/.local/share/maestro-r13-default-t_b05cae84/models
OUT=$HOME/media-lab-simple/pool/h3-out
mkdir -p "$OUT"
MODEL_DST=minimax_h3_${VARIANT}_pruned_int8_convrot.safetensors
MODEL_SRC=$A/diffusion_models/$MODEL_DST
if [[ "$VARIANT" == "fused_r1024" ]]; then
  MODEL_SRC=$HOME/.local/share/h3-fused-r1024/models/minimax_h3_pruned_ref_delta_fused_r1024_int8_convrot_fc2bf16.safetensors
  # Maestro's actor-capable model definition resolves the Ref2VA filename in
  # the container. The host filename stays explicit so the experimental weight
  # can never overwrite or masquerade as the promoted official checkpoint.
  MODEL_DST=minimax_h3_ref2va_pruned_int8_convrot.safetensors
  FUSED_SHA256=304e808416e714a91c348b7ba70ad5098d8b294783596d304c9eec602f1f609c
  FUSED_SIZE_BYTES=24832507786
  [[ -f "$MODEL_SRC" ]] || { echo "Missing fused r1024 H3 checkpoint" >&2; exit 2; }
  [[ "$(stat -c %s "$MODEL_SRC")" == "$FUSED_SIZE_BYTES" ]] || {
    echo "Fused r1024 H3 checkpoint size mismatch" >&2; exit 2;
  }
  printf '%s  %s\n' "$FUSED_SHA256" "$MODEL_SRC" | sha256sum -c - >/dev/null
fi
TURBO_ARGS=()
if [[ -n "$TURBO_PRESET" ]]; then
  [[ "$TURBO_PRESET" == "v4-6step" || "$TURBO_PRESET" == "v4-8step" ]] || {
    echo "Unsupported managed H3 Turbo preset: $TURBO_PRESET" >&2; exit 2;
  }
  H3_TURBO_FILE=$A/loras/minimax_h3_turbo_v4_step600_ema.safetensors
  H3_TURBO_SHA256=5f3a626cd72c93a8b9318d6760c510bc5092d2ab13aaba1f932c5bab07a416d3
  H3_TURBO_SIZE_BYTES=779849816
  if [[ "$VARIANT" == "fl2va" ]]; then
    H3_AFFINE_ARCH=fl2va
    H3_AFFINE_MAP_SHA256=a42778e02ab2708dc70e23837ec4d3061b44f938c940decbc7a5b91f2c27c59e
  else
    # Official Ref2VA and the lab-only fused Ref2VA checkpoint share the
    # actor-capable affine layout.
    H3_AFFINE_ARCH=ref2va
    H3_AFFINE_MAP_SHA256=7179899e59fce9c36038cd6c0c57edaced0032c769c436cef234b07bf809381f
  fi
  H3_AFFINE_MAP=$A/lora_affine_maps/${H3_AFFINE_ARCH}_rank8.sft
  H3_AFFINE_MAP_SIZE_BYTES=130072
  [[ -f "$H3_TURBO_FILE" ]] || { echo "Missing H3 Turbo adapter" >&2; exit 2; }
  [[ -f "$H3_AFFINE_MAP" ]] || { echo "Missing pinned H3 ${H3_AFFINE_ARCH} affine map" >&2; exit 2; }
  [[ "$(stat -c %s "$H3_TURBO_FILE")" == "$H3_TURBO_SIZE_BYTES" ]] || {
    echo "H3 Turbo adapter size mismatch" >&2; exit 2;
  }
  [[ "$(stat -c %s "$H3_AFFINE_MAP")" == "$H3_AFFINE_MAP_SIZE_BYTES" ]] || {
    echo "H3 ${H3_AFFINE_ARCH} affine map size mismatch" >&2; exit 2;
  }
  printf '%s  %s\n' "$H3_TURBO_SHA256" "$H3_TURBO_FILE" | sha256sum -c - >/dev/null
  printf '%s  %s\n' "$H3_AFFINE_MAP_SHA256" "$H3_AFFINE_MAP" | sha256sum -c - >/dev/null
  TURBO_ARGS+=(--env "H3_TURBO_PRESET=$TURBO_PRESET")
  TURBO_ARGS+=(--mount "type=bind,src=$H3_TURBO_FILE,dst=/models/loras/minimax_h3_turbo_v4_step600_ema.safetensors,readonly")
  TURBO_ARGS+=(--mount "type=bind,src=$H3_AFFINE_MAP,dst=/models/minimax_h3/lora_affine_maps/${H3_AFFINE_ARCH}_rank8.sft,readonly")
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart no --gpus all --ipc host --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 --read-only \
  -p 127.0.0.1:8291:8291 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=16g \
  --env HOME=/tmp --env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env ENGINE=h3 --env PORT=8291 --env H3_VARIANT="$VARIANT" \
  "${TURBO_ARGS[@]}" \
  --mount "type=bind,src=$MODEL_SRC,dst=/models/transformer/$MODEL_DST,readonly" \
  --mount "type=bind,src=$A/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors,dst=/models/qwen/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors,readonly" \
  --mount "type=bind,src=$A/vae,dst=/models/minimax_h3/vae,readonly" \
  --mount "type=bind,src=$S/processor,dst=/models/minimax_h3/processor,readonly" \
  --mount "type=bind,src=$S/text_encoder,dst=/models/minimax_h3/text_encoder,readonly" \
  --mount "type=bind,src=$HOME/media-lab-simple/runner/engine_server.py,dst=/work/server.py,readonly" \
  --mount "type=bind,src=$HOME/media-lab-simple/runner/h3_fused_dtype.py,dst=/work/h3_fused_dtype.py,readonly" \
  --mount "type=bind,src=$OUT,dst=/work/out" \
  --entrypoint python3 maestro-v1.8.0-h3-r13:t_b05cae84 /work/server.py >/dev/null
echo STARTED
