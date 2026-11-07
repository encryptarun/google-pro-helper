/* Google AI Pro Offer Injector — checkout price guard
 * Reads terms in the Play checkout iframe and displays the detected price status.
 */
(() => {
  if (window.__GAPT_GUARD || window.top === window) return; // Run only in child frames
  window.__GAPT_GUARD = true;

  const FREE_WORDS = [
    "free trial",
    "start your free trial",
    "US$0.00",
    "$0.00",
    "0,00",
    "€0.00",
    "won't be charged",
  ];
  const PAID_WORDS = [
    "/mo after",
    "/month after",
    "per month after",
    "pay now",
    "charged today",
  ];

  let bar = null;
  let label = null;
  let iconPath = null;
  const ICON_PATHS = {
    ok: "M5 12l4 4L19 6",
    bad: "M6 6l12 12M18 6L6 18",
    warn: "M12 8v5m0 3h.01M12 3L2 21h20L12 3z",
  };

  function paint(kind, text) {
    if (!document.body) return;
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "gapt-notice gapt-guard";
      bar.dataset.gaptOwned = "true";
      bar.setAttribute("role", "status");
      bar.setAttribute("aria-live", "polite");
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "gapt-notice__icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "2");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.setAttribute("aria-hidden", "true");
      iconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
      icon.appendChild(iconPath);
      label = document.createElement("span");
      label.className = "gapt-notice__text";
      bar.append(icon, label);
      document.body.appendChild(bar);
    }
    if (bar.dataset.kind !== kind) {
      bar.dataset.kind = kind;
      iconPath.setAttribute("d", ICON_PATHS[kind]);
    }
    if (label.textContent !== text) label.textContent = text;
  }

  function judge() {
    let t = "";
    try {
      // Exclude our own visible text without changing the page's rendered-text semantics.
      if (bar) bar.style.setProperty("display", "none", "important");
      try {
        t = ((document.body && document.body.innerText) || "").toLowerCase();
      } finally {
        if (bar) bar.style.removeProperty("display");
      }
    } catch (e) {
      return;
    }
    if (!t || t.length < 5) return;
    const free = FREE_WORDS.some((w) => t.includes(w.toLowerCase()));
    const paid = PAID_WORDS.some((w) => t.includes(w.toLowerCase()));

    if (free && !paid) {
      paint(
        "ok",
        "Free trial detected — check that the amount is $0.00 before subscribing",
      );
    } else if (paid && !free) {
      paint(
        "bad",
        "No free trial detected; a charge may apply — do not subscribe",
      );
    } else if (free && paid) {
      paint(
        "warn",
        "Both free and paid terms appear — verify the amount manually",
      );
    } else {
      paint(
        "warn",
        "Price terms not detected (the checkout may still be loading)",
      );
    }
  }

  setTimeout(judge, 1200);
  setTimeout(judge, 3000);
  setTimeout(judge, 6000);
  try {
    const mo = new MutationObserver((mutations) => {
      const isOwnNode = (node) => node === bar || (bar && bar.contains(node));
      if (mutations.some((mutation) => {
        if (isOwnNode(mutation.target)) return false;
        if (mutation.type !== "childList") return true;
        return [...mutation.addedNodes, ...mutation.removedNodes].some(
          (node) => !isOwnNode(node),
        );
      })) judge();
    });
    const observe = () => {
      if (document.body) mo.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", observe, { once: true });
    } else {
      observe();
    }
  } catch (e) { }
})();
