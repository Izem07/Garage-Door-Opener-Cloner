// Halo service worker: shows Halo notifications (with the Halo icon) even when the
// app is closed. It does no caching, so it never serves an old version of Halo.
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (e) { data = { title: "Halo", body: event.data ? event.data.text() : "" }; }
  const actions = Array.isArray(data.actions) ? data.actions : [];
  const options = {
    body: data.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: data.tag || "halo",
    renotify: true,
    data: { url: data.url || "/", actions },
  };
  // Buttons show on Android and desktop; iPhone ignores them (tap opens Halo instead).
  if (actions.length) options.actions = actions.map(a => ({ action: a.action, title: a.title }));
  event.waitUntil(self.registration.showNotification(data.title || "Halo", options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const info = event.notification.data || {};
  const button = (info.actions || []).find(a => a.action === event.action);
  if (button) {
    event.waitUntil(fetch(button.url, { method: "POST" }).catch(() => {}));
    return;
  }
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    for (const w of windows) if ("focus" in w) return w.focus();
    return self.clients.openWindow(info.url || "/");
  }));
});
