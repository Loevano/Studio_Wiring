(function routingMatrixBootstrap() {
  "use strict";

  const CONFIG_API = "/api/config";
  const ROUTING_API = "/api/routing";
  const SAVE_ROUTING_API = "/api/save-routing";
  const AUTO_SAVE_DELAY_MS = 700;
  const PROJECT_SELECTION_STORAGE_KEY = "studioWiringProjectSelectionV1";
  const QUERY = new URLSearchParams(window.location.search || "");

  const dom = {
    app: document.getElementById("routingMatrixApp"),
    context: document.getElementById("routingContext"),
    mode: document.getElementById("routeModeSelect"),
    span: document.getElementById("routeSpanSelect"),
    undo: document.getElementById("undoButton"),
    redo: document.getElementById("redoButton"),
    reload: document.getElementById("reloadButton"),
    save: document.getElementById("saveButton"),
    saveState: document.getElementById("saveState"),
    summary: document.getElementById("matrixSummary"),
    empty: document.getElementById("routingEmptyState"),
    scroller: document.getElementById("matrixScroller"),
    table: document.getElementById("routingMatrixTable"),
    head: document.getElementById("matrixHead"),
    body: document.getElementById("matrixBody"),
    status: document.getElementById("routingStatus"),
  };

  const state = {
    config: null,
    projects: [],
    projectKey: "",
    modelPath: "",
    routingPath: "",
    modelHash: "",
    routingHash: "",
    endpoints: [],
    sources: [],
    destinations: [],
    routes: [],
    savedFingerprint: "",
    history: [],
    historyIndex: -1,
    loading: false,
    saving: false,
    savePromise: null,
    loadSequence: 0,
    autoSaveEnabled: false,
    autoSaveTimer: 0,
    conflicted: false,
  };

  let statusTimer = 0;

  function text(value) {
    return String(value == null ? "" : value).trim();
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function naturalCompare(a, b) {
    return String(a || "").localeCompare(String(b || ""), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }

  function basename(pathValue) {
    const parts = text(pathValue).split("/").filter(Boolean);
    return parts[parts.length - 1] || text(pathValue);
  }

  function endpointKey(device, port) {
    return `${text(device)}::${text(port)}`;
  }

  function routeDestinationKey(route) {
    return endpointKey(route.dest_device, route.dest_port);
  }

  function routePairKey(route) {
    return `${endpointKey(route.source_device, route.source_port)}=>${routeDestinationKey(route)}`;
  }

  function canonicalRoutes(routes) {
    return (Array.isArray(routes) ? routes : [])
      .map((row) => ({
        source_device: text(row?.source_device),
        source_port: text(row?.source_port),
        dest_device: text(row?.dest_device),
        dest_port: text(row?.dest_port),
        ...(text(row?.notes) ? { notes: text(row.notes) } : {}),
      }))
      .filter((row) => row.source_device && row.source_port && row.dest_device && row.dest_port)
      .sort((a, b) => naturalCompare(routeDestinationKey(a), routeDestinationKey(b))
        || naturalCompare(endpointKey(a.source_device, a.source_port), endpointKey(b.source_device, b.source_port))
        || naturalCompare(a.notes || "", b.notes || ""));
  }

  function routeFingerprint(routes = state.routes) {
    return JSON.stringify(canonicalRoutes(routes));
  }

  function isDirty() {
    return routeFingerprint() !== state.savedFingerprint;
  }

  function postToShell(payload) {
    if (window.parent === window) return;
    try {
      window.parent.postMessage(payload, "*");
    } catch (_error) {
      // The page remains usable when it is not embedded in the shell.
    }
  }

  function reportAutoSaveState(messageType = "studio-shell-autosave-state") {
    postToShell({
      type: messageType,
      enabled: Boolean(state.autoSaveEnabled),
      dirty: isDirty(),
      saving: Boolean(state.saving),
    });
  }

  function setSaveState(kind, label) {
    if (!(dom.saveState instanceof HTMLElement)) return;
    dom.saveState.dataset.state = kind;
    dom.saveState.textContent = label;
  }

  function updateSaveState() {
    if (state.loading) {
      setSaveState("loading", "Loading…");
    } else if (state.saving) {
      setSaveState("saving", "Saving…");
    } else if (state.conflicted) {
      setSaveState("conflict", "Conflict — reload required");
    } else if (isDirty()) {
      setSaveState("dirty", state.autoSaveEnabled ? "Unsaved — auto save queued" : "Unsaved changes");
    } else {
      setSaveState("saved", state.routingPath ? "Saved" : "No routing file");
    }
    if (dom.save instanceof HTMLButtonElement) {
      dom.save.disabled = state.loading || state.saving || !state.routingPath || !isDirty();
    }
    if (dom.undo instanceof HTMLButtonElement) dom.undo.disabled = state.historyIndex <= 0 || state.loading || state.saving;
    if (dom.redo instanceof HTMLButtonElement) dom.redo.disabled = state.historyIndex >= state.history.length - 1 || state.loading || state.saving;
    reportAutoSaveState();
  }

  function showStatus(message, error = false, persistent = false) {
    if (!(dom.status instanceof HTMLElement)) return;
    window.clearTimeout(statusTimer);
    dom.status.textContent = text(message);
    dom.status.classList.toggle("error", Boolean(error));
    dom.status.classList.toggle("visible", Boolean(text(message)));
    if (message && !persistent) {
      statusTimer = window.setTimeout(() => dom.status?.classList.remove("visible"), 4200);
    }
  }

  function projectByKey(key) {
    return state.projects.find((project) => text(project?.key) === text(key)) || null;
  }

  function modelPathsForProject(project) {
    return Array.isArray(project?.device_configs) ? project.device_configs.map(text).filter(Boolean) : [];
  }

  function routingPathsForModel(project, modelPath) {
    const mapping = project?.device_routing_map;
    if (mapping && typeof mapping === "object" && !Array.isArray(mapping)) {
      const direct = mapping[modelPath];
      if (Array.isArray(direct) && direct.length) return direct.map(text).filter(Boolean);
    }
    return Array.isArray(project?.routing_configs) ? project.routing_configs.map(text).filter(Boolean) : [];
  }

  function preferredValue(values, requested, fallback) {
    if (values.includes(text(requested))) return text(requested);
    if (values.includes(text(fallback))) return text(fallback);
    return values[0] || "";
  }

  function renderContext() {
    const project = projectByKey(state.projectKey);
    if (!(dom.context instanceof HTMLElement)) return;
    if (!project || !state.modelPath) {
      dom.context.textContent = "No Wiring Matrix project and device configuration selected.";
      return;
    }
    const projectLabel = text(project.name) || text(project.key);
    const routeLabel = state.routingPath ? basename(state.routingPath) : "no routing configuration";
    dom.context.textContent = `${projectLabel} · ${basename(state.modelPath)} · ${routeLabel}`;
  }

  function loadSharedWiringSelection() {
    try {
      const payload = JSON.parse(window.localStorage.getItem(PROJECT_SELECTION_STORAGE_KEY) || "null");
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
      return {
        project_key: text(payload.project_key),
        model_path: text(payload.model_path),
        connections_path: text(payload.connections_path),
      };
    } catch (_error) {
      return {};
    }
  }

  function selectInitialTargets(config) {
    state.projects = Array.isArray(config?.projects) ? config.projects : [];
    const sharedSelection = loadSharedWiringSelection();
    const project = projectByKey(sharedSelection.project_key)
      || projectByKey(text(QUERY.get("project_key")))
      || projectByKey(config?.active_project_key)
      || state.projects[0]
      || null;
    state.projectKey = text(project?.key);
    const models = modelPathsForProject(project);
    state.modelPath = preferredValue(
      models,
      sharedSelection.model_path || QUERY.get("model_path") || QUERY.get("model"),
      project?.default_device_config || config?.model_path,
    );
    const routePaths = routingPathsForModel(project, state.modelPath);
    state.routingPath = preferredValue(
      routePaths,
      QUERY.get("routing_path"),
      project?.default_routing_config || config?.routing_path,
    );
  }

  function updateUrl() {
    const url = new URL(window.location.href);
    for (const [key, value] of [
      ["project_key", state.projectKey],
      ["model_path", state.modelPath],
      ["routing_path", state.routingPath],
    ]) {
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function normalizeDirection(value) {
    const direction = text(value).toLowerCase();
    return ["in", "out", "io"].includes(direction) ? direction : "";
  }

  function normalizeRoles(raw) {
    const values = Array.isArray(raw) ? raw : [];
    return values.map((value) => text(value).toLowerCase()).filter((value) => value === "source" || value === "destination");
  }

  function normalizeGroup(raw, fallback = "") {
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      return {
        name: text(raw.name) || fallback,
        index: Number.isFinite(Number(raw.index)) ? Number(raw.index) : null,
        size: Number.isFinite(Number(raw.size)) ? Number(raw.size) : null,
      };
    }
    return { name: text(raw) || fallback, index: null, size: null };
  }

  function normalizeEndpoint(raw, order) {
    const compositeId = text(raw?.id);
    const separatorIndex = compositeId.indexOf("::");
    const device = text(raw?.device || raw?.device_name || (separatorIndex > 0 ? compositeId.slice(0, separatorIndex) : ""));
    const port = text(raw?.port || raw?.port_id || (separatorIndex > 0 ? compositeId.slice(separatorIndex + 2) : compositeId));
    if (!device || !port) return null;
    const direction = normalizeDirection(raw?.direction);
    const explicitRoles = normalizeRoles(raw?.routing_roles || raw?.roles);
    // Direction controls which topology is valid, not which axis can display an
    // endpoint. An input can source a same-device crosspoint and an output can
    // be its destination, so direction-only server endpoints belong on both axes.
    const roles = explicitRoles.length || !direction
      ? explicitRoles
      : ["source", "destination"];
    const name = text(raw?.name || raw?.port_name || port) || port;
    const group = normalizeGroup(raw?.group, "");
    const rawChannel = raw?.channel ?? raw?.group_index ?? group.index;
    const channel = rawChannel == null || text(rawChannel) === ""
      ? null
      : (Number.isFinite(Number(rawChannel)) ? Number(rawChannel) : text(rawChannel));
    return {
      id: compositeId || endpointKey(device, port),
      device,
      port,
      name,
      direction,
      roles,
      transport: text(raw?.transport),
      hardware: text(raw?.hardware),
      connection2: text(raw?.connection_2),
      group,
      channel,
      order: Number.isFinite(Number(raw?.order)) ? Number(raw.order) : order,
      enabled: raw?.enabled !== false && raw?.disabled !== true,
    };
  }

  function compareEndpoints(a, b) {
    return naturalCompare(a.device, b.device)
      || naturalCompare(a.group.name, b.group.name)
      || ((a.channel ?? Number.MAX_SAFE_INTEGER) - (b.channel ?? Number.MAX_SAFE_INTEGER))
      || (a.order - b.order)
      || naturalCompare(a.name, b.name);
  }

  function endpointHasRole(endpoint, role) {
    return endpoint.enabled && endpoint.roles.includes(role);
  }

  function canRoute(source, destination) {
    if (!source || !destination || source.id === destination.id) return false;
    if (!endpointHasRole(source, "source") || !endpointHasRole(destination, "destination")) return false;
    if (source.device === destination.device) {
      return (source.direction === "in" || source.direction === "io")
        && (destination.direction === "out" || destination.direction === "io");
    }
    return (source.direction === "out" || source.direction === "io")
      && (destination.direction === "in" || destination.direction === "io");
  }

  function normalizeRoute(raw) {
    const source = raw?.source && typeof raw.source === "object" ? raw.source : {};
    const destination = raw?.destination && typeof raw.destination === "object" ? raw.destination : {};
    const row = {
      source_device: text(raw?.source_device || source.device),
      source_port: text(raw?.source_port || source.port || source.endpoint_id),
      dest_device: text(raw?.dest_device || destination.device),
      dest_port: text(raw?.dest_port || destination.port || destination.endpoint_id),
    };
    if (text(raw?.notes)) row.notes = text(raw.notes);
    return row.source_device && row.source_port && row.dest_device && row.dest_port ? row : null;
  }

  function extractDocument(payload) {
    let documentPayload = payload?.document;
    if (!documentPayload && payload?.routes && !Array.isArray(payload.routes)) documentPayload = payload.routes;
    if (!documentPayload && Array.isArray(payload?.routes)) documentPayload = { version: 1, routes: payload.routes };
    if (!documentPayload || typeof documentPayload !== "object") documentPayload = { version: 1, routes: [] };
    const rows = Array.isArray(documentPayload.routes) ? documentPayload.routes : [];
    return { version: 1, routes: rows.map(normalizeRoute).filter(Boolean) };
  }

  function setLoadedData(payload) {
    const endpointRows = Array.isArray(payload?.endpoints) ? payload.endpoints : [];
    state.endpoints = endpointRows.map(normalizeEndpoint).filter(Boolean).sort(compareEndpoints);
    state.sources = state.endpoints.filter((endpoint) => endpointHasRole(endpoint, "source"));
    state.destinations = state.endpoints.filter((endpoint) => endpointHasRole(endpoint, "destination"));
    state.routes = canonicalRoutes(extractDocument(payload).routes);
    state.modelPath = text(payload?.model_path) || state.modelPath;
    state.routingPath = text(payload?.routing_path) || state.routingPath;
    state.modelHash = text(payload?.model_hash);
    state.routingHash = text(payload?.routing_hash || payload?.routes_hash || payload?.hash);
    state.savedFingerprint = routeFingerprint();
    state.history = [state.savedFingerprint];
    state.historyIndex = 0;
    state.conflicted = false;
  }

  function endpointSearchText(endpoint) {
    return `${endpoint.device} ${endpoint.name} ${endpoint.port} ${endpoint.transport} ${endpoint.hardware} ${endpoint.connection2} ${endpoint.group.name} ${endpoint.channel ?? ""}`.toLowerCase();
  }

  function visibleEndpoints(endpoints, filterValue) {
    const tokens = text(filterValue).toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return endpoints;
    return endpoints.filter((endpoint) => {
      const haystack = endpointSearchText(endpoint);
      return tokens.every((token) => haystack.includes(token));
    });
  }

  function endpointLabel(endpoint) {
    const channel = endpoint.channel == null ? "" : ` ch ${endpoint.channel}`;
    const transport = endpoint.transport ? `, ${endpoint.transport}` : "";
    const details = [endpoint.hardware, endpoint.connection2].filter(Boolean).join(", ");
    return `${endpoint.device}, ${endpoint.name}${channel}${transport}${details ? `, ${details}` : ""}`;
  }

  function endpointLabelHtml(endpoint, vertical = false) {
    const channel = endpoint.channel == null ? "" : ` ${endpoint.channel}`;
    const portLabel = endpoint.name + (channel && !endpoint.name.includes(String(endpoint.channel)) ? channel : "");
    const details = [endpoint.transport, endpoint.hardware, endpoint.connection2].filter(Boolean).join(" · ");
    return `<span class="device">${escapeHtml(endpoint.device)}</span>`
      + `<span class="port">${escapeHtml(portLabel)}</span>`
      + (details ? `<span class="transport">${escapeHtml(details)}</span>` : "")
      + (vertical ? "" : `<span class="sr-only">, ${escapeHtml(endpoint.direction)}</span>`);
  }

  function currentRouteByDestination() {
    const map = new Map();
    for (const route of state.routes) map.set(routeDestinationKey(route), route);
    return map;
  }

  function renderMatrix() {
    if (!(dom.head instanceof HTMLElement) || !(dom.body instanceof HTMLElement)) return;
    const sources = visibleEndpoints(state.sources, dom.sourceFilter?.value);
    const destinations = visibleEndpoints(state.destinations, dom.destinationFilter?.value);
    const routeByDestination = currentRouteByDestination();

    if (dom.summary instanceof HTMLElement) {
      dom.summary.textContent = `${state.routes.length} route${state.routes.length === 1 ? "" : "s"} · `
        + `${sources.length}/${state.sources.length} sources · ${destinations.length}/${state.destinations.length} destinations`;
    }

    if (!state.routingPath || !state.endpoints.length || !sources.length || !destinations.length) {
      let message = "";
      if (!state.projects.length) message = "No studio projects were found. Create or select a project before making logical routes.";
      else if (!state.routingPath) message = "This device model has no routing configuration. Add a project routing file and map it to the device model.";
      else if (!state.endpoints.length) message = "This device model has no routing-capable audio endpoints. Add per-device routing_ports with explicit source/destination roles.";
      else message = "No routing endpoints match the current source and destination filters.";
      if (dom.empty instanceof HTMLElement) {
        dom.empty.hidden = false;
        dom.empty.textContent = message;
      }
      if (dom.scroller instanceof HTMLElement) dom.scroller.hidden = true;
      dom.head.innerHTML = "";
      dom.body.innerHTML = "";
      updateSaveState();
      return;
    }

    if (dom.empty instanceof HTMLElement) dom.empty.hidden = true;
    if (dom.scroller instanceof HTMLElement) dom.scroller.hidden = false;
    dom.head.innerHTML = `<tr><th class="corner" scope="col"><span class="axis-cue">Destinations →</span><span class="axis-cue">Sources ↓</span></th>`
      + destinations.map((destination) => (
        `<th class="destination-head" scope="col" title="${escapeHtml(endpointLabel(destination))}">`
        + `<span class="destination-label">${endpointLabelHtml(destination, true)}</span></th>`
      )).join("")
      + "</tr>";

    const rows = [];
    for (const source of sources) {
      const cells = [];
      for (const destination of destinations) {
        const valid = canRoute(source, destination);
        const existing = routeByDestination.get(endpointKey(destination.device, destination.port));
        const active = Boolean(existing
          && endpointKey(existing.source_device, existing.source_port) === endpointKey(source.device, source.port));
        const occupiedByOther = Boolean(existing && !active);
        const sourceLabel = endpointLabel(source);
        const destinationLabel = endpointLabel(destination);
        const actionLabel = active ? "Disconnect" : occupiedByOther ? "Replace route to" : "Connect";
        const title = valid
          ? `${actionLabel} ${sourceLabel} → ${destinationLabel}`
          : `Not a valid route: ${source.direction || "unknown"} → ${destination.direction || "unknown"}`;
        cells.push(`<td class="route-cell${valid ? "" : " invalid"}" data-source-id="${escapeHtml(source.id)}" data-destination-id="${escapeHtml(destination.id)}">`
          + `<button type="button" class="route-cell-button${occupiedByOther && valid ? " replaced-destination" : ""}"`
          + ` data-route-source="${escapeHtml(source.id)}" data-route-destination="${escapeHtml(destination.id)}"`
          + ` aria-label="${escapeHtml(title)}" aria-pressed="${active ? "true" : "false"}" title="${escapeHtml(title)}"`
          + `${valid ? "" : " disabled"}></button></td>`);
      }
      rows.push(`<tr><th class="source-head" scope="row" title="${escapeHtml(endpointLabel(source))}">`
        + `<span class="source-label">${endpointLabelHtml(source)}</span></th>${cells.join("")}</tr>`);
    }
    dom.body.innerHTML = rows.join("");
    updateSaveState();
  }

  function groupIdentity(endpoint) {
    if (!endpoint?.group?.name) return `endpoint:${endpoint?.id || ""}`;
    return `${endpoint.device}::${endpoint.direction}::${endpoint.group.name}`;
  }

  function bundleMembers(endpoint, candidates) {
    const identity = groupIdentity(endpoint);
    return candidates.filter((candidate) => groupIdentity(candidate) === identity).sort(compareEndpoints);
  }

  function rangePairs(source, destination, spanValue) {
    const sourceBundle = bundleMembers(source, state.sources);
    const destinationBundle = bundleMembers(destination, state.destinations);
    const sourceStart = sourceBundle.findIndex((endpoint) => endpoint.id === source.id);
    const destinationStart = destinationBundle.findIndex((endpoint) => endpoint.id === destination.id);
    if (sourceStart < 0 || destinationStart < 0) throw new Error("The selected routing endpoints are no longer available.");
    const sourceRemaining = sourceBundle.length - sourceStart;
    const destinationRemaining = destinationBundle.length - destinationStart;
    let count;
    if (spanValue === "all") {
      if (sourceRemaining !== destinationRemaining) {
        throw new Error(`Bundle sizes do not align from this cell (${sourceRemaining} sources, ${destinationRemaining} destinations).`);
      }
      count = sourceRemaining;
    } else {
      count = Math.max(1, Number.parseInt(spanValue, 10) || 1);
      if (sourceRemaining < count || destinationRemaining < count) {
        throw new Error(`A ${count}-channel route does not fit from this cell.`);
      }
    }
    const pairs = [];
    for (let index = 0; index < count; index += 1) {
      const pair = { source: sourceBundle[sourceStart + index], destination: destinationBundle[destinationStart + index] };
      if (!canRoute(pair.source, pair.destination)) {
        throw new Error(`Channel ${index + 1} is not a valid route: ${endpointLabel(pair.source)} → ${endpointLabel(pair.destination)}.`);
      }
      pairs.push(pair);
    }
    return pairs;
  }

  function pushHistory(nextRoutes) {
    const snapshot = routeFingerprint(nextRoutes);
    if (snapshot === state.history[state.historyIndex]) return false;
    state.history = state.history.slice(0, state.historyIndex + 1);
    state.history.push(snapshot);
    state.historyIndex = state.history.length - 1;
    state.routes = JSON.parse(snapshot);
    state.conflicted = false;
    renderMatrix();
    scheduleAutoSave();
    return true;
  }

  function applyRange(source, destination) {
    const mode = text(dom.mode?.value) || "add";
    const pairs = rangePairs(source, destination, text(dom.span?.value) || "1");
    const byDestination = currentRouteByDestination();
    let changed = 0;
    for (const pair of pairs) {
      const destinationKey = endpointKey(pair.destination.device, pair.destination.port);
      const existing = byDestination.get(destinationKey);
      const exact = Boolean(existing
        && endpointKey(existing.source_device, existing.source_port) === endpointKey(pair.source.device, pair.source.port));
      if (mode === "remove" || (mode === "toggle" && exact)) {
        if (exact) {
          byDestination.delete(destinationKey);
          changed += 1;
        }
        continue;
      }
      const next = {
        source_device: pair.source.device,
        source_port: pair.source.port,
        dest_device: pair.destination.device,
        dest_port: pair.destination.port,
      };
      if (!exact || text(existing?.notes)) {
        byDestination.set(destinationKey, next);
        changed += 1;
      }
    }
    if (!changed) {
      showStatus(mode === "remove" ? "None of the selected source/destination pairs is connected." : "The selected routes are already set.");
      return;
    }
    const didPush = pushHistory(Array.from(byDestination.values()));
    if (didPush) {
      const verb = mode === "remove" ? "Removed" : mode === "toggle" ? "Updated" : "Connected";
      showStatus(`${verb} ${changed} channel${changed === 1 ? "" : "s"}.`);
    }
  }

  function scheduleAutoSave() {
    window.clearTimeout(state.autoSaveTimer);
    state.autoSaveTimer = 0;
    updateSaveState();
    if (!state.autoSaveEnabled || !isDirty() || state.conflicted) return;
    state.autoSaveTimer = window.setTimeout(() => {
      state.autoSaveTimer = 0;
      void saveRoutes({ quiet: true });
    }, AUTO_SAVE_DELAY_MS);
  }

  function responseError(payload, fallback) {
    const issues = Array.isArray(payload?.validation_issues)
      ? payload.validation_issues.map((issue) => text(issue?.message || issue)).filter(Boolean)
      : [];
    return issues.length ? `${text(payload?.error) || fallback}: ${issues.join("; ")}` : text(payload?.error) || fallback;
  }

  async function saveRoutes(options = {}) {
    if (state.savePromise) return state.savePromise;
    if (!isDirty()) return true;
    if (!state.projectKey || !state.modelPath || !state.routingPath) {
      showStatus("Select a project, device model, and routing configuration before saving.", true, true);
      return false;
    }
    window.clearTimeout(state.autoSaveTimer);
    state.autoSaveTimer = 0;
    state.saving = true;
    updateSaveState();
    const fingerprintBeingSaved = routeFingerprint();
    const documentPayload = { version: 1, routes: canonicalRoutes(state.routes) };

    state.savePromise = (async () => {
      try {
        const response = await fetch(SAVE_ROUTING_API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_key: state.projectKey,
            model_path: state.modelPath,
            routing_path: state.routingPath,
            routes: documentPayload,
            expected_hash: state.routingHash,
          }),
        });
        let payload = {};
        try { payload = await response.json(); } catch (_error) { payload = {}; }
        if (!response.ok || payload?.ok === false) {
          if (response.status === 409 || payload?.conflict) {
            state.conflicted = true;
            throw new Error(responseError(payload, "Routing file changed on disk. Reload before saving again."));
          }
          throw new Error(responseError(payload, `Save failed (HTTP ${response.status}).`));
        }
        state.routingHash = text(payload?.saved?.hash || payload?.routing_hash || payload?.hash) || state.routingHash;
        state.savedFingerprint = fingerprintBeingSaved;
        state.conflicted = false;
        if (!options.quiet) showStatus(`Saved ${documentPayload.routes.length} route${documentPayload.routes.length === 1 ? "" : "s"}.`);
        return true;
      } catch (error) {
        showStatus(text(error?.message || error || "Could not save routes."), true, true);
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

  async function confirmDiscardOrSave(forceDiscard = false) {
    if (!isDirty()) return true;
    if (forceDiscard) return window.confirm("Reload and discard unsaved logical routing changes?");
    if (state.autoSaveEnabled) return saveRoutes({ quiet: true });
    return window.confirm("Discard unsaved logical routing changes?");
  }

  async function loadRouting() {
    state.loadSequence += 1;
    const sequence = state.loadSequence;
    state.loading = true;
    state.conflicted = false;
    renderContext();
    updateSaveState();
    if (!state.projectKey || !state.modelPath || !state.routingPath) {
      state.endpoints = [];
      state.sources = [];
      state.destinations = [];
      state.routes = [];
      state.savedFingerprint = "[]";
      state.history = ["[]"];
      state.historyIndex = 0;
      state.loading = false;
      renderContext();
      renderMatrix();
      return;
    }
    try {
      const url = new URL(ROUTING_API, window.location.origin);
      url.searchParams.set("project_key", state.projectKey);
      url.searchParams.set("model_path", state.modelPath);
      url.searchParams.set("routing_path", state.routingPath);
      const response = await fetch(url.toString(), { cache: "no-store" });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (!response.ok || payload?.ok === false) throw new Error(responseError(payload, `Routing load failed (HTTP ${response.status}).`));
      if (sequence !== state.loadSequence) return;
      setLoadedData(payload);
      updateUrl();
      showStatus(`Loaded ${state.routes.length} route${state.routes.length === 1 ? "" : "s"}.`);
    } catch (error) {
      if (sequence !== state.loadSequence) return;
      state.endpoints = [];
      state.sources = [];
      state.destinations = [];
      state.routes = [];
      state.savedFingerprint = "[]";
      state.history = ["[]"];
      state.historyIndex = 0;
      showStatus(text(error?.message || error || "Could not load routing data."), true, true);
    } finally {
      if (sequence === state.loadSequence) {
        state.loading = false;
        renderContext();
        renderMatrix();
      }
    }
  }

  function restoreHistory(index) {
    if (index < 0 || index >= state.history.length || index === state.historyIndex) return;
    state.historyIndex = index;
    state.routes = JSON.parse(state.history[index]);
    state.conflicted = false;
    renderMatrix();
    scheduleAutoSave();
  }

  function applyTheme(mode, notify = false) {
    const normalized = text(mode).toLowerCase() === "dark" ? "dark" : "light";
    document.body.classList.toggle("theme-dark", normalized === "dark");
    document.body.classList.toggle("theme-light", normalized !== "dark");
    if (notify) postToShell({ type: "studio-theme-changed", mode: normalized });
  }

  function debugReport() {
    return JSON.stringify({
      page: "Routing Matrix",
      version: 1,
      project_key: state.projectKey,
      model_path: state.modelPath,
      routing_path: state.routingPath,
      model_hash: state.modelHash,
      routing_hash: state.routingHash,
      endpoint_count: state.endpoints.length,
      source_count: state.sources.length,
      destination_count: state.destinations.length,
      route_count: state.routes.length,
      dirty: isDirty(),
      conflicted: state.conflicted,
      auto_save: state.autoSaveEnabled,
    }, null, 2);
  }

  async function copyDebugReport() {
    try {
      await navigator.clipboard.writeText(debugReport());
      showStatus("Routing Matrix debug report copied.");
    } catch (_error) {
      showStatus("Could not copy the debug report. Clipboard permission may be blocked.", true);
    }
  }

  function bindEvents() {
    dom.body?.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-route-source][data-route-destination]") : null;
      if (!(button instanceof HTMLButtonElement) || button.disabled || state.loading || state.saving) return;
      const source = state.sources.find((endpoint) => endpoint.id === text(button.dataset.routeSource));
      const destination = state.destinations.find((endpoint) => endpoint.id === text(button.dataset.routeDestination));
      try {
        applyRange(source, destination);
      } catch (error) {
        showStatus(text(error?.message || error || "The route could not be changed."), true);
      }
    });
    dom.undo?.addEventListener("click", () => restoreHistory(state.historyIndex - 1));
    dom.redo?.addEventListener("click", () => restoreHistory(state.historyIndex + 1));
    dom.save?.addEventListener("click", () => void saveRoutes());
    dom.reload?.addEventListener("click", async () => {
      if (!(await confirmDiscardOrSave(state.conflicted))) return;
      await loadRouting();
    });
    window.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "s") {
        event.preventDefault();
        void saveRoutes();
      } else if (key === "z" && !event.shiftKey) {
        event.preventDefault();
        restoreHistory(state.historyIndex - 1);
      } else if (key === "z" && event.shiftKey) {
        event.preventDefault();
        restoreHistory(state.historyIndex + 1);
      }
    });
    window.addEventListener("beforeunload", (event) => {
      if (!isDirty() || state.autoSaveEnabled) return;
      event.preventDefault();
      event.returnValue = "";
    });
    window.addEventListener("message", (event) => {
      const data = event?.data;
      if (!data || typeof data !== "object") return;
      if (data.type === "studio-theme-set") {
        applyTheme(data.mode);
      } else if (data.type === "studio-shell-autosave-set") {
        state.autoSaveEnabled = Boolean(data.enabled);
        reportAutoSaveState("studio-shell-autosave-changed");
        scheduleAutoSave();
      } else if (data.type === "studio-shell-autosave-request") {
        reportAutoSaveState();
      } else if (data.type === "studio-shell-autosave-flush") {
        window.clearTimeout(state.autoSaveTimer);
        state.autoSaveTimer = 0;
        void saveRoutes({ quiet: true }).then((ok) => {
          postToShell({
            type: "studio-shell-autosave-flushed",
            request_id: text(data.request_id),
            ok: Boolean(ok),
          });
        });
      } else if (data.type === "studio-shell-copy-debug-report") {
        void copyDebugReport();
      }
    });
    window.addEventListener("storage", (event) => {
      if (event.key !== PROJECT_SELECTION_STORAGE_KEY || !state.config) return;
      const previous = `${state.projectKey}\n${state.modelPath}\n${state.routingPath}`;
      selectInitialTargets(state.config);
      renderContext();
      const next = `${state.projectKey}\n${state.modelPath}\n${state.routingPath}`;
      if (next !== previous && !isDirty()) void loadRouting();
    });
  }

  async function initialize() {
    bindEvents();
    applyTheme(QUERY.get("theme") || "light");
    postToShell({ type: "studio-theme-request" });
    reportAutoSaveState();
    state.loading = true;
    updateSaveState();
    try {
      const response = await fetch(CONFIG_API, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload?.ok === false) throw new Error(responseError(payload, `Configuration load failed (HTTP ${response.status}).`));
      state.config = payload;
      selectInitialTargets(payload);
      renderContext();
      await loadRouting();
    } catch (error) {
      state.loading = false;
      state.projects = [];
      renderContext();
      renderMatrix();
      showStatus(text(error?.message || error || "Could not load project configuration."), true, true);
      setSaveState("error", "Load failed");
    }
  }

  void initialize();
})();
