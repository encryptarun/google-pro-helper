(() => {
  // Two-position switch. It follows the OS theme by default; a click overrides it,
  // and the override clears when it matches the OS again or the OS theme changes.
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const button = document.getElementById("themeToggle");
  const osTheme = () => (systemTheme.matches ? "dark" : "light");
  let mode = "system";
  try {
    const saved = localStorage.getItem("gph-theme");
    if (saved === "light" || saved === "dark") mode = saved;
  } catch (_) { }

  function sync() {
    const resolved = mode === "system" ? osTheme() : mode;
    document.documentElement.dataset.theme = resolved;
    const next = resolved === "dark" ? "light" : "dark";
    const label = `${resolved === "dark" ? "Dark" : "Light"} theme${mode === "system" ? " (system)" : ""}. Switch to ${next}.`;
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  function save() {
    try {
      localStorage.setItem("gph-theme", mode);
    } catch (_) { }
    if (globalThis.chrome?.storage?.local) {
      chrome.storage.local.set({ gapt_theme: mode }, () => {
        void chrome.runtime.lastError;
      });
    }
  }

  button.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    mode = next === osTheme() ? "system" : next;
    save();
    sync();
  });
  systemTheme.addEventListener("change", () => {
    mode = "system";
    save();
    sync();
  });
  sync();
  save();
})();
