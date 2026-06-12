/* PyPDF Editor PWA service worker — caches the app shell + MuPDF.js engine for offline use.
   Bump CACHE whenever index.html or the vendored libraries change so phones fetch the new copy. */
const CACHE = "pypdf-pwa-v4-mupdf";

const APP_SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-180.png",
  "./icon-192.png",
  "./icon-512.png",
  // engine + pen (all same-origin, vendored — works fully offline)
  "./vendor/pdf-lib.min.js",
  "./vendor/mupdf/mupdf.js",
  "./vendor/mupdf/mupdf-wasm.js",
  "./vendor/mupdf/mupdf-wasm.wasm",
];

self.addEventListener("install", (e)=>{
  e.waitUntil((async ()=>{
    const cache = await caches.open(CACHE);
    await cache.addAll(APP_SHELL);   // includes the ~10MB wasm — cached once, then offline
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (e)=>{
  e.waitUntil((async ()=>{
    const keys = await caches.keys();
    await Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)));
    self.clients.claim();
  })());
});

self.addEventListener("fetch", (e)=>{
  const req = e.request;
  if (req.method !== "GET") return;
  e.respondWith((async ()=>{
    const cached = await caches.match(req);
    if (cached) return cached;
    try {
      const res = await fetch(req);
      if (res && res.ok && new URL(req.url).origin === location.origin){
        const cache = await caches.open(CACHE); cache.put(req, res.clone());
      }
      return res;
    } catch (err){
      if (req.mode === "navigate") return caches.match("./index.html");
      throw err;
    }
  })());
});
