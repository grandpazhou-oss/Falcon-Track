(() => {
  const themeButton = document.querySelector(".theme-toggle");
  if (themeButton) {
    const root = document.documentElement;
    function applyTheme(theme) {
      const dark = theme === "dark";
      const label = dark ? themeButton.dataset.lightLabel : themeButton.dataset.darkLabel;
      root.dataset.theme = theme;
      root.style.colorScheme = theme;
      themeButton.title = label;
      themeButton.setAttribute("aria-label", label);
      themeButton.setAttribute("aria-pressed", String(dark));
    }
    applyTheme(root.dataset.theme || "light");
    themeButton.addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      try { localStorage.setItem("radar-theme", next); } catch {}
    });
  }
  const toggle = document.querySelector(".menu-toggle");
  const sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    function setOpen(open) {
      sidebar.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? toggle.dataset.menuClose : toggle.dataset.menuOpen);
      if (open) (sidebar.querySelector("[aria-current]") || sidebar.querySelector("a")).focus();
    }
    toggle.addEventListener("click", () => setOpen(!sidebar.classList.contains("open")));
    document.addEventListener("click", event => {
      if (!sidebar.contains(event.target) && !toggle.contains(event.target)) setOpen(false);
    });
    sidebar.addEventListener("focusout", event => {
      if (event.relatedTarget && !sidebar.contains(event.relatedTarget) && event.relatedTarget !== toggle) setOpen(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && sidebar.classList.contains("open")) {
        setOpen(false);
        toggle.focus();
      }
    });
  }
  if (document.querySelector("[data-auto-refresh]")) {
    const refresh = () => {
      const busy = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      const editing = document.querySelector(".source-settings[open]");
      if (busy || editing || document.hidden) setTimeout(refresh, 10000);
      else window.location.reload();
    };
    setTimeout(refresh, 12000);
  }
})();

document.addEventListener("click", async event => {
  const evidence = event.target.closest('a[href^="#analysis-"]');
  if (evidence) {
    const target = document.getElementById(evidence.hash.slice(1));
    for (let parent = target?.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  }
  const button = event.target.closest("[data-copy-trigger]");
  if (!button || button.disabled) return;
  const lines = button.closest("[data-brief-copy]").querySelectorAll("[data-copy-line]");
  const content = Array.from(lines, line => line.textContent.trim()).join("\n\n");
  const label = button.textContent;
  button.disabled = true;
  try {
    await navigator.clipboard.writeText(content);
    button.textContent = button.dataset.copyOk;
  } catch {
    button.textContent = button.dataset.copyError;
  } finally {
    setTimeout(() => {
      button.textContent = label;
      button.disabled = false;
    }, 2000);
  }
});
