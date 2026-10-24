/* Service worker minimal — SENGAJA tanpa penyimpanan cache.
 *
 * Kenapa: halaman ini adalah UI kendali agent. HTML/API yang tersaji dari
 * simpanan lama membuat user melihat state palsu (task yang sudah selesai
 * tampak jalan, dsb). Satu-satunya alasan berkas ini ada adalah syarat
 * "Add to Home Screen" (installability) di Chrome/Edge.
 *
 * Jadi: semua request diteruskan apa adanya ke jaringan
 * (`fetch(event.request)`), tanpa menyimpan apa pun lebih dulu, tanpa
 * offline fallback, dan API penyimpanan offline sama sekali tidak dipakai. `skipWaiting` +
 * `clients.claim` supaya versi baru langsung mengambil alih tanpa user harus
 * menutup tab.
 *
 * Catatan: service worker hanya berjalan di secure context (https atau
 * localhost). Di http://<IP-LAN> API-nya bahkan tidak diekspos browser, jadi
 * pendaftaran gagal — halaman menangkapnya diam-diam dan tetap normal dipakai
 * sebagai web biasa.
 */
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // Hanya GET yang masuk akal dipassthrough; sisanya biarkan browser urus.
  if (req.method !== "GET") return;
  event.respondWith(fetch(req));
});
