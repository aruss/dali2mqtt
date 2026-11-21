// Bus capture display. Capture belongs to the server; these controls affect this browser only.
const MONITOR_HISTORY_LIMIT = 10000;
const MONITOR_PAGE_SIZE = 100;
const MONITOR_POLL_MS = 1000;
const MONITOR_COLUMNS = ["Line #", "Type", "Hex Data", "Address", "Command", "Time", "Date", "Comment"];
let monOn = false;
let monitorState = null;
let monitorEpoch = 0;

function toggleMonitor() {
  if (monOn) { stopMonitor(); } else { startMonitor(); }
}

function monitorNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) { node.textContent = text; }
  if (className) { node.className = className; }
  return node;
}

function startMonitor() {
  stopMonitor();
  monOn = true;
  const state = {
    epoch: ++monitorEpoch, records: [], columns: new WeakMap(), cursor: 0, captureId: null,
    floor: 0, page: 0, selected: null, following: true, paused: false, loading: false,
    timer: null, controller: null, observer: null, expanded: new Set(), matches: [],
    filtered: [], visible: [], notices: new Set(), requestError: null, metadata: {},
    view: "reports", findHex: "", findAddress: "", findCommand: "",
    classFilter: "", addressFilter: "", hideQueries: false, errorsOnly: false
  };
  monitorState = state;
  $("#btnMon").classList.add("primary");
  $("#btnMon").textContent = "Stop monitor";
  SEL = null;
  SELDEV = null;
  renderList();
  // Only constant markup is inserted. All captured values use textContent below.
  $("#detail").innerHTML = `
    <div class="card monitor-card" id="busMonitor">
      <h2>Bus monitor</h2>
      <div class="cmds monitor-tools">
        <label>View <select id="monView"><option value="reports">Reports</option><option value="transactions">Transactions</option><option value="all">All records</option></select></label>
        <button id="monPause">Pause display</button><button id="monFollow">Follow latest</button>
        <button id="monClear">Clear view</button><button id="monCopy">Copy visible rows</button>
        <a class="monitor-download" href="api/monitor/export?format=jsonl" download>Export JSONL</a>
        <a class="monitor-download" href="api/monitor/export?format=text" download>Export text</a>
      </div>
      <div class="cmds monitor-find">
        <label>find Hex <input id="monFindHex" type="text" spellcheck="false" placeholder="0x01 00"></label>
        <label>find Addr <input id="monFindAddr" type="text" placeholder="Gear G0"></label>
        <label>find Cmd <input id="monFindCmd" type="text" placeholder="Command or event"></label>
        <button id="monFindPrev" title="Shift+Enter">Previous match</button><button id="monFindNext" title="Enter">Next match</button>
        <span id="monMatchCount" aria-live="polite"></span>
      </div>
      <div id="monHexError" class="monitor-error" role="status"></div>
      <details class="monitor-filters"><summary>Optional filters</summary><div class="cmds monitor-tools">
        <label>Class <select id="monClass"><option value="">All classes</option><option>forward frame</option><option>input event</option><option>backward frame</option><option>adapter status</option><option>transaction</option><option>application diagnostic</option></select></label>
        <label>Address <input id="monAddress" type="text" placeholder="Any address"></label>
        <label><input id="monQueries" type="checkbox"> Hide queries</label>
        <label><input id="monErrors" type="checkbox"> Errors only</label>
      </div></details>
      <div id="monStat" class="muted" aria-live="polite"></div>
      <div id="monAlerts" class="monitor-error" role="status"></div>
      <div class="cmds monitor-tools"><button id="monOlder">Older page</button><span id="monPage"></span><button id="monNewer">Newer page</button></div>
      <div id="monTableScroll" class="monitor-table-scroll"><table class="monitor-table"><thead><tr></tr></thead><tbody id="monRows"></tbody></table></div>
      <div id="monNotice" class="muted monitor-notice"></div>
    </div>`;
  const headings = $("#busMonitor thead tr");
  for (const column of MONITOR_COLUMNS) { headings.append(monitorNode("th", column)); }
  const bind = (id, event, handler) => $(id).addEventListener(event, handler);
  bind("#monView", "change", event => { state.view = event.target.value; monitorFilterChanged(state); });
  for (const [id, key] of [["#monFindHex", "findHex"], ["#monFindAddr", "findAddress"], ["#monFindCmd", "findCommand"]]) {
    bind(id, "input", event => { state[key] = event.target.value; monitorFilterChanged(state, true); });
    bind(id, "keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); findMonitor(event.shiftKey ? -1 : 1); }
    });
  }
  for (const [id, key] of [["#monClass", "classFilter"], ["#monAddress", "addressFilter"]]) {
    bind(id, id === "#monAddress" ? "input" : "change", event => { state[key] = event.target.value; monitorFilterChanged(state); });
  }
  for (const [id, key] of [["#monQueries", "hideQueries"], ["#monErrors", "errorsOnly"]]) {
    bind(id, "change", event => { state[key] = event.target.checked; monitorFilterChanged(state); });
  }
  bind("#monFindPrev", "click", () => findMonitor(-1));
  bind("#monFindNext", "click", () => findMonitor(1));
  bind("#monPause", "click", () => {
    state.paused = !state.paused;
    $("#monPause").textContent = state.paused ? "Resume" : "Pause display";
    renderMonitor();
    if (!state.paused) { scheduleMonitorPoll(state, 0); }
  });
  bind("#monFollow", "click", () => {
    state.following = true;
    state.paused = false;
    state.selected = null;
    $("#monPause").textContent = "Pause display";
    renderMonitor();
    scheduleMonitorPoll(state, 0);
  });
  bind("#monClear", "click", clearMonitor);
  bind("#monCopy", "click", copyMonitorRows);
  bind("#monOlder", "click", () => monitorPage(-1));
  bind("#monNewer", "click", () => monitorPage(1));
  // Covers every existing screen switch, including async actions which replace detail.
  state.observer = new MutationObserver(() => {
    if (monOn && monitorState === state && !document.getElementById("busMonitor")) { stopMonitor(); }
  });
  state.observer.observe($("#detail"), { childList: true });
  renderMonitor();
  pollMonitor(state);
}

function stopMonitor() {
  monOn = false;
  monitorEpoch += 1;
  const state = monitorState;
  if (state) {
    clearTimeout(state.timer);
    if (state.controller) { state.controller.abort(); }
    if (state.observer) { state.observer.disconnect(); }
  }
  const button = document.getElementById("btnMon");
  if (button) { button.classList.remove("primary"); button.textContent = "🔎 Bus monitor"; }
  const status = document.getElementById("monStat");
  if (status) { status.textContent = "Display stopped. Shared capture continues."; }
}

function monitorActive(state) {
  return monOn && monitorState === state && state.epoch === monitorEpoch && document.getElementById("busMonitor") !== null;
}

function scheduleMonitorPoll(state, delay = MONITOR_POLL_MS) {
  if (!monitorActive(state) || state.loading) { return; }
  clearTimeout(state.timer);
  state.timer = setTimeout(() => pollMonitor(state), delay);
}

async function pollMonitor(state = monitorState) {
  if (!state || !monitorActive(state) || state.loading) { return; }
  clearTimeout(state.timer);
  state.loading = true;
  try {
    let more = true;
    while (more && monitorActive(state)) {
      state.controller = new AbortController();
      const query = new URLSearchParams({ since: String(state.cursor), limit: state.paused ? "1" : "500" });
      if (state.captureId !== null) { query.set("capture_id", state.captureId); }
      const response = await fetch("api/monitor/records?" + query, { signal: state.controller.signal });
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      const data = await response.json();
      if (!monitorActive(state)) { return; }
      state.requestError = null;
      const changed = state.captureId !== null && data.capture_id !== state.captureId;
      if (changed || data.capture_reset) {
        state.records = [];
        state.columns = new WeakMap();
        state.cursor = 0;
        state.floor = 0;
        state.selected = null;
        state.expanded.clear();
        state.notices.add("Capture reset: application restarted; previous client history was cleared.");
      }
      state.captureId = data.capture_id;
      // Keep payload records only in the bounded history, never in metadata.
      const { records: receivedRecords = [], ...metadata } = data;
      state.metadata = metadata;
      if ((changed || data.capture_reset) && state.paused) { renderMonitor(); }
      if (data.history_gap) { state.notices.add("History gap: older records were evicted before this browser received them."); }
      // While paused, keep only the existing client history. Re-read from its
      // cursor on Resume; the shared server capture continues with bounded memory.
      if (!state.paused) {
        const previousCursor = state.cursor;
        for (const record of receivedRecords) {
          if (record.seq > state.floor && record.seq > state.cursor) { state.records.push(record); }
        }
        if (state.records.length > MONITOR_HISTORY_LIMIT) {
          state.records.splice(0, state.records.length - MONITOR_HISTORY_LIMIT);
        }
        state.cursor = Math.max(state.cursor, data.next ?? state.cursor);
        const retained = new Set(state.records.map(record => record.seq));
        for (const sequence of state.expanded) { if (!retained.has(sequence)) { state.expanded.delete(sequence); } }
        more = Boolean(data.has_more) && state.cursor > previousCursor;
        renderMonitor();
      } else {
        more = false;
        // The table remains frozen. Connection/request state remains visible.
        renderMonitorStatus(state);
      }
    }
  } catch (error) {
    if (monitorActive(state) && !(error instanceof DOMException && error.name === "AbortError")) {
      state.requestError = error instanceof Error ? error.message : "Request failed";
      renderMonitorStatus(state);
    }
  } finally {
    state.loading = false;
    state.controller = null;
    scheduleMonitorPoll(state);
  }
}

function clearMonitor() {
  const state = monitorState;
  if (!state || !monitorActive(state)) { return; }
  state.floor = Math.max(state.cursor, state.metadata.newest ?? 0);
  state.cursor = state.floor;
  state.records = [];
  state.columns = new WeakMap();
  state.expanded.clear();
  state.selected = null;
  state.page = 0;
  renderMonitor();
}

function monitorAddress(record) {
  const decoded = record.decoded || {};
  const source = decoded.source;
  const destination = decoded.destination;
  const address = source || destination;
  if (!address) { return "—"; }
  const namespace = address.namespace === "gear" ? "Gear" : address.namespace === "device" ? "Device" : "Unknown namespace";
  let value;
  if (address.kind === "short") { value = (address.namespace === "gear" ? "A" : "D") + address.address; }
  else if (address.kind === "group") { value = "G" + address.address; }
  else if (address.kind === "device_group") { value = "device group G" + address.address; }
  else if (address.kind === "instance_group") { value = "instance group G" + address.address; }
  else if (address.kind === "broadcast") { value = "broadcast"; }
  else if (address.kind === "broadcast_unaddressed") { value = "unaddressed broadcast"; }
  else { value = address.address === null || address.address === undefined ? address.kind || "unknown address" : String(address.address); }
  if (source && decoded.instance_number !== null && decoded.instance_number !== undefined) { value += " / I" + decoded.instance_number; }
  const instance = decoded.destination_instance;
  if (!source && instance && instance.value !== null && instance.value !== undefined) { value += " / " + instance.kind + " " + instance.value; }
  return namespace + " " + value + (source ? " -> source" : " -> destination");
}

function monitorCommand(record) {
  const decoded = record.decoded || {};
  const reply = record.reply || {};
  const application = record.application;
  if (application) {
    if (application.event === "connection") { return "Connection " + application.status; }
    return (application.name || application.event) + ": " + (application.phase || "diagnostic");
  }
  if (decoded.command_text) {
    let text = decoded.command_text;
    if (record.classification === "transaction" && reply.text !== null && reply.text !== undefined) { text += " -> " + reply.text; }
    if (record.classification === "transaction" && reply.state === "no response") { text += " (no response)"; }
    return text;
  }
  if (record.classification === "backward frame") { return "Backward response 0x" + (record.frame?.hex ?? "unknown"); }
  const adapter = record.adapter || {};
  if (adapter.mode_code === 1) { return "Adapter initialization"; }
  if (adapter.type_code === 0x71) { return "No response (adapter notification)"; }
  if (adapter.type_code === 0x77) { return (record.diagnostics || {}).adapter_status || "Adapter status"; }
  return "Unknown / unsupported adapter report";
}

function monitorComments(record) {
  const diagnostics = record.diagnostics || {};
  const comments = [...(Array.isArray(diagnostics.comments) ? diagnostics.comments : [])];
  if (diagnostics.configuration_repeat_validation_failed) { comments.push("configuration repeat validation failed"); }
  if (diagnostics.unknown) { comments.push("Unknown decode"); }
  if (diagnostics.ambiguous) { comments.push("Ambiguous input-event type"); }
  if (diagnostics.decode_error) { comments.push("Decode error: " + diagnostics.decode_error); }
  if (diagnostics.capture_error) { comments.push("Capture error: " + diagnostics.capture_error); }
  if (diagnostics.configuration_busy) { comments.push("Configuration busy (does not identify sender)"); }
  if (record.configuration) { comments.push("Operation: " + record.configuration.name); }
  if (record.adapter?.origin_hint) { comments.push(record.adapter.origin_hint); }
  if (record.application?.error) { comments.push(record.application.error); }
  return [...new Set(comments)].join("; ");
}

function monitorColumns(record, state) {
  const cached = state.columns.get(record);
  if (cached) { return cached; }
  const received = new Date(record.received_utc);
  const validDate = !Number.isNaN(received.getTime());
  const time = validDate ? received.toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3 }) : "—";
  const type = record.classification || "unknown";
  const columns = [String(record.seq), type.charAt(0).toUpperCase() + type.slice(1), record.frame?.hex ?? "—", monitorAddress(record), monitorCommand(record), time, validDate ? received.toLocaleDateString() : "—", monitorComments(record)];
  state.columns.set(record, columns);
  return columns;
}

function monitorHasError(record) {
  const diagnostics = record.diagnostics || {};
  return Boolean(diagnostics.decode_error || diagnostics.capture_error || diagnostics.configuration_repeat_validation_failed || record.application?.phase === "failure" || [1, 2, 3].includes(diagnostics.adapter_status_code));
}

function monitorFilterChanged(state, searchChanged = false) {
  state.page = 0;
  if (searchChanged) { state.selected = null; }
  renderMonitor();
}

function monitorFindTerms(state) {
  const raw = state.findHex.trim().replace(/^0x/i, "").replace(/[\s:-]/g, "");
  const valid = state.findHex.trim() === "" || /^[0-9a-f]+$/i.test(raw);
  return { hex: raw.toUpperCase(), address: state.findAddress.toLowerCase(), command: state.findCommand.toLowerCase(), valid, active: Boolean(state.findHex.trim() || state.findAddress || state.findCommand) };
}

function renderMonitorStatus(state) {
  if (!monitorActive(state)) { return; }
  const counters = state.metadata.counters || {};
  const connection = state.metadata.connection ?? "unknown";
  $("#monStat").textContent = `${state.records.length.toLocaleString()} client retained · ${state.filtered.length.toLocaleString()} in view · ${state.visible.length} on page · capture ${state.captureId || "waiting"} · connection ${connection} · evicted ${counters.evicted ?? 0} · capture errors ${counters.capture_errors ?? 0} · decode errors ${counters.decode_errors ?? 0}${state.paused ? " · display paused" : state.following ? " · following latest" : " · follow suspended"}`;
  const notices = [...state.notices];
  if (connection === "disconnected" || connection === "failed") { notices.push("Connection lost: " + connection + ". Retained capture remains available."); }
  if (state.requestError) { notices.push("Request failed: " + state.requestError + ". Retrying; retained rows remain available."); }
  $("#monAlerts").textContent = notices.join(" ");
  $("#monNotice").textContent = (state.metadata.capabilities?.notice || "Waiting for driver capabilities.") + " Host receipt times are not precise wire timestamps. Only reports delivered by the interface are captured; interface-level losses may be invisible. Gear destinations cannot identify a physical sender. Pause and Clear affect this browser only. Downloads include all server-retained records, independent of these filters.";
}

function renderMonitor() {
  const state = monitorState;
  if (!state || !monitorActive(state)) { return; }
  const terms = monitorFindTerms(state);
  const addressFilter = state.addressFilter.toLowerCase();
  state.filtered = state.records.filter(record => {
    if (state.view === "reports" && (record.raw_report_hex === null || record.raw_report_hex === undefined)) { return false; }
    if (state.view === "transactions" && record.classification !== "transaction") { return false; }
    if (state.classFilter && record.classification !== state.classFilter) { return false; }
    if (state.hideQueries && (record.decoded?.is_query === true || /^Query/.test(record.decoded?.command_type || ""))) { return false; }
    if (state.errorsOnly && !monitorHasError(record)) { return false; }
    return !addressFilter || monitorColumns(record, state)[3].toLowerCase().includes(addressFilter);
  });
  state.matches = terms.active && terms.valid ? state.filtered.filter(record => {
    const columns = monitorColumns(record, state);
    return (!terms.hex || (record.frame?.hex || "").toUpperCase().includes(terms.hex)) && (!terms.address || columns[3].toLowerCase().includes(terms.address)) && (!terms.command || columns[4].toLowerCase().includes(terms.command));
  }).map(record => record.seq) : [];
  $("#monHexError").textContent = terms.valid ? "" : "Invalid hex: use hex digits, spaces, colons or hyphens, with optional 0x prefix.";
  $("#monFindHex").setAttribute("aria-invalid", String(!terms.valid));
  const selectionIndex = state.matches.indexOf(state.selected);
  $("#monMatchCount").textContent = terms.active ? `${state.matches.length} matches${selectionIndex >= 0 ? " · " + (selectionIndex + 1) + "/" + state.matches.length : ""}` : "Enter: next · Shift+Enter: previous";
  $("#monFindPrev").disabled = !state.matches.length;
  $("#monFindNext").disabled = !state.matches.length;
  const pageCount = Math.max(1, Math.ceil(state.filtered.length / MONITOR_PAGE_SIZE));
  if (state.following) { state.page = pageCount - 1; }
  else if (state.selected !== null) {
    const selectedIndex = state.filtered.findIndex(record => record.seq === state.selected);
    if (selectedIndex >= 0) { state.page = Math.floor(selectedIndex / MONITOR_PAGE_SIZE); }
  }
  state.page = Math.max(0, Math.min(state.page, pageCount - 1));
  state.visible = state.filtered.slice(state.page * MONITOR_PAGE_SIZE, (state.page + 1) * MONITOR_PAGE_SIZE);
  $("#monPage").textContent = `Page ${state.page + 1} of ${pageCount}`;
  $("#monOlder").disabled = state.page === 0;
  $("#monNewer").disabled = state.page === pageCount - 1;
  const matchSet = new Set(state.matches);
  const fragment = document.createDocumentFragment();
  for (const record of state.visible) {
    const row = monitorNode("tr");
    row.dataset.sequence = String(record.seq);
    if (matchSet.has(record.seq)) { row.classList.add("monitor-match"); }
    if (record.seq === state.selected) { row.classList.add("monitor-selected"); }
    const columns = monitorColumns(record, state);
    columns.forEach((value, index) => {
      const cell = monitorNode("td");
      if (index === 0) {
        const expand = monitorNode("button", (state.expanded.has(record.seq) ? "▾ " : "▸ ") + value, "monitor-expand");
        expand.setAttribute("aria-expanded", String(state.expanded.has(record.seq)));
        expand.setAttribute("aria-label", "Details for line " + value);
        expand.addEventListener("click", () => {
          state.following = false;
          if (state.expanded.has(record.seq)) { state.expanded.delete(record.seq); } else { state.expanded.add(record.seq); }
          renderMonitor();
        });
        cell.append(expand);
      } else { cell.textContent = value; }
      row.append(cell);
    });
    fragment.append(row);
    if (state.expanded.has(record.seq)) {
      const detailsRow = monitorNode("tr", undefined, "monitor-details");
      const cell = monitorNode("td");
      cell.colSpan = MONITOR_COLUMNS.length;
      cell.append(monitorNode("pre", JSON.stringify(record, null, 2)));
      detailsRow.append(cell);
      fragment.append(detailsRow);
    }
  }
  if (!state.visible.length) {
    const row = monitorNode("tr");
    const cell = monitorNode("td", "No records in this view. Capture continues; try All records for decoded-only drivers.", "muted");
    cell.colSpan = MONITOR_COLUMNS.length;
    row.append(cell);
    fragment.append(row);
  }
  $("#monRows").replaceChildren(fragment);
  renderMonitorStatus(state);
  if (state.following && !state.paused) {
    const scroll = $("#monTableScroll");
    scroll.scrollTop = scroll.scrollHeight;
  }
}

function findMonitor(direction) {
  const state = monitorState;
  if (!state || !monitorActive(state) || !state.matches.length) { return; }
  const current = state.matches.indexOf(state.selected);
  const index = current < 0 ? (direction > 0 ? 0 : state.matches.length - 1) : (current + direction + state.matches.length) % state.matches.length;
  state.selected = state.matches[index];
  state.following = false;
  renderMonitor();
  const row = document.querySelector(`#monRows tr[data-sequence="${state.selected}"]`);
  if (row) { row.scrollIntoView({ block: "nearest" }); }
}

function monitorPage(direction) {
  const state = monitorState;
  if (!state || !monitorActive(state)) { return; }
  state.following = false;
  state.selected = null;
  state.page += direction;
  renderMonitor();
  $("#monTableScroll").scrollTop = 0;
}

async function copyMonitorRows() {
  const state = monitorState;
  if (!state || !monitorActive(state)) { return; }
  const copiedRowCount = state.visible.length;
  const lines = [MONITOR_COLUMNS, ...state.visible.map(record => monitorColumns(record, state))];
  const text = lines.map(columns => columns.map(value => String(value).replace(/[\t\r\n]/g, " ")).join("\t")).join("\n");
  try {
    if (!navigator.clipboard) { throw new Error("Clipboard unavailable"); }
    await navigator.clipboard.writeText(text);
    if (!monitorActive(state)) { return; }
    toast("Copied " + copiedRowCount + " visible rows");
  } catch (_) {
    if (!monitorActive(state)) { return; }
    const textArea = monitorNode("textarea");
    textArea.value = text;
    textArea.setAttribute("aria-label", "Visible monitor rows to copy");
    $("#busMonitor").append(textArea);
    textArea.select();
    const copied = document.execCommand("copy");
    textArea.remove();
    toast(copied ? "Visible rows copied" : "Clipboard access unavailable");
  }
}
