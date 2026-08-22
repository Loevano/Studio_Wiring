(function visibilityBootstrap() {
  "use strict";

  const CONFIG_API = "/api/config";
  const SAVE_MODEL_API = "/api/save-model";
  const AUTO_SAVE_DELAY_MS = 700;
  const PROJECT_SELECTION_STORAGE_KEY = "studioWiringProjectSelectionV1";
  const TARGETS = [
    { key: "wiring_matrix", label: "Show in Wiring Matrix" },
    { key: "routing_matrix", label: "Show in Routing Matrix" },
    { key: "connection_overview", label: "Show in Connection Overview" },
    { key: "visuals", label: "Show in Visuals" },
  ];

  const dom = {
    context: document.getElementById("visibilityContext"),
    summary: document.getElementById("visibilitySummary"),
    list: document.getElementById("visibilityList"),
    saveState: document.getElementById("saveState"),
    showAll: document.getElementById("showAllButton"),
    hideAll: document.getElementById("hideAllButton"),
    reload: document.getElementById("reloadButton"),
    save: document.getElementById("saveButton"),
    status: document.getElementById("statusToast"),
  };

  const state = {
    config: null,
    model: null,
    modelPath: "",
    modelHash: "",
    savedFingerprint: "",
    selected: new Set(),
    anchor: "",
    loading: false,
    saving: false,
    conflicted: false,
    autoSaveEnabled: false,
    autoSaveTimer: 0,
    savePromise: null,
  };
  let statusTimer = 0;

  function text(value) { return String(value == null ? "" : value).trim(); }
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }
  function naturalCompare(a, b) {
    return String(a || "").localeCompare(String(b || ""), undefined, { numeric: true, sensitivity: "base" });
  }
  function devices() { return Array.isArray(state.model?.devices) ? state.model.devices : []; }
  function deviceName(device) { return text(device?.name); }
  function legacyVisible(device) { return device?.hidden !== true && device?.visible !== false; }
  function visibleFor(device, target) {
    const value = device?.visibility?.[target];
    return typeof value === "boolean" ? value : legacyVisible(device);
  }
  function setVisible(device, target, visible) {
    if (!device || typeof device !== "object") return false;
    if (!device.visibility || typeof device.visibility !== "object" || Array.isArray(device.visibility)) device.visibility = {};
    const next = Boolean(visible);
    if (device.visibility[target] === next) return false;
    device.visibility[target] = next;
    return true;
  }
  function orderedDevices() {
    const rows = devices().filter((device) => device && typeof device === "object" && deviceName(device));
    const order = Array.isArray(state.model?.ui_config?.visibility?.device_order)
      ? state.model.ui_config.visibility.device_order.map(text) : [];
    const rank = new Map(order.map((name, index) => [name, index]));
    return rows.sort((a, b) => {
      const ar = rank.has(deviceName(a)) ? rank.get(deviceName(a)) : Number.MAX_SAFE_INTEGER;
      const br = rank.has(deviceName(b)) ? rank.get(deviceName(b)) : Number.MAX_SAFE_INTEGER;
      return ar - br || naturalCompare(deviceName(a), deviceName(b));
    });
  }
  function fingerprint() {
    return JSON.stringify(devices().map((device) => [
      deviceName(device),
      ...TARGETS.map((target) => visibleFor(device, target.key)),
    ]));
  }
  function isDirty() { return Boolean(state.model) && fingerprint() !== state.savedFingerprint; }
  function loadSharedSelection() {
    try {
      const payload = JSON.parse(window.localStorage.getItem(PROJECT_SELECTION_STORAGE_KEY) || "null");
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
      return {
        projectKey: text(payload.project_key),
        modelPath: text(payload.model_path),
        connectionsPath: text(payload.connections_path),
      };
    } catch (_error) {
      return {};
    }
  }
  function projectForSelection(config, selection) {
    const projects = Array.isArray(config?.projects) ? config.projects : [];
    return projects.find((project) => text(project?.key) === selection.projectKey)
      || projects.find((project) => Array.isArray(project?.device_configs) && project.device_configs.map(text).includes(selection.modelPath))
      || projects.find((project) => text(project?.key) === text(config?.active_project_key))
      || null;
  }
  async function activateSelectedTargets(config) {
    const selection = loadSharedSelection();
    const project = projectForSelection(config, selection);
    const modelPath = text(config?.model_path) || selection.modelPath;
    if (!modelPath) return config;
    if (text(config?.model_path) === modelPath && text(config?.model_hash)) return config;
    const connectionsPath = selection.connectionsPath
      || text(project?.default_patch_config)
      || text(config?.connections_path);
    const htmlDirectory = text(project?.output_html_directory);
    const svgDirectory = text(project?.output_svg_directory);
    const debugDirectory = text(project?.output_debug_directory);
    const targets = {
      model_path: modelPath,
      ...(connectionsPath ? { connections_path: connectionsPath } : {}),
      ...(htmlDirectory ? { preview_html: `${htmlDirectory}/studio_wiring_point_to_point.html` } : {}),
      ...(svgDirectory ? { preview_svg_dir: svgDirectory } : {}),
      ...(debugDirectory ? { route_debug_path: `${debugDirectory}/route-debug.json` } : {}),
    };
    const response = await fetch("/api/set-targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(targets),
    });
    const payload = await response.json();
    if (!response.ok || payload?.ok === false) throw new Error(text(payload?.error) || "Could not activate the selected device configuration.");
    return payload;
  }
  function postToShell(payload) {
    if (window.parent === window) return;
    try { window.parent.postMessage(payload, "*"); } catch (_error) { /* standalone mode */ }
  }
  function reportAutoSaveState(type = "studio-shell-autosave-state") {
    postToShell({ type, enabled: state.autoSaveEnabled, dirty: isDirty(), saving: state.saving });
  }
  function setSaveState(kind, label) {
    dom.saveState.dataset.state = kind;
    dom.saveState.textContent = label;
  }
  function updateSaveState() {
    if (state.loading) setSaveState("loading", "Loading…");
    else if (state.saving) setSaveState("saving", "Saving…");
    else if (state.conflicted) setSaveState("conflict", "Conflict — reload required");
    else if (isDirty()) setSaveState("dirty", state.autoSaveEnabled ? "Unsaved — auto save queued" : "Unsaved changes");
    else setSaveState("saved", "Saved");
    dom.save.disabled = state.loading || state.saving || state.conflicted || !isDirty();
    reportAutoSaveState();
  }
  function showStatus(message, error = false, persistent = false) {
    window.clearTimeout(statusTimer);
    dom.status.textContent = text(message);
    dom.status.classList.toggle("error", Boolean(error));
    dom.status.classList.toggle("visible", Boolean(text(message)));
    if (message && !persistent) statusTimer = window.setTimeout(() => dom.status.classList.remove("visible"), 4200);
  }
  function renderContext() {
    const title = text(state.model?.title) || "Studio Wiring";
    dom.context.textContent = state.modelPath ? `${title} · ${state.modelPath.split("/").pop()}` : "No active device configuration selected.";
  }
  function selectionNames() { return orderedDevices().map(deviceName); }
  function selectDevice(name, modifiers = {}, keepSelectedGroup = false) {
    const names = selectionNames();
    if (!names.includes(name)) return;
    if (modifiers.altKey) {
      state.selected = new Set(names);
    } else if (modifiers.shiftKey && state.anchor && names.includes(state.anchor)) {
      const start = names.indexOf(state.anchor);
      const end = names.indexOf(name);
      if (!modifiers.metaKey && !modifiers.ctrlKey) state.selected.clear();
      for (let index = Math.min(start, end); index <= Math.max(start, end); index += 1) state.selected.add(names[index]);
    } else if (modifiers.metaKey || modifiers.ctrlKey) {
      if (state.selected.has(name)) state.selected.delete(name); else state.selected.add(name);
      state.anchor = name;
    } else if (!(keepSelectedGroup && state.selected.has(name) && state.selected.size > 1)) {
      state.selected.clear();
      state.selected.add(name);
      state.anchor = name;
    }
    if (!state.anchor) state.anchor = name;
  }
  function render() {
    renderContext();
    const rows = orderedDevices();
    const valid = new Set(rows.map(deviceName));
    for (const name of Array.from(state.selected)) if (!valid.has(name)) state.selected.delete(name);
    const counts = TARGETS.map((target) => `${target.label.replace("Show in ", "")}: ${rows.filter((device) => visibleFor(device, target.key)).length}/${rows.length}`);
    dom.summary.textContent = rows.length ? `${counts.join(" · ")} · Selected: ${state.selected.size}` : "No devices in this model.";
    if (!rows.length) {
      dom.list.innerHTML = '<div class="empty">No devices to configure.</div>';
      updateSaveState();
      return;
    }
    const header = `<div class="visibility-header" aria-hidden="true"><span>Device</span>${TARGETS.map((target) => `<span>${escapeHtml(target.label)}</span>`).join("")}</div>`;
    const body = rows.map((device) => {
      const name = deviceName(device);
      const ports = Array.isArray(device.ports) ? device.ports.length : 0;
      const type = text(device.device_type) || "Device";
      const selected = state.selected.has(name);
      const toggles = TARGETS.map((target) => `<label class="toggle-cell" title="${escapeHtml(target.label)}"><input type="checkbox" data-device="${escapeHtml(name)}" data-target="${target.key}" aria-label="${escapeHtml(`${target.label}: ${name}`)}"${visibleFor(device, target.key) ? " checked" : ""}></label>`).join("");
      return `<div class="visibility-row${selected ? " selected" : ""}" data-row-device="${escapeHtml(name)}" role="option" aria-selected="${selected}"><div class="device-info"><span class="device-name">${escapeHtml(name)}</span><span class="device-meta">${escapeHtml(type)} · ${ports} ports</span></div>${toggles}</div>`;
    }).join("");
    dom.list.innerHTML = header + body;
    updateSaveState();
  }
  function markChanged(message) {
    state.conflicted = false;
    render();
    showStatus(message);
    scheduleAutoSave();
  }
  function scheduleAutoSave() {
    window.clearTimeout(state.autoSaveTimer);
    state.autoSaveTimer = 0;
    updateSaveState();
    if (!state.autoSaveEnabled || !isDirty() || state.conflicted) return;
    state.autoSaveTimer = window.setTimeout(() => { state.autoSaveTimer = 0; void saveModel(true); }, AUTO_SAVE_DELAY_MS);
  }
  async function saveModel(quiet = false) {
    if (state.savePromise) return state.savePromise;
    if (!isDirty()) return true;
    if (!state.model || !state.modelPath) return false;
    window.clearTimeout(state.autoSaveTimer);
    state.saving = true;
    updateSaveState();
    const savedFingerprint = fingerprint();
    state.savePromise = (async () => {
      try {
        const headers = { "Content-Type": "application/json" };
        if (state.modelHash) headers["X-If-Unmodified-Model-Hash"] = state.modelHash;
        const response = await fetch(SAVE_MODEL_API, { method: "POST", headers, body: JSON.stringify(state.model) });
        let payload = {};
        try { payload = await response.json(); } catch (_error) { payload = {}; }
        if (!response.ok || payload?.ok === false) {
          if (response.status === 409 || payload?.conflict) state.conflicted = true;
          throw new Error(text(payload?.error) || `Save failed (HTTP ${response.status}).`);
        }
        state.savedFingerprint = savedFingerprint;
        const configResponse = await fetch(CONFIG_API, { cache: "no-store" });
        if (configResponse.ok) {
          const config = await configResponse.json();
          state.config = config;
          state.modelHash = text(config?.model_hash) || state.modelHash;
        }
        if (!quiet) showStatus(payload?.regenerate_ok === false ? "Visibility saved; visual regeneration reported a warning." : "Visibility saved and visuals updated.", payload?.regenerate_ok === false);
        return true;
      } catch (error) {
        showStatus(text(error?.message || error || "Could not save visibility."), true, true);
        return false;
      } finally {
        state.saving = false;
        state.savePromise = null;
        updateSaveState();
        if (isDirty() && state.autoSaveEnabled && !state.conflicted) scheduleAutoSave();
      }
    })();
    return state.savePromise;
  }
  async function loadModel(force = false) {
    if (isDirty() && !force && !window.confirm("Reload and discard unsaved visibility changes?")) return false;
    state.loading = true;
    state.conflicted = false;
    updateSaveState();
    try {
      const configResponse = await fetch(CONFIG_API, { cache: "no-store" });
      let config = await configResponse.json();
      if (!configResponse.ok || config?.ok === false) throw new Error(text(config?.error) || "Could not load server configuration.");
      config = await activateSelectedTargets(config);
      state.config = config;
      state.modelPath = text(config?.model_path);
      state.modelHash = text(config?.model_hash);
      if (!state.modelPath) throw new Error("Select a project and device configuration in the Wiring Matrix first.");
      const modelUrl = new URL(`/${state.modelPath.replace(/^\/+/, "")}`, window.location.origin);
      modelUrl.searchParams.set("_ts", String(Date.now()));
      const modelResponse = await fetch(modelUrl.toString(), { cache: "no-store" });
      const model = await modelResponse.json();
      if (!modelResponse.ok || !model || typeof model !== "object") throw new Error(`Could not load ${state.modelPath}.`);
      state.model = model;
      state.savedFingerprint = fingerprint();
      state.selected.clear();
      state.anchor = "";
      showStatus(`Loaded ${devices().length} devices.`);
      return true;
    } catch (error) {
      state.model = null;
      state.modelPath = "";
      state.savedFingerprint = "";
      showStatus(text(error?.message || error || "Could not load visibility data."), true, true);
      return false;
    } finally {
      state.loading = false;
      render();
    }
  }
  function applyAll(value) {
    let changed = 0;
    for (const device of devices()) for (const target of TARGETS) if (setVisible(device, target.key, value)) changed += 1;
    state.selected = new Set(selectionNames());
    markChanged(`${value ? "Showing" : "Hiding"} all devices everywhere (${changed} changes).`);
  }
  function debugReport() {
    return JSON.stringify({ page: "Device Visibility", version: 1, model_path: state.modelPath, model_hash: state.modelHash, device_count: devices().length, selected_count: state.selected.size, dirty: isDirty(), conflicted: state.conflicted, auto_save: state.autoSaveEnabled }, null, 2);
  }
  async function copyDebugReport() {
    try { await navigator.clipboard.writeText(debugReport()); showStatus("Visibility debug report copied."); }
    catch (_error) { showStatus("Could not copy the debug report.", true); }
  }

  dom.list.addEventListener("click", (event) => {
    const input = event.target instanceof Element ? event.target.closest("input[data-device][data-target]") : null;
    if (input instanceof HTMLInputElement) {
      event.preventDefault();
      const name = text(input.dataset.device);
      const target = text(input.dataset.target);
      selectDevice(name, event, true);
      const affected = event.altKey ? selectionNames() : (state.selected.size ? Array.from(state.selected) : [name]);
      const next = !visibleFor(devices().find((device) => deviceName(device) === name), target);
      let changed = 0;
      for (const candidate of devices()) if (affected.includes(deviceName(candidate)) && setVisible(candidate, target, next)) changed += 1;
      markChanged(`${next ? "Showing" : "Hiding"} ${changed} device${changed === 1 ? "" : "s"} in ${TARGETS.find((item) => item.key === target)?.label.replace("Show in ", "") || target}.`);
      return;
    }
    const row = event.target instanceof Element ? event.target.closest("[data-row-device]") : null;
    if (!(row instanceof HTMLElement)) return;
    selectDevice(text(row.dataset.rowDevice), event, false);
    render();
  });
  dom.showAll.addEventListener("click", () => applyAll(true));
  dom.hideAll.addEventListener("click", () => applyAll(false));
  dom.reload.addEventListener("click", () => void loadModel(false));
  dom.save.addEventListener("click", () => void saveModel(false));
  window.addEventListener("message", (event) => {
    const data = event?.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "studio-theme-set") {
      const mode = text(data.mode).toLowerCase() === "dark" ? "dark" : "light";
      document.body.classList.toggle("theme-dark", mode === "dark");
    } else if (data.type === "studio-shell-autosave-set") {
      state.autoSaveEnabled = Boolean(data.enabled);
      reportAutoSaveState("studio-shell-autosave-changed");
      scheduleAutoSave();
    } else if (data.type === "studio-shell-autosave-request") {
      reportAutoSaveState();
    } else if (data.type === "studio-shell-autosave-flush") {
      const requestId = text(data.request_id);
      void (isDirty() ? saveModel(true) : Promise.resolve(true)).then((ok) => postToShell({ type: "studio-shell-autosave-flushed", request_id: requestId, ok: Boolean(ok) }));
    } else if (data.type === "studio-shell-copy-debug-report") {
      void copyDebugReport();
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== PROJECT_SELECTION_STORAGE_KEY || isDirty()) return;
    void loadModel(true);
  });
  window.addEventListener("beforeunload", (event) => {
    if (!isDirty() || state.autoSaveEnabled) return;
    event.preventDefault();
    event.returnValue = "";
  });

  postToShell({ type: "studio-theme-request" });
  reportAutoSaveState();
  void loadModel(true);
})();
