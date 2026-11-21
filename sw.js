// ============================================================
// Service Worker (sw) - map vector tile cache
// ============================================================
// Adapted from Boulder Group Rides (MIT License, (c) 2026 Roger Bergua).
//
// Keeps a copy of every map vector tile on the device for 120 days, so a
// return visit draws the map from disk instead of the network. Tiles from
// OpenFreeMap (the default map) and MapTiler (optional, see MAPTILER_API_KEY
// in index.html) are cached; every other request is left alone.
//
// On activation, old cache versions are deleted and tiles older than
// 120 days are swept out. Bump CACHE_NAME to force every device to start
// from an empty cache.
//
// This file must sit next to index.html: a service worker only controls
// pages in its own folder and below.
// ============================================================

const CACHE_NAME  = 'map-tiles-v1';      // bump to v2 to force-clear all cached tiles
const MAX_AGE_SEC = 120 * 24 * 3600;      // 120 days in seconds

function normalizeRequest(request) {
  const url = new URL(request.url);
  return new Request(url.origin + url.pathname);
}

// Stores a copy of the tile with a timestamp header, used later to expire it.
async function saveTile(cache, cacheKey, response) {
  const headers = new Headers(response.headers);
  headers.set('sw-cache-time', Date.now().toString());

  await cache.put(cacheKey, new Response(await response.arrayBuffer(), {
    status: response.status,
    statusText: response.statusText,
    headers
  }));
}

// Runs once when this SW version activates (fresh install or after a CACHE_NAME bump).
// 1. Deletes every cache that isn't CACHE_NAME (cleans up old versions).
// 2. Sweeps the current cache and deletes anything already older than MAX_AGE_SEC,
//    so the cache never silently accumulates tiles and grows in size.
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(async keys => {

      // 1. Delete old cache versions
      await Promise.all(
        keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
      );

      // 2. Sweep expired tiles out of the current cache
      const cache   = await caches.open(CACHE_NAME);
      const requests = await cache.keys();
      const now     = Date.now();

      await Promise.all(
        requests.map(async req => {
          const res        = await cache.match(req);
          const cachedTime = res?.headers.get('sw-cache-time');
          if (!cachedTime) return;
          const age = (now - parseInt(cachedTime, 10)) / 1000;
          if (age >= MAX_AGE_SEC) await cache.delete(req);
        })
      );
    })
  );
});

self.addEventListener('fetch', event => {
  const url = event.request.url;

  // Only intercept map vector tile requests. Ignore everything else.
  // HEAD requests (used for the availability) are not cached
  if (!url.includes('api.maptiler.com/tiles/') && !/^https:\/\/tiles\.openfreemap\.org\/planet\/.+\.pbf/.test(url)) return;

  if (event.request.method !== 'GET') return;

  const cacheKey = normalizeRequest(event.request);
  let saving = Promise.resolve(); // Replaced by the background cache write on a successful network fetch

  const tile = caches.open(CACHE_NAME).then(async cache => {

    // 1. Check the cache first
    const cached = await cache.match(cacheKey);

    if (cached) {
      const cachedTime = cached.headers.get('sw-cache-time');

      if (cachedTime) {
        const age = (Date.now() - parseInt(cachedTime, 10)) / 1000;

        // If the cached tile is still fresh, return it immediately
        if (age < MAX_AGE_SEC) {
          return cached;
        }

        // Expired: block and fetch fresh from the tile server
        await cache.delete(cacheKey);
      }
    }

    // 2. Cache miss: fetch from the tile server
    const response = await fetch(event.request);

    // 3. Save a copy in the background (only if the request succeeded)
    if (response.ok) {
      saving = saveTile(cache, cacheKey, response.clone())
        .catch(() => {}); // Ignore write errors (e.g. storage full): the tile is simply fetched again next time
    }

    // 4. Return the original response to the browser immediately
    return response;
  });

  // Both are registered synchronously.
  event.respondWith(tile);
  event.waitUntil(tile.then(() => saving, () => {})); // Keeps the SW alive until the background write finishes
});