/** Resolve ViKnow access API base (separate port from Playground UI). */
window.PlaygroundApi = (() => {
  function base() {
    const configured = String(window.VIKNOW_API_URL || "").replace(/\/$/, "");
    return configured;
  }

  function join(path) {
    const root = base();
    if (!root) {
      return path;
    }
    return `${root}${path}`;
  }

  return {
    base,
    knowledge: () => join("/api/v1/knowledge"),
    agent: () => join("/api/v1/agent"),
    users: () => join("/api/v1/users"),
  };
})();
