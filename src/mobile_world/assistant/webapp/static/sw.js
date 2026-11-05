/* Minimal service worker so Android Chrome offers "install". Nothing is cached:
 * the assistant needs the server anyway, and stale app code would be worse than none. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
