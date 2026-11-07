/* Google AI Pro Offer Injector — MAIN world
 * Rewrites Play checkout requests on one.google.com with the configured offer token.
 */
(() => {
  if (window.__GAPT_HOOKED) return;
  window.__GAPT_HOOKED = true;

  const PREFIX = "subs:com.google.android.apps.subscriptions.red:";

  const TOKENS = {
    "12m":
      "AVbCH/cspB8VdqKJ+ax/uPxUFAVuYRPX4iSjtg4Fu0PG9zUeURSM7y//LlQva1dCd/rbKVM6EJmyiG/C9BoQ95pMl8yDCHuiwCvM5CuGJME=",
    "6m_ss":
      "AVbCH/cgl4zvPT2Lqw95Gn+RYAJIVwFwi783i6uVprX7u3MCa5J4CcTXH5RABa5WjBrUoQnwf8vI10TNV7ktd26myIdxqIwGmNoJ5ofYNqpQ10RKoSvHs75wsfA=",
    "6m": "AVbCH/fUON3w8hpfeutprA3BMQwrBwYIB6Y8CSWi1isDCDswKUUhGQ3dluj5WXsNKOvr8K5UULBGrRsf4KlW1rHLz0fF3SvxQhBFRbBOHCA=",
    "6m_2":
      "AVbCH/cJ5SeHgkVRt+bdcrapx/aLSu2kbsBgMdI413zbllDNsNQoC/zVlG/Npbu55WlMzBwWPhUedIvZFDg7fw0CmCv/8DKj04IZ21pK7mrV/hh5",
  };
  const YT_FREE_TOK =
    "AVbCH/eW4Nfp8KBl0zlm7x9PhaiCvpnqwUuL2D8NFPBQgmM9+HuYS1h2xH5lLWU/BsBj775LK5GqK8UHKqoSzhDDz+PqP3paNesqLU0r9i6XCrYa";

  let currentTest = null;
  let subId = null;

  function makeCfg(layer) {
    if (!layer || layer === "off" || !TOKENS[layer]) return null;
    return {
      name: "gapt_" + layer,
      full_items: [
        [
          [PREFIX + "g1.2tb.ai.subs_variant", 21],
          null,
          null,
          null,
          1,
          [TOKENS[layer]],
        ],
        [
          [PREFIX + "g1.youtube_premium_individual_add_on", 21],
          null,
          null,
          null,
          1,
          [YT_FREE_TOK],
        ],
      ],
      ear8: null,
    };
  }

  let badge = null;
  let badgeText = null;
  let badgeIcon = null;
  let badgeCheck = null;
  let badgeLabel = null;
  let badgeDuration = null;
  let badgeDurationText = null;
  let badgeRenewal = null;
  function showBadge(text, active, duration = "", renewal = false) {
    try {
      if (!document.body) return;
      if (!badge) {
        badge = document.createElement("div");
        badge.className = "gapt-notice gapt-badge";
        badge.dataset.gaptOwned = "true";
        badge.setAttribute("role", "status");
        badge.setAttribute("aria-live", "polite");
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("class", "gapt-notice__icon");
        icon.setAttribute("viewBox", "0 0 24 24");
        icon.setAttribute("aria-hidden", "true");
        badgeIcon = document.createElementNS("http://www.w3.org/2000/svg", "path");
        icon.appendChild(badgeIcon);
        badgeCheck = document.createElementNS("http://www.w3.org/2000/svg", "path");
        badgeCheck.setAttribute("class", "gapt-badge__check");
        badgeCheck.setAttribute("d", "M9 12l2 2 4-4");
        badgeCheck.setAttribute("pathLength", "1");
        icon.appendChild(badgeCheck);
        badgeText = document.createElement("span");
        badgeText.className = "gapt-notice__text";
        badgeLabel = document.createElement("span");
        badgeDuration = document.createElement("span");
        badgeDuration.className = "gapt-badge__duration";
        const calendar = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        calendar.setAttribute("viewBox", "0 0 24 24");
        calendar.setAttribute("aria-hidden", "true");
        const calendarPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
        calendarPath.setAttribute("d", "M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z");
        calendar.appendChild(calendarPath);
        badgeDurationText = document.createElement("span");
        badgeDuration.append(calendar, badgeDurationText);
        badgeRenewal = document.createElement("span");
        badgeRenewal.className = "gapt-badge__renewal";
        badgeRenewal.textContent = "Renewal offer";
        badgeText.append(badgeLabel, badgeDuration, badgeRenewal);
        badge.append(icon, badgeText);
        document.body.appendChild(badge);
      }
      const kind = active ? "ok" : "idle";
      if (badge.dataset.kind !== kind) {
        badge.dataset.kind = kind;
        badgeIcon.setAttribute("d", active
          ? "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
          : "M8 12h8M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z");
      }
      if (badgeLabel.textContent !== text) badgeLabel.textContent = text;
      if (badgeDurationText.textContent !== duration) badgeDurationText.textContent = duration;
      badgeDuration.hidden = !duration;
      badgeRenewal.hidden = !renewal;
    } catch (e) { }
  }

  function refreshBadge() {
    if (!currentTest) {
      showBadge(
        "Injector: disabled",
        false,
      );
    } else {
      const label =
        {
          "12m": "12 months",
          "6m_ss": "6 months (SS)",
          "6m": "6 months",
          "6m_2": "6 months (backup)",
        }[currentTest.layer] || currentTest.layer;
      showBadge(
        "Offer ready",
        true,
        label,
        Boolean(subId),
      );
    }
  }

  function patchNode(node) {
    if (
      !Array.isArray(node) ||
      !currentTest ||
      !Array.isArray(currentTest.full_items)
    )
      return false;
    let patched = false;
    if (node.length > 12 && Array.isArray(node[12])) {
      if (
        Array.isArray(node[7]) &&
        Array.isArray(node[7][5]) &&
        node[7][5].length > 0
      )
        subId = node[7][5][0];
      node[12] = currentTest.full_items;
      const mode = (currentTest.ear8 && currentTest.ear8[1]) || 5;
      if (currentTest.ear8) node[7] = currentTest.ear8;
      else if (
        Array.isArray(node[7]) &&
        Array.isArray(node[7][5]) &&
        node[7][5].length > 0
      )
        node[7][1] = mode;
      else if (subId) node[7] = [null, mode, null, null, null, [subId]];
      patched = true;
    }
    if (
      node.length > 10 &&
      Array.isArray(node[10]) &&
      Array.isArray(node[10][0])
    ) {
      node[10] = [currentTest.full_items];
      if (
        Array.isArray(node[2]) &&
        Array.isArray(node[2][2]) &&
        Array.isArray(node[2][2][8])
      ) {
        node[2][2][8] = [currentTest.full_items];
      }
      if (currentTest.ear8) node[6] = currentTest.ear8;
      patched = true;
    }
    return patched;
  }

  function rewriteFreq(bodyStr) {
    if (
      typeof bodyStr !== "string" ||
      !bodyStr.includes("f.req=") ||
      !currentTest
    )
      return bodyStr;
    try {
      const parts = bodyStr.split("&");
      for (let i = 0; i < parts.length; i++) {
        if (parts[i].startsWith("f.req=")) {
          const raw = decodeURIComponent(
            parts[i].slice(6).replace(/\+/g, " "),
          );
          const outer = JSON.parse(raw);
          let changed = false;
          if (Array.isArray(outer)) {
            for (const group of outer) {
              if (!Array.isArray(group)) continue;
              for (const call of group) {
                if (
                  Array.isArray(call) &&
                  call[0] === "Ngbm" &&
                  typeof call[1] === "string"
                ) {
                  const payload = JSON.parse(call[1]);
                  if (Array.isArray(payload)) {
                    if (patchNode(payload[0])) changed = true;
                    if (patchNode(payload[1])) changed = true;
                  }
                  if (changed) call[1] = JSON.stringify(payload);
                }
              }
            }
          }
          if (changed) {
            parts[i] = "f.req=" + encodeURIComponent(JSON.stringify(outer));
            return parts.join("&");
          }
        }
      }
    } catch (e) { }
    return bodyStr;
  }

  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (typeof body === "string" && body.includes("Ngbm"))
        body = rewriteFreq(body);
    } catch (e) { }
    return origSend.call(this, body);
  };

  function rewriteUrl(val) {
    const s = String(val || "");
    if (!s.includes("/eacquire/multiline") || !currentTest) return val;
    try {
      const u = new URL(s, location.origin);
      const earStr = u.searchParams.get("ear");
      if (!earStr) return val;
      const ear = JSON.parse(earStr);
      if (
        Array.isArray(ear[8]) &&
        Array.isArray(ear[8][5]) &&
        ear[8][5].length > 0
      )
        subId = ear[8][5][0];
      if (Array.isArray(ear[13]) && Array.isArray(currentTest.full_items))
        ear[13][0] = currentTest.full_items;
      if (
        Array.isArray(ear[4]) &&
        typeof ear[4][0] === "string" &&
        Array.isArray(currentTest.full_items)
      ) {
        try {
          const inner4 = JSON.parse(atob(ear[4][0]));
          if (Array.isArray(inner4) && inner4.length > 8) {
            inner4[8] = [currentTest.full_items];
            ear[4][0] = btoa(JSON.stringify(inner4));
          }
        } catch (e4) { }
      }
      const mode = (currentTest.ear8 && currentTest.ear8[1]) || 5;
      if (currentTest.ear8) ear[8] = currentTest.ear8;
      else if (
        Array.isArray(ear[8]) &&
        Array.isArray(ear[8][5]) &&
        ear[8][5].length > 0
      )
        ear[8][1] = mode;
      else if (subId) ear[8] = [null, mode, null, null, null, [subId]];
      ear[24] = null;
      u.searchParams.set("ear", JSON.stringify(ear));
      return u.toString();
    } catch (e) {
      return val;
    }
  }

  if (window.TrustedTypePolicy && TrustedTypePolicy.prototype.createScriptURL) {
    const origCreate = TrustedTypePolicy.prototype.createScriptURL;
    TrustedTypePolicy.prototype.createScriptURL = function (input, ...args) {
      return origCreate.call(this, rewriteUrl(input), ...args);
    };
  }

  const srcDesc = Object.getOwnPropertyDescriptor(
    HTMLIFrameElement.prototype,
    "src",
  );
  if (srcDesc && srcDesc.set) {
    Object.defineProperty(HTMLIFrameElement.prototype, "src", {
      ...srcDesc,
      set(v) {
        if (typeof v === "string") v = rewriteUrl(v);
        srcDesc.set.call(this, v);
      },
    });
  }
  const origSetAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, val) {
    if (
      this instanceof HTMLIFrameElement &&
      String(name).toLowerCase() === "src"
    )
      val = rewriteUrl(String(val));
    return origSetAttr.call(this, name, val);
  };

  function applyLayer(layer) {
    const cfg = makeCfg(layer);
    currentTest = cfg ? Object.assign({ layer: layer }, cfg) : null;
    window.__GAPT_READY = true;
    window.__GAPT_STATE = { layer: layer || "off", mounted: !!currentTest };
    refreshBadge();
    try {
      window.postMessage(
        { __gapt: 1, type: "state", state: window.__GAPT_STATE },
        "*",
      );
    } catch (e) { }
  }

  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.__gapt !== 1) return;
    if (d.type === "cfg") applyLayer(d.layer);
    if (d.type === "ping") {
      try {
        window.postMessage(
          {
            __gapt: 1,
            type: "state",
            state: window.__GAPT_STATE || { layer: "off", mounted: false },
          },
          "*",
        );
      } catch (e) { }
    }
  });

  // Read the initial configuration (content.js also posts it once)
  const attrCfg = document.documentElement.getAttribute("data-gapt-layer");
  applyLayer(attrCfg || "off");
  document.addEventListener("DOMContentLoaded", refreshBadge);
})();
