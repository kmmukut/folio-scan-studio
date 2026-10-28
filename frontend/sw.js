// Folio's service worker. It makes Folio installable as an app on phones and
// computers, and nothing more: it keeps no copies of pages or data (Folio is
// no use without its server, and stored copies would outlive upgrades). When
// Folio can't be reached, it says so instead of the browser's error page.
'use strict';
const UNREACHABLE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Folio can’t be reached</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f2e9;color:#1b2b20;
font:15px/1.6 -apple-system,"Segoe UI",sans-serif}main{max-width:340px;padding:24px}
h1{font:400 34px/1.1 Georgia,serif;margin:0 0 14px}em{color:#b35a3c}a{color:#b35a3c;font-weight:700}</style></head>
<body><main><h1>Folio can’t be <em>reached.</em></h1><p>Folio runs on a computer on your network. Check that you’re
connected to that network (at home or on the office Wi-Fi) and that the computer is on.</p><p><a href="/">Try again</a></p>
</main></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;  // Everything else goes to the network as usual
  event.respondWith(fetch(event.request).catch(() =>
    new Response(UNREACHABLE, {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
