# Media Lab Image Template Collection — source notice

This local collection includes prompt records and example preview images from:

- **Project:** [freestylefly/awesome-gpt-image-2](https://github.com/freestylefly/awesome-gpt-image-2)
- **Reviewed commit:** `3a9c63baa03e6bbe2f28c89a2654cf9845466646`
- **Project code/data license:** MIT; see [`LICENSE`](./LICENSE).

The Media Lab browser and chat integration are a custom implementation, not a raw port of the source application.

## Third-party-content warning

The source project says it organizes publicly accessible community prompts and example images for learning, research, methodology study, and automated model testing. It claims no ownership of third-party original content. Source labels and URLs are retained on each case where supplied.

**The MIT license for the repository does not by itself guarantee commercial rights to every third-party prompt or image. Treat these examples as research/learning references. Before commercial use, review the original source and obtain permission from the applicable original author or rightsholder.**

The source project specifically credits public content from [YouMind](https://youmind.com/) and [OpenNana](https://opennana.com/) and asks rightsholders to contact that project when a listing should be corrected or removed.

Cases that showed or named real people, or featured a real company's logo, product or trademark, were taken out on 2026-09-26 (rule 6 and the second table below). Nothing here grants a right to use a person's likeness or a trademark.

## Provenance rules

This folder ships in a public repository, so every case must be one we may redistribute.

1. **Traceable source.** Every case keeps its original author label and a link to where it was published (`sourceLabel`, plus `githubUrl` for upstream cases or `sourceUrl` for local additions).
2. **Upstream cases** come only from the reviewed commit above, unchanged apart from translation.
3. **Local additions** (`"provenance": "media-lab-local"`) need a `rightsNote` that says the author allows public redistribution. A record marked private, research-only, "no redistribution", "no publication" or "non-commercial" does not belong here; keep it in a studio's own gitignored overlay (`config/local/`, see `docs/LOCAL-OVERLAY.md`).
4. **No franchise characters or franchise branding.** A case may not show, name, cosplay or mock up a character, title screen, logo or game interface from a film, TV, anime, manga, comic or game franchise, even when the prompt only has a placeholder. Style words ("in an anime style", "Pixar-like lighting") are fine; a named character or franchise is not.
5. **Removal requests win.** If an author or rightsholder asks, the case comes out in the next release, no questions asked.
6. **No real people and no real brands.** A case may not show or name a real person, public figures included, or make a real company's logo, product or trademark its subject, and it may not mock up a real organisation's document, stamp or account. Historical figures in a plainly historical scene are fine. Generic layouts (a live stream, a social feed, a video player, a product page) with made-up people and content are fine, and so are placeholders such as `[brand name]`.

`tests/test_image_template_static_contract.py` enforces rules 1 to 4 and 6 for the cases that ship. Rule 6 was checked by a text scan and a visual review of every preview on 2026-09-26. That review is a best effort, not a rights clearance.

## Removed for rights reasons

These case ids from the reviewed commit (and one local addition) were taken out and must not be re-added:

| Case | Why |
|---|---|
| 24 | Cosplay of a Genshin Impact character (miHoYo) |
| 25 | Minecraft skin sheet (Mojang/Microsoft game art) |
| 43 | Game of Thrones characters (HBO) |
| 52 | Labelled as a Vagabond manga character (Takehiko Inoue) |
| 84 | Demon Slayer character map (Koyoharu Gotouge / Shueisha) |
| 85 | Naruto character map (Masashi Kishimoto / Shueisha) |
| 86 | Bleach character map (Tite Kubo / Shueisha) |
| 87 | Dragon Ball character map (Akira Toriyama / Shueisha) |
| 91 | Minecraft title and game art (Mojang/Microsoft) |
| 112 | Saint Seiya Gold Saints (Masami Kurumada / Toei) |
| 125 | Nazuna Nanakusa, Call of the Night (Kotoyama / Shogakukan) |
| 143 | The Amazing World of Gumball house set (Cartoon Network) |
| 145 | Prompt names the Terminator T-800; the upstream record's prompt does not match its image |
| 146 | Mai Shiranui, The King of Fighters (SNK); prompt names the Terminator T-800 |
| 147 | Prompt names the Terminator T-800; the upstream record's prompt does not match its image |
| 148 | Terminator T-800 (Terminator franchise) |
| 161 | Grand Theft Auto VI screenshot mock-up (Rockstar Games) |
| 166 | Saint Seiya Gold Saints (Masami Kurumada / Toei) |
| 174 | Xenomorph from the Alien films (20th Century Studios) |
| 197 | League of Legends game screen (Riot Games) with real politicians |
| 207 | Black Myth game branding (Game Science) |
| 241 | Naruto character map (Masashi Kishimoto / Shueisha) |
| 250 | The Little Prince character co-branded with SpaceX |
| 251 | The Garden of Words film branding (Makoto Shinkai / CoMix Wave) |
| 282 | Cosplay of a Honkai: Star Rail character (miHoYo) |
| 287 | Mai Shiranui, The King of Fighters (SNK) |
| 299 | JoJo's Bizarre Adventure character (Hirohiko Araki / Shueisha) |
| 301 | Terminator T-800 (Terminator franchise) |
| 309 | Tom from Tom and Jerry (Warner Bros.) |
| 527 | Anime franchise character; local record marked private research, no publication or redistribution |

## Removed for likeness and trademark reasons (2026-09-26)

These case ids from the reviewed commit were taken out under rule 6 and must not be re-added. The first 48 were flagged by the franchise review above. The last 16 were found by a second text scan and a visual review of every preview. Where a person is not named here, the record or its preview shows or names a real one.

| Case | Why |
|---|---|
| 2 | Mock-up of a post from OpenAI's verified account, with its logo |
| 3 | A named professional footballer on a film poster |
| 4 | A named politician selling a named chili-sauce brand in a live stream |
| 10 | Named technology executives on a propaganda-style poster |
| 16 | FC Barcelona crest and named real players |
| 17 | Exploded view of a branded consumer headset |
| 21 | A named technology executive as the default live-stream host |
| 36 | A sportswear brand's logo on the subject's clothing |
| 48 | A named actress as the default live-stream host |
| 49 | A named actress as the default live-stream host |
| 83 | Real footballers and UEFA Champions League branding |
| 90 | Real studio and brand marks on a concept poster |
| 101 | The ChatGPT logo as the subject of a side-hustle thumbnail |
| 102 | A real player's match performance with competition branding |
| 103 | A named real pianist as the default performer |
| 104 | A named real YouTuber in a YouTube Premium live-stream mock-up |
| 107 | A company launch live stream with its logo and a real executive's likeness |
| 111 | A real person's likeness in a true-crime thumbnail |
| 114 | Real public figures in a satirical comic |
| 149 | A named technology executive wearing a company logo |
| 152 | A lookalike of a real public figure as the default subject |
| 154 | A named carmaker's model (Alpine A110 R) |
| 164 | A named politician in a live-stream mock-up |
| 177 | A named carmaker's model dashboard (Geely Galaxy M9) |
| 178 | Amazon product-listing branding |
| 181 | A real restaurant chain's logo on a menu poster |
| 201 | A realistic prescription bearing a real hospital's name and stamp |
| 227 | A named real streamer in a Bilibili live-stream mock-up |
| 233 | A soft-drink brand as the subject of a famous-painting parody |
| 239 | A named actress in a live-stream mock-up |
| 244 | Two named brands' co-branding campaign |
| 245 | A named technology executive's name carved into seals |
| 262 | A named company's product launch with its executive |
| 289 | Two named heads of state in a live-stream mock-up |
| 302 | Nine named real designers' portraits |
| 310 | A real snack brand's packaging |
| 322 | A real drinks brand's bottle |
| 323 | A sportswear brand's logo on the subject's clothing |
| 343 | A named fashion house's magazine cover |
| 345 | A real film star's likeness on a poster |
| 350 | A real footballer's likeness and career stats |
| 353 | A cosmetics brand's branded product report (YSL Beauty) |
| 359 | A real person's likeness on a promotional poster |
| 361 | A phone maker's branded device |
| 363 | A real company logo as the worked example |
| 365 | Real scientists' likenesses sold as collectible toys |
| 378 | A real public figure's photo as the worked example |
| 379 | A real brand's logo as the worked example |
| 385 | A named beer brand as the design brief |
| 388 | An advertisement for a real AI product (Claude) |
| 389 | A named supplement brand's campaign |
| 424 | A named candy brand's billboard |
| 427 | Fashion brand logos in a portrait collage |
| 444 | A real app's logo as the worked example |
| 449 | Named watchmakers' products |
| 454 | A named snack brand's advertising style |
| 459 | A real tea-drink brand's campaign poster |
| 477 | Brand logos on the products in a flat lay |
| 478 | A real brand as the worked example |
| 486 | A named cricket franchise's branding |
| 487 | A named skincare brand's product commercial |
| 496 | Well-known brands' logos |
| 504 | A real public figure's photo as the worked example |
| 516 | A real brand's logo as the worked example |
