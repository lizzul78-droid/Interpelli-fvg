// Funzionamento senza rete: l'app si apre sempre, i dati si aggiornano quando c'e' connessione.
const NOME = "interpelli-v1";
const BASE = ["./", "index.html", "config.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(NOME).then(c => c.addAll(BASE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== NOME).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET" || new URL(e.request.url).origin !== location.origin) return;
  const dati = e.request.url.includes("/data/");
  e.respondWith(
    fetch(e.request).then(r => {                       // prima la rete (dati sempre freschi)...
      const copia = r.clone();
      caches.open(NOME).then(c => c.put(e.request, copia));
      return r;
    }).catch(() => caches.match(e.request).then(r => r || (dati ? new Response("{}", {headers: {"Content-Type": "application/json"}}) : caches.match("index.html"))))
  );
});
