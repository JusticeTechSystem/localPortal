/* EduPortal service worker: caches the app shell (CSS/JS/icons) and shows a friendly offline page.
   Signed-in pages are never cached, so nothing private is stored on shared devices. */
var VERSION = 'eduportal-v1';
var SHELL = ['/offline', '/static/css/style.css', '/static/js/app.js', '/static/js/theme.js', '/static/favicon.svg',
             '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(VERSION).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== VERSION; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener('fetch', function (e) {
  var req = e.request; var url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (req.headers.has('range')) return;                                   // never touch video streaming
  if (req.mode === 'navigate') {                                          // pages: network first, offline page on failure
    e.respondWith(fetch(req).catch(function () { return caches.match('/offline'); }));
    return;
  }
  if (url.pathname.indexOf('/static/') === 0) {                           // static files: cache first, refresh in background
    e.respondWith(caches.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) { var copy = res.clone(); caches.open(VERSION).then(function (c) { c.put(req, copy); }); return res; }).catch(function () { return hit; });
      return hit || net;
    }));
  }
});
