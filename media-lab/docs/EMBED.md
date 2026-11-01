# Media Lab inside VibeX Studio

The VibeX Studio app shows a paired Media Lab inside its **Create** tab: an
iframe on web and desktop, a WebView on phones. This page is how that frame is
allowed in and how it signs in. Code: `media_lab_core/embed_gate.py` (server),
`app/src/components/media-lab/` and `app/src/lib/media-lab-embed.ts` (app).

## Who may frame the studio

Every response carries

```
Content-Security-Policy: frame-ancestors 'self' <allowed app origins>
X-Frame-Options: SAMEORIGIN
```

Allowed app origins are:

- the desktop app's own origins (`tauri://localhost`, `http://tauri.localhost`),
- every origin in `MEDIA_LAB_BROWSER_ORIGINS` (config/local.env), for example a
  hosted copy of the VibeX Studio web app,
- `http://localhost:*` and `http://127.0.0.1:*` only while the studio itself is
  reached over loopback (a laptop running the app's dev server next to its
  studio).

Any other page that tries to frame the studio gets a blank frame, so the studio
cannot be clickjacked. `X-Frame-Options` is only for browsers too old to know
`frame-ancestors`; newer ones ignore it when `frame-ancestors` is present.

The manifest says `"vibexEmbed": 1`, which is how the app knows it can show the
studio inside itself. Older studios keep opening in their own tab.

## How the frame signs in

A frame on another site is sent no `SameSite=Lax` cookie, and a long-lived pass
must never ride in a URL. So:

1. The app loads `/embed?next=/?embed=1` in the frame. `/embed` holds no secret.
2. If the frame is not signed in, `/embed` asks its parent for a ticket
   (`postMessage` to `window.parent`, or the phone app's native bridge).
3. The app mints one with the generation pass it got when it was paired with
   the family (or admin) code:
   `POST /api/embed/ticket`, `Authorization: Bearer <render pass>`. A ticket
   works once, lives 60 seconds, carries the pass's role (family or owner),
   and is bound to the app
   origin the browser reported (`Origin`). An app origin that is not allowed
   gets `403 {"error": "origin-not-allowed"}`.
4. The app posts the ticket into the frame, addressed to the studio's exact
   origin. The frame accepts it only from its own parent and only from an
   allowed app origin, then redeems it same-origin: `POST /api/embed/redeem`.
5. The server sets `mlab_embed` (24 hours; the app re-handshakes when it is
   older than 12). Over HTTPS it is `SameSite=None; Secure; Partitioned`, a
   CHIPS cookie that exists only under the app's own top-level site. Over plain
   HTTP a cross-site frame cannot hold any cookie; there it is `SameSite=Lax`,
   which works when the app and the studio share a site or in the phone
   WebView (where the studio is the top-level page). When the cookie does not
   stick, `/embed` says so and the app offers "Open Media Lab in its own window".
6. `mlab_embed` counts only on requests the studio page itself made
   (`Sec-Fetch-Site: same-origin`, or `none` for a reload), so no other site
   can ride on it. Rotating the family or admin code signs every embed session
   out, like every other pass.

Nothing here trusts a client IP.

## Checking a studio

```sh
curl -sI https://<studio>/embed | grep -i -E 'content-security-policy|x-frame-options'
curl -s https://<studio>/manifest.json | grep vibexEmbed
# an app origin that is not listed is refused before a ticket exists:
curl -s -X POST -H 'Origin: https://not-listed.example' -H 'Authorization: Bearer x' \
  https://<studio>/api/embed/ticket          # 401 without a real pass
```

To let a hosted copy of the app show the studio, add its exact origin to
`MEDIA_LAB_BROWSER_ORIGINS` and restart the studio. Plain-HTTP studios work
inside the app only when the app is on the same site; pair devices with the
studio's HTTPS address for the in-app view everywhere.
