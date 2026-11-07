/* Google AI Pro Offer Injector — content script (isolated world: sends configuration and receives status) */
(() => {
  function applyPageTheme(mode) {
    if (!document.documentElement) {
      document.addEventListener("DOMContentLoaded", () => applyPageTheme(mode), { once: true });
      return;
    }
    document.documentElement.setAttribute("data-gapt-theme",
      mode === "light" || mode === "dark" ? mode : "system");
  }

  try {
    chrome.storage.local.get(["gapt_theme"], (values) => applyPageTheme(values.gapt_theme));
    chrome.storage.onChanged.addListener((changes) => {
      if (changes.gapt_theme) applyPageTheme(changes.gapt_theme.newValue);
    });
  } catch (_) { }

  // Checkout frames only need appearance synchronization, not offer configuration.
  if (location.hostname !== "one.google.com") return;

  let lastState = { layer: "off", mounted: false };

  function push(layer) {
    try {
      document.documentElement.setAttribute("data-gapt-layer", layer || "off");
    } catch (e) { }
    window.postMessage({ __gapt: 1, type: "cfg", layer: layer || "off" }, "*");
  }

  function loadAndPush() {
    try {
      chrome.storage.local.get(["gapt_layer"], (r) =>
        push(r && r.gapt_layer ? r.gapt_layer : "off"),
      );
    } catch (e) {
      push("off");
    }
  }

  // Receive status from the MAIN world
  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (d && d.__gapt === 1 && d.type === "state" && d.state)
      lastState = d.state;
  });

  // Handle popup status queries and configuration updates
  try {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (!msg) return;
      if (msg.type === "getState") {
        window.postMessage({ __gapt: 1, type: "ping" }, "*");
        setTimeout(() => sendResponse(lastState), 150);
        return true;
      }
      if (msg.type === "setLayer") {
        push(msg.layer);
        setTimeout(() => sendResponse({ ok: true, state: lastState }), 150);
        return true;
      }
    });
    chrome.storage.onChanged.addListener((ch) => {
      if (ch && ch.gapt_layer) push(ch.gapt_layer.newValue || "off");
    });
  } catch (e) { }

  loadAndPush();
  // Resend after SPA navigation in case the hook mounts later
  setTimeout(loadAndPush, 800);
  setTimeout(loadAndPush, 2500);
})();
