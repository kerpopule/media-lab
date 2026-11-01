/* Media Lab service worker — app shell cached for instant open,
   network-first for API and media, web push for finished creations. */
const CACHE = "medialab-studio-v24";
const SHELL = [
  "/",
  "/cut",
  "/manifest.json",
  "/static/cut.html",
  "/static/cut.css",
  "/static/cut.js",
  "/static/medialab-chat.js",
  "/static/image-templates.css",
  "/static/image-templates.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // MEDIA IS NEVER TOUCHED. <video>/<audio> fetch with Range headers; the old
  // code ran them through the Cache API, where cache.put() rejects any 206 and
  // a cached full 200 gets replayed for a range request. That is a classic
  // cause of a clip refetching and restarting mid-play. The server already
  // answers Range correctly (accept-ranges: bytes, 206 + content-range), so the
  // best thing a service worker can do here is get out of the way entirely.
  if (url.pathname.startsWith("/media/")) return;

  // The local template collection is ~143 MiB. Load its JSON and previews
  // lazily from the local server, but never duplicate the whole library in the
  // browser Cache API as a user scrolls through it.
  if (url.pathname.startsWith("/static/template-library/")) return;

  if (url.pathname.startsWith("/api/")) {
    // network-first: live data when online, last-known when not
    e.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }
  // shell/static: NETWORK-FIRST with cache fallback. Stale-while-revalidate
  // meant a phone kept running yesterday's UI for a whole visit after every
  // deploy — fresh code matters more than a few hundred ms on open.
  e.respondWith(
    fetch(req)
      .then((r) => {
        if (r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return r;
      })
      .catch(() => caches.match(req))
  );
});

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data.json(); } catch (err) {}
  e.waitUntil(
    self.registration.showNotification(data.title || "Media Lab", {
      body: data.body || "Your creation is ready.",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      data: { url: data.url || "/?queue=1" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/?queue=1";
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) { w.navigate(target); return w.focus(); }
      }
      return clients.openWindow(target);
    })
  );
});
