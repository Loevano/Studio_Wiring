(function shellBootstrap() {
  "use strict";

  const MANIFEST_PATH = "../manifests/tabs.json";
  const EMBEDDED_APP_CACHE_VERSION = "20260822-family-axis-fix-1";
  const THEME_STORAGE_KEY = "studioWiringThemeModeV1";
  const AUTO_SAVE_STORAGE_KEY = "studioWiringAutoSaveToDiskV1";
  const AUTO_SAVE_FLUSH_TIMEOUT_MS = 8000;

  const tabBar = document.getElementById("shellTabBar");
  const tabFrame = document.getElementById("shellTabFrame");
  const themeToggleBtn = document.getElementById("shellThemeToggle");
  const autoSaveToggleBtn = document.getElementById("shellAutoSaveToggle");
  const copyDebugReportBtn = document.getElementById("shellCopyDebugReportBtn");

  const prefersDarkMedia = (window.matchMedia && typeof window.matchMedia === "function")
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

  let activeTheme = "light";
  let autoSaveEnabled = false;
  let frameReady = false;
  let activeTabFrame = tabFrame;
  let autoSaveFlushSequence = 0;
  let tabNavigationSequence = 0;
  const pendingAutoSaveFlushes = new Map();
  const tabFramesByApp = new Map();
  const readyTabFrames = new WeakSet();

  function reportStatus(message, warn) {
    const text = String(message || "").trim();
    if (!text) return;
    if (warn) {
      console.warn(`[shell] ${text}`);
    } else {
      console.info(`[shell] ${text}`);
    }
  }

  function parseEmbeddedTabSelection(src) {
    try {
      const url = new URL(String(src || ""), window.location.href);
      const tab = String(url.searchParams.get("tab") || "").trim().toLowerCase();
      const matrixSubTab = String(url.searchParams.get("matrix_subtab") || "").trim().toLowerCase();
      return { tab, matrixSubTab };
    } catch (_error) {
      return { tab: "", matrixSubTab: "" };
    }
  }

  function canReuseLoadedFrame(nextSrc) {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return false;
    const currentSrc = String(activeTabFrame.src || "").trim();
    if (!currentSrc) return false;
    try {
      const currentUrl = new URL(currentSrc);
      const nextUrl = new URL(String(nextSrc || ""), window.location.href);
      return currentUrl.origin === nextUrl.origin
        && currentUrl.pathname === nextUrl.pathname;
    } catch (_error) {
      return false;
    }
  }

  function postTabSelectionToFrame(tab, matrixSubTab) {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return false;
    if (!activeTabFrame.contentWindow) return false;
    const normalizedTab = String(tab || "").trim().toLowerCase();
    if (!normalizedTab) return false;
    try {
      activeTabFrame.contentWindow.postMessage({
        type: "studio-shell-main-tab-set",
        tab: normalizedTab,
        matrix_subtab: String(matrixSubTab || "").trim().toLowerCase(),
      }, "*");
      return true;
    } catch (_error) {
      return false;
    }
  }

  function normalizeTabKey(value, knownKeys, fallbackKey) {
    const token = String(value || "").trim().toLowerCase();
    if (Array.isArray(knownKeys) && knownKeys.includes(token)) return token;
    return String(fallbackKey || "").trim().toLowerCase();
  }

  function readTabFromUrl() {
    const search = new URLSearchParams(window.location.search || "");
    return String(search.get("tab") || "").trim().toLowerCase();
  }

  function writeTabToUrl(tabKey) {
    const url = new URL(window.location.href);
    if (!tabKey) {
      url.searchParams.delete("tab");
    } else {
      url.searchParams.set("tab", tabKey);
    }
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function toAbsoluteUrl(base, relativePath) {
    try {
      return new URL(String(relativePath || ""), base).toString();
    } catch (_error) {
      return "";
    }
  }

  function frameCacheKey(src) {
    try {
      const url = new URL(String(src || ""), window.location.href);
      return `${url.origin}${url.pathname}`;
    } catch (_error) {
      return String(src || "").trim();
    }
  }

  function handleTabFrameLoad(frame) {
    if (!(frame instanceof HTMLIFrameElement)) return;
    readyTabFrames.add(frame);
    if (frame !== activeTabFrame) return;
    frameReady = true;
    postThemeToFrame(activeTheme);
    postAutoSaveToFrame(autoSaveEnabled);
    requestAutoSaveStateFromFrame();
  }

  function createTabFrame() {
    const frame = document.createElement("iframe");
    frame.className = "shell-frame";
    frame.title = "Routing Tab Frame";
    frame.loading = "eager";
    frame.hidden = true;
    frame.addEventListener("load", () => handleTabFrameLoad(frame));
    tabFrame.parentElement?.appendChild(frame);
    return frame;
  }

  function frameForSource(src) {
    const key = frameCacheKey(src);
    let frame = tabFramesByApp.get(key);
    if (frame instanceof HTMLIFrameElement) return frame;
    frame = tabFramesByApp.size === 0 ? tabFrame : createTabFrame();
    tabFramesByApp.set(key, frame);
    return frame;
  }

  function activateTabFrame(frame) {
    if (!(frame instanceof HTMLIFrameElement)) return;
    for (const cachedFrame of tabFramesByApp.values()) {
      cachedFrame.hidden = cachedFrame !== frame;
    }
    activeTabFrame = frame;
    frameReady = readyTabFrames.has(frame);
  }

  function normalizeTheme(mode) {
    const token = String(mode || "").trim().toLowerCase();
    if (token === "dark" || token === "light") return token;
    return "";
  }

  function resolveTheme(mode) {
    const normalized = normalizeTheme(mode);
    if (normalized) return normalized;
    return prefersDarkMedia?.matches ? "dark" : "light";
  }

  function loadThemePreference() {
    try {
      return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY) || "");
    } catch (_error) {
      return "";
    }
  }

  function persistThemePreference(mode) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, mode);
    } catch (_error) {
      // Ignore storage failures.
    }
  }

  function loadAutoSavePreference() {
    try {
      return window.localStorage.getItem(AUTO_SAVE_STORAGE_KEY) === "1";
    } catch (_error) {
      return false;
    }
  }

  function persistAutoSavePreference(enabled) {
    try {
      window.localStorage.setItem(AUTO_SAVE_STORAGE_KEY, enabled ? "1" : "0");
    } catch (_error) {
      // Ignore storage failures.
    }
  }

  function postThemeToFrame(mode) {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return;
    if (!activeTabFrame.contentWindow) return;
    const normalized = normalizeTheme(mode);
    if (!normalized) return;
    try {
      activeTabFrame.contentWindow.postMessage({ type: "studio-theme-set", mode: normalized }, "*");
    } catch (_error) {
      // Ignore message failures.
    }
  }

  function postAutoSaveToFrame(enabled) {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return;
    if (!activeTabFrame.contentWindow) return;
    try {
      activeTabFrame.contentWindow.postMessage({
        type: "studio-shell-autosave-set",
        enabled: Boolean(enabled),
      }, "*");
    } catch (_error) {
      // Ignore message failures.
    }
  }

  function requestAutoSaveStateFromFrame() {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return;
    if (!activeTabFrame.contentWindow) return;
    try {
      activeTabFrame.contentWindow.postMessage({ type: "studio-shell-autosave-request" }, "*");
    } catch (_error) {
      // Ignore message failures.
    }
  }

  function requestAutoSaveFlushFromFrame(reason) {
    if (!autoSaveEnabled || !frameReady) return Promise.resolve(true);
    if (!(activeTabFrame instanceof HTMLIFrameElement) || !activeTabFrame.contentWindow) {
      return Promise.resolve(true);
    }
    autoSaveFlushSequence += 1;
    const requestId = `shell-autosave-${autoSaveFlushSequence}`;
    return new Promise((resolve) => {
      const timeoutId = window.setTimeout(() => {
        pendingAutoSaveFlushes.delete(requestId);
        resolve(false);
      }, AUTO_SAVE_FLUSH_TIMEOUT_MS);
      pendingAutoSaveFlushes.set(requestId, {
        resolve: (ok) => {
          window.clearTimeout(timeoutId);
          pendingAutoSaveFlushes.delete(requestId);
          resolve(Boolean(ok));
        },
      });
      try {
        activeTabFrame.contentWindow.postMessage({
          type: "studio-shell-autosave-flush",
          request_id: requestId,
          reason: String(reason || "shell-navigation"),
        }, "*");
      } catch (_error) {
        const pending = pendingAutoSaveFlushes.get(requestId);
        if (pending) pending.resolve(false);
      }
    });
  }

  function requestDebugReportCopyFromFrame() {
    if (!(activeTabFrame instanceof HTMLIFrameElement)) return;
    if (!activeTabFrame.contentWindow) return;
    try {
      activeTabFrame.contentWindow.postMessage({ type: "studio-shell-copy-debug-report" }, "*");
    } catch (_error) {
      // Ignore message failures.
    }
  }

  function applyAutoSaveButtonState(enabled) {
    autoSaveEnabled = Boolean(enabled);
    persistAutoSavePreference(autoSaveEnabled);
    if (!(autoSaveToggleBtn instanceof HTMLButtonElement)) return;
    autoSaveToggleBtn.textContent = autoSaveEnabled ? "Auto Save: On" : "Auto Save: Off";
    autoSaveToggleBtn.setAttribute("aria-pressed", autoSaveEnabled ? "true" : "false");
    autoSaveToggleBtn.title = autoSaveEnabled
      ? "Disable automatic save after edits"
      : "Enable automatic save after edits";
  }

  function applyTheme(mode, persist = true, broadcast = true) {
    const resolved = resolveTheme(mode);
    const changed = resolved !== activeTheme;
    activeTheme = resolved;

    document.body.classList.toggle("theme-dark", resolved === "dark");
    document.body.classList.toggle("theme-light", resolved !== "dark");

    if (themeToggleBtn instanceof HTMLButtonElement) {
      const isDark = resolved === "dark";
      themeToggleBtn.textContent = isDark ? "Dark Mode: On" : "Dark Mode: Off";
      themeToggleBtn.title = isDark ? "Switch to light mode" : "Switch to dark mode";
    }

    if (persist) persistThemePreference(resolved);
    if (broadcast && (changed || !persist)) {
      postThemeToFrame(resolved);
    }
  }

  async function loadManifest() {
    const response = await fetch(MANIFEST_PATH, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Manifest load failed: HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (!payload || typeof payload !== "object") {
      throw new Error("Manifest JSON is invalid.");
    }
    const tabRows = Array.isArray(payload.tabs) ? payload.tabs : [];
    const tabs = tabRows
      .map((row) => ({
        key: String(row?.key || "").trim().toLowerCase(),
        label: String(row?.label || row?.key || "").trim(),
        src: String(row?.src || "").trim(),
      }))
      .filter((row) => row.key && row.src);
    if (!tabs.length) {
      throw new Error("Manifest has no tab entries.");
    }
    return {
      defaultTab: String(payload.default_tab || tabs[0].key).trim().toLowerCase(),
      tabs,
    };
  }

  function initUi(manifest) {
    if (!(tabBar instanceof HTMLElement) || !(tabFrame instanceof HTMLIFrameElement)) {
      throw new Error("Shell DOM elements are missing.");
    }

    const tabs = manifest.tabs;
    const knownKeys = tabs.map((tab) => tab.key);
    const defaultKey = normalizeTabKey(manifest.defaultTab, knownKeys, tabs[0].key);
    let activeKey = normalizeTabKey(readTabFromUrl(), knownKeys, defaultKey);

    function renderButtons() {
      tabBar.innerHTML = tabs.map((tab) => {
        const active = tab.key === activeKey ? " active" : "";
        return `<button type=\"button\" class=\"shell-tab-btn${active}\" data-shell-tab=\"${tab.key}\">${tab.label}</button>`;
      }).join("");
    }

    async function loadTab(tabKey) {
      tabNavigationSequence += 1;
      const navigationId = tabNavigationSequence;
      const nextKey = normalizeTabKey(tabKey, knownKeys, defaultKey);
      const tab = tabs.find((entry) => entry.key === nextKey) || tabs[0];

      if (frameReady && nextKey !== activeKey && autoSaveEnabled) {
        const flushed = await requestAutoSaveFlushFromFrame(`shell-tab:${activeKey || "unknown"}->${nextKey}`);
        if (navigationId !== tabNavigationSequence) return;
        if (!flushed) {
          reportStatus("Auto-save did not finish; tab switch was cancelled to protect unsaved changes.", true);
          requestAutoSaveStateFromFrame();
          return;
        }
      }
      activeKey = tab.key;

      const srcBase = toAbsoluteUrl(window.location.href, tab.src);
      let src = srcBase;
      try {
        const srcUrl = new URL(srcBase);
        srcUrl.searchParams.set("theme", activeTheme);
        srcUrl.searchParams.set("_shell_ui", EMBEDDED_APP_CACHE_VERSION);
        src = srcUrl.toString();
      } catch (_error) {
        // Keep base src.
      }

      if (!src) {
        reportStatus(`Invalid tab src for '${tab.key}'.`, true);
        return;
      }

      const tabFrame = frameForSource(src);
      activateTabFrame(tabFrame);
      const selection = parseEmbeddedTabSelection(src);
      const reuseFrame = frameReady && canReuseLoadedFrame(src);
      if (reuseFrame) {
        const routed = !selection.tab
          || postTabSelectionToFrame(selection.tab, selection.matrixSubTab);
        if (!routed) {
          frameReady = false;
          readyTabFrames.delete(tabFrame);
          tabFrame.src = src;
        } else {
          postThemeToFrame(activeTheme);
          postAutoSaveToFrame(autoSaveEnabled);
          requestAutoSaveStateFromFrame();
        }
      } else if (String(tabFrame.src || "") !== src) {
        frameReady = false;
        readyTabFrames.delete(tabFrame);
        tabFrame.src = src;
      } else {
        postThemeToFrame(activeTheme);
        postAutoSaveToFrame(autoSaveEnabled);
        requestAutoSaveStateFromFrame();
      }
      renderButtons();
      writeTabToUrl(tab.key);
    }

    tabBar.addEventListener("click", (event) => {
      const target = event.target instanceof HTMLElement
        ? event.target.closest("[data-shell-tab]")
        : null;
      if (!(target instanceof HTMLElement)) return;
      const tabKey = String(target.getAttribute("data-shell-tab") || "").trim();
      if (!tabKey) return;
      void loadTab(tabKey);
    });

    tabFrame.addEventListener("load", () => {
      handleTabFrameLoad(tabFrame);
    });

    window.addEventListener("message", (event) => {
      if (event.source !== activeTabFrame.contentWindow) return;
      const data = event?.data;
      if (!data || typeof data !== "object") return;
      if (data.type === "studio-shell-autosave-flushed") {
        const requestId = String(data.request_id || "");
        const pending = pendingAutoSaveFlushes.get(requestId);
        if (pending) pending.resolve(Boolean(data.ok));
        return;
      }
      if (data.type === "studio-theme-request") {
        postThemeToFrame(activeTheme);
        return;
      }
      if (data.type === "studio-theme-changed") {
        const incoming = normalizeTheme(data.mode);
        if (!incoming || incoming === activeTheme) return;
        applyTheme(incoming, true, false);
        return;
      }
      if (data.type === "studio-shell-autosave-state" || data.type === "studio-shell-autosave-changed") {
        applyAutoSaveButtonState(Boolean(data.enabled));
      }
    });

    if (themeToggleBtn instanceof HTMLButtonElement) {
      themeToggleBtn.addEventListener("click", () => {
        const next = activeTheme === "dark" ? "light" : "dark";
        applyTheme(next, true, true);
      });
    }
    if (autoSaveToggleBtn instanceof HTMLButtonElement) {
      autoSaveToggleBtn.addEventListener("click", () => {
        const next = !autoSaveEnabled;
        applyAutoSaveButtonState(next);
        postAutoSaveToFrame(next);
      });
    }
    if (copyDebugReportBtn instanceof HTMLButtonElement) {
      copyDebugReportBtn.addEventListener("click", () => {
        requestDebugReportCopyFromFrame();
      });
    }

    applyAutoSaveButtonState(loadAutoSavePreference());
    applyTheme(loadThemePreference(), false, false);
    void loadTab(activeKey);
  }

  loadManifest()
    .then((manifest) => {
      initUi(manifest);
    })
    .catch((error) => {
      reportStatus(String(error || "Shell initialization failed."), true);
    });
})();
