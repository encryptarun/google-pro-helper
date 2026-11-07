const LABELS = {
  off: "Disabled",
  "12m": "12 months",
  "6m_ss": "6 months (SS)",
  "6m": "6 months",
  "6m_2": "6 months (backup)",
};
const stateEl = document.getElementById("state");
const stateTextEl = document.getElementById("stateText");
const stateIconEl = document.getElementById("stateIcon");

function paintState(st) {
  const layer = (st && st.layer) || "off";
  const mounted = !!(st && st.mounted) && layer !== "off";
  stateEl.className = mounted ? "ok" : "warn";
  stateIconEl.setAttribute(
    "href",
    mounted ? "#icon-status-ok" : "#icon-status-info",
  );
  stateTextEl.textContent = mounted
    ? "Active: " +
    (LABELS[layer] || layer) +
    ". Select Get discount on the plans page."
    : "No active offer detected. Open the plans page from Instructions.";
}

function queryTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const t = tabs && tabs[0];
    if (!t) return paintState(null);
    chrome.tabs.sendMessage(t.id, { type: "getState" }, (resp) => {
      if (chrome.runtime.lastError || !resp) return paintState(null);
      paintState(resp);
    });
  });
}

chrome.storage.local.get(["gapt_layer"], (r) => {
  const layer = (r && r.gapt_layer) || "off";
  document.querySelectorAll('input[name="layer"]').forEach((el) => {
    el.checked = el.value === layer;
    el.addEventListener("change", () => {
      if (!el.checked) return;
      chrome.storage.local.set({ gapt_layer: el.value }, () => {
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
          const t = tabs && tabs[0];
          if (!t) return queryTab();
          chrome.tabs.sendMessage(
            t.id,
            { type: "setLayer", layer: el.value },
            () => {
              void chrome.runtime.lastError;
              setTimeout(queryTab, 350);
            },
          );
        });
      });
    });
  });
  queryTab();
  setTimeout(queryTab, 800);
});

setInterval(queryTab, 1500);
