// ESSA desktop app — the service worker that makes the app installable.
//
// It caches NOTHING. Every request goes to the server exactly as it would
// without it: stock, bills and money must never be answered from a copy kept on
// this machine, and an ERP that showed yesterday's stock while offline would be
// worse than one that says it cannot reach the server. Its only job is to exist,
// with a fetch handler, so Chrome and Edge offer "Install ESSA".
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', () => {
  // no respondWith: the browser fetches from the network as usual
})
