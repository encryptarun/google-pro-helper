(() => {
  const tabList = document.querySelector('[role="tablist"]');
  const tabs = [...tabList.querySelectorAll('[role="tab"]')];
  const scrollArea = document.querySelector(".panel-area");
  const copyZip = document.getElementById("copyZip");
  const copyFeedback = document.getElementById("copyZipFeedback");
  let copyFeedbackTimer;

  copyZip.addEventListener("click", async () => {
    clearTimeout(copyFeedbackTimer);
    try {
      await navigator.clipboard.writeText("97201");
      copyFeedback.textContent = "Copied";
    } catch (_) {
      copyFeedback.textContent = "Couldn’t copy";
    }
    copyFeedbackTimer = setTimeout(() => {
      copyFeedback.textContent = "";
    }, 1500);
  });

  function selectTab(selected) {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden =
        !active;
    }
    scrollArea.scrollTop = 0;
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (event) => {
      let index = tabs.indexOf(tab);
      if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft")
        index = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[index]);
      tabs[index].focus();
    });
  }
})();
