# Studio UX — the Pika/Luma/Krea review

*2026-08-23. The question: what makes this the go-to for a 9-year-old making a
music video AND a pro cutting the world's best ad — on hardware you own.*

## The DNA those three products share

1. **The feed is the product.** Pika's home is finished work you can remix in
   one tap. Luma opens on boards full of ideas. Krea shows you the style before
   the tool. Inspiration IS navigation — nobody starts from a blank prompt box.
2. **Verbs, not parameters.** Pikaffects are the masterclass: "Squish it.
   Melt it. Cake-ify it." A transformation with a fun name is a product; a
   parameter form is homework.
3. **The gratification ladder.** Cheap fast draft → pick the winner → upgrade
   it. Krea's realtime canvas is the extreme; Pika's fast previews the norm.
   Waiting 8 minutes to find out a prompt was wrong is the #1 novice killer.
4. **Iterate by talking.** Luma's creative agent: "warmer, slower, more dusk."
   Nobody re-fills a form to change one thing.
5. **Zero jargon above the fold.** No user ever sees "LTX 2.5 distilled 8k+1
   frame contract." They see **Fast** and **Cinematic**.
6. **Steal-this-prompt.** Every showcase item exposes its full prompt. That's
   how novices become intermediates without a manual.

## Where the Studio already wins

Own hardware (no credits anxiety — a kid can burn 50 drafts), Sparky (a real
director, not a form), characters with real identity pipelines (nobody else has
your-face-that-actually-works), 523 templates + 1,475 known characters + 200
recipes, show presets, PWA push. The bones are genuinely ahead. The skin between
a novice and those bones is what's missing.

## The gaps, ranked by leverage

### P0 — the novice loop (do first)
1. **Rename the engines.** `⚡ Fast draft` / `🎬 Cinematic` / `🎛 Recipes`.
   Keep real names in a tooltip for pros. Jargon is a bouncer at the door.
2. **Remix is the hero.** On every gallery tile, Remix becomes the big
   affordance (it exists — promote it). Add a **Showcase shelf** at the top of
   the feed for fresh visitors: 8–12 curated best takes, each opening with its
   full prompt visible — steal-this-prompt as onboarding.
3. **Effect verbs.** One row of one-tap transformations on any image/video:
   ✨ Bring to life · 💥 Blow it up · 🫠 Melt it · 🎂 Cake-ify · 🕺 Make it
   dance · 🌊 Flood it · 🚀 Launch it. Implementation is cheap: curated prompt
   templates over the existing i2v/v2v rails. Naming is the product. Kids live
   here; pros use them as animatics.
4. **Talk-to-iterate.** A `🎬 Direct changes` button on every finished take
   opens Sparky with that job's context; operator gains one verb —
   `iterate(job_id, change)` — that requeues with the tweak. "Same but at
   sunset" should be one sentence, not a re-form.
5. **Platform shapes.** Wide/Tall/Square become **YouTube · TikTok/Reels ·
   Square** (same values, real names), with a text-safe-zone overlay toggle in
   the storyboard for ad work.

### P1 — the ladder and the first minute
6. **Variations grid → upgrade.** "Roll camera" in draft mode returns 4 seeds
   as a grid; tap one → **Upgrade** re-renders at Cinematic/enhanced (the
   engine_generate + enhance suite already exist; this is orchestration + UI).
   Bookmark accelerants: Kijai turbo LoRA + SageAttention qualify as the
   draft-mode speedup (never the final path).
7. **First-run magic.** New device → 3-step overlay: pick a vibe card → one
   line (or a suggestion chip) → Roll. Target: first video in under 60 seconds
   with zero reading.
8. **Progress you can feel.** Percent + a mid-render frame peek in the queue
   drawer; calibrated ETAs; a small celebration on done. Waiting with proof of
   life beats waiting in the dark.
9. **Brand kit (pro lane opens).** Per-project logo, palette, font, tone.
   Storyboard/ads inherit it silently. (The instructor-slide-studio pattern,
   generalized.)

### P2 — the pro floor and the moat
10. **Batch matrix.** N seeds × M styles overnight; contact-sheet review;
    "the studio worked while you slept" is a Spark-only flex.
11. **Exact-spec export.** Codec/bitrate/loudness presets per destination
    (broadcast, Meta, TikTok, DOOH). Pros need the last mile.
12. **Camera language.** The 42-movement typed planner (already spec'd in the
    VideoLab candidate) surfaces in storyboard's Advanced fold.
13. **Kids mode.** One toggle: prompt + vibes + GO, big type, curated styles.
    The family-Spark pitch writes itself.
14. **Share to approve.** A storyboard/take gets a read-only share link with
    comment pins — the ad-agency loop without leaving the Studio.
15. **Trending effects pipeline.** Model-watch feeds new community effects
    (DiffSynth audio-reactive, 2D→photoreal character converts, the Banodoco
    76-style survey) into the effect-verb row monthly. The verb row never
    goes stale — that's Pika's growth engine, ours runs on RSS + Sparky.

## The one-sentence test for every future control

*Would a 9-year-old know what happens when they tap it, and would a pro trust
what happened?* If either answer is no, it goes behind a fold or gets a
better name.
