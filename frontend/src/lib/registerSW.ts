/**
 * Service-worker registration helper.
 *
 * Called from main.tsx on startup. The SW is only registered in
 * production builds — the dev server changes ports / proxies
 * often, and an active SW caching the shell would mask backend
 * changes from the principal driving the dev loop.
 *
 * Idempotent: ``navigator.serviceWorker.register`` returns the
 * existing registration if the SW is already installed. Failures
 * are logged but swallowed — the SPA still works without a SW,
 * the principal just loses the install prompt + offline shell.
 */
export function registerServiceWorker(): void {
  if (import.meta.env.DEV) {
    return;
  }
  if (!("serviceWorker" in navigator)) {
    return;
  }
  // Wait for load so SW registration doesn't compete with the
  // first paint of the SPA.
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch((err: unknown) => {
        // eslint-disable-next-line no-console
        console.warn("sw registration failed", err);
      });
  });
}
