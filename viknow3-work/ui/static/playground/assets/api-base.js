/** Resolve ViKnow Access API URLs (browser → VIKNOW_PLAYGROUND_API_URL, not same-origin UI). */
(function () {
  const raw = typeof window.VIKNOW_API_BASE === "string" ? window.VIKNOW_API_BASE : "";
  const base = raw.replace(/\/$/, "");

  window.viknowApiUrl = function viknowApiUrl(path) {
    const suffix = path.startsWith("/") ? path : `/${path}`;
    return base ? `${base}${suffix}` : suffix;
  };

  window.viknowAppKey = function viknowAppKey() {
    const fromStorage = localStorage.getItem("viknow_app_key");
    if (fromStorage && fromStorage.trim()) return fromStorage.trim();
    if (typeof window.VIKNOW_APP_KEY === "string" && window.VIKNOW_APP_KEY.trim()) {
      return window.VIKNOW_APP_KEY.trim();
    }
    return "";
  };

  window.viknowApiHeaders = function viknowApiHeaders(extra) {
    const headers = { ...(extra || {}) };
    const key = window.viknowAppKey();
    if (key) {
      headers["X-App-Key"] = key;
    }
    return headers;
  };
})();
