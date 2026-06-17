// Fetch Push has no dashboard UI — it only exposes the /api/plugins/fetch/*
// device-registration routes (see plugin_api.py). Register a hidden, no-op tab
// so the dashboard plugin loader is satisfied.
(function () {
  window.__HERMES_PLUGINS__.register("fetch", function () {
    return null;
  });
})();
