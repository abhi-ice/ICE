import React from "react";
import ReactDOM from "react-dom/client";
import "@/index.css";
import App from "@/App";
import ErrorBoundary from "@/components/ErrorBoundary";

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    {/* Day-1 audit finding 43: a render crash shows a calm message with
        Reload / Go to home instead of a blank white page. */}
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);

// PWA service worker — RE-ENABLED for full offline Daily Report support.
//
// The old SW was removed because it was cache-first for EVERYTHING and kept
// serving stale bundles after deploys. The new public/service-worker.js is
// NETWORK-FIRST for HTML (always tries the network for the app shell first,
// ~3s timeout, cache fallback) and cache-first only for content-hashed
// /static/* files — so a fresh deploy is always picked up while the app can
// still boot with zero connectivity. See public/service-worker.js for the
// full rationale.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    // One-time cleanup: the old self-destruct SW deleted every cache on
    // activate; if a client still has a stray legacy cache under the old
    // (unversioned / workbox) names, remove it so it can't shadow the new
    // versioned caches. Best-effort; the new SW also prunes on activate.
    if (window.caches && caches.keys) {
      caches.keys()
        .then((keys) => Promise.all(
          keys
            .filter((k) => k.startsWith("workbox-") || k === "ice-cache" || k === "ice")
            .map((k) => caches.delete(k)),
        ))
        .catch(() => {});
    }
    navigator.serviceWorker
      .register("/service-worker.js")
      .catch(() => { /* SW registration is best-effort; app works online without it */ });
  });
}
