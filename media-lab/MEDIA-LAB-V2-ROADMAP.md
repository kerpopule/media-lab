# Media Lab v2 — the owner's spec (2026-08-15), thought through

North star: as simple as Google Omni / Grok Image Edit 2 / Ideogram Canvas.
Absolute beginners have fun in seconds. Mobile-first, camera/mic-native.
Advanced mode (full Maestro GUI :7862) stays one tap away, never in the way.

Live today (v1, :7863): Video tab — prompt + style chips + length + shape →
LTX-2.5/Maestro defaults (the proven winner), Retro Cam chip = H3.
Single-flight GPU queue with reservation discipline. Gallery. Mobile-tested.

## Tab structure (bottom nav, 5 icons, no submenus)

🎬 Video · 🎵 Music · 🖼 Images · 🧑‍🎤 Characters · 🎞 Storyboard

### 🎵 Music (build next — engine already installed)
- One text box: "What should your song feel like?" + optional lyrics box
  (collapsed by default behind "✍️ Add lyrics").
- Backend: user text → local Qwen (llama-server :8003, already always-on)
  rewrites it into the three-section Music 3 caption format (Global
  Metadata / Vocal Details / Arrangement — the format the official Music
  Caption Rewriter skill defines). User never sees the machinery unless
  they tap "show enhanced prompt".
- Length chips: 1 min / 2 min / 3 min. Seed hidden ("🎲 Another take" re-rolls).
- Engine: MiniMax Music 3 in ~/runtime/music3-iso (models + graph proven).
- Output: audio player card + waveform + Save. Feeds the Video tab
  ("🎬 Make a music video from this song" → LTX audio-conditioned pipeline).

### 🖼 Images (generate + edit playground)
- Generate: prompt → image. Engine: qwen-image (already on Spark in
  comfy-ltx25: qwen_image_edit_2511_int8_convrot + Lightning 4-step LoRA =
  seconds-fast). Style chips shared with Video tab.
- Edit: tap any image → mark area / type change ("make it night", "add a
  hat") → qwen-image-edit. This IS the Grok-Image-Edit-2-style loop.
- Every image has "🎬 Animate this" (LTX I2V) and "→ Storyboard" buttons.

### 🧑‍🎤 Characters (webcam → reference sheet → consistent casting)
- Flow: "Make yourself a character" → webcam opens (getUserMedia, works on
  phone) → on-screen coach: "look straight… slowly turn left… right… chin
  up… smile" → auto-captures 6-8 angle stills (face landmarks via
  MediaPipe-in-browser to know when each pose is hit).
- Backend: stills → qwen-image-edit restyles the set in the user's prompted
  style ("as a 1940s film star", "as a Pixar character") → composited into
  a character reference sheet (grid PNG) + saved as a named Character.
- Characters become chips in Video/Images/Storyboard ("cast: Ava-toon").
  LTX multi-subject reference conditioning consumes the sheet.
- Voice: "Give them your voice" → the app shows 3 short paragraphs to read
  aloud (mic capture, ~45 s total) → local voice-clone TTS. Engine already
  in Maestro image: index_tts2 / chatterbox TTS families — qualify which
  clones best from ~45 s and wire that one. Cloned voice attaches to the
  Character and drives dialogue in videos (H3/LTX audio conditioning).
  Consent gate: only your own voice; explicit on-screen consent text.
- HTTPS REQUIRED for getUserMedia on mobile — serve behind Tailscale
  HTTPS (tailscale serve) or a self-signed cert profile.

### 🎞 Storyboard
- Horizontal strip of scene cards. Each card: thumbnail + one-line beat.
- Start from: blank / "write my story" (Qwen expands a premise into beats)
  / existing gallery items dragged in.
- Each card generates its clip with the shared style + cast; the strip
  assembles into one MP4 (concat + crossfade, ffmpeg) with the Music-tab
  track as soundtrack. Export = the full short.
- This is the "arbitrary-length assembly" the product addendum asked for,
  but drag-simple.

## Design rules (all tabs)
- One primary action per screen. Everything else is a chip or a card.
- No parameter names anywhere (no "cfg", "steps", "seed", "denoise").
- Progress = friendly stages + honest ETA; queue position when busy.
- Every result card: Save / Share / Remix / Send-to-other-tab.
- Thumb-reachable bottom nav; large touch targets; dark, glassy, gradient
  accent (already established in v1).
- "In person" mode: camera and mic flows must work one-handed on a phone.

## Engineering notes
- All GPU work goes through the single-flight queue + reservation-restore
  discipline (proven tonight across ~10 transactions).
- Engines all local on the Spark: LTX-2.5 + H3 (Maestro image), Music 3
  (music3-iso), qwen-image (comfy-ltx25 models), Qwen 27B text (:8003),
  TTS (Maestro image). No cloud inference.
- Advanced mode start/stop moves the same lock; the app shows "Advanced
  mode is using the GPU" rather than failing silently.
- Build order: Music tab → Images tab → Characters (capture UI, then
  restyle, then voice) → Storyboard. Each ships when it's fun, not before.
