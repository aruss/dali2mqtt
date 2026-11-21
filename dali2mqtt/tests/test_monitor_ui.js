"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../app/static/monitor.js"), "utf8");
const CAPTURE_ID = "00000000-0000-4000-8000-000000000001";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createElement() {
  return {
    textContent: "", dataset: {}, attributes: {}, children: [], disabled: false,
    classList: { add() {}, remove() {} },
    addEventListener() {},
    setAttribute(name, value) { this.attributes[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    scrollIntoView() {}, select() {}, remove() {}
  };
}

function loadMonitor() {
  const environment = { active: true, fallbackCount: 0, toasts: [], elements: new Map() };
  const element = selector => {
    if (!environment.elements.has(selector)) { environment.elements.set(selector, createElement()); }
    return environment.elements.get(selector);
  };
  const context = vm.createContext({
    AbortController, DOMException, URLSearchParams,
    setTimeout: () => 1, clearTimeout() {},
    fetch: () => { throw new Error("Unexpected request"); },
    navigator: { clipboard: { writeText: async () => {} } },
    toast: text => environment.toasts.push(text),
    $: element,
    document: {
      getElementById: id => id === "busMonitor" && !environment.active ? null : element("#" + id),
      querySelector: element,
      createElement: tag => {
        if (tag === "textarea") { environment.fallbackCount += 1; }
        return createElement();
      },
      createDocumentFragment: createElement,
      execCommand: () => true
    }
  });
  vm.runInContext(source, context, { filename: "monitor.js" });
  vm.runInContext(`globalThis.monitorApi = {
    attach(state) { monitorState = state; monOn = true; monitorEpoch = state.epoch; },
    pollMonitor, clearMonitor, copyMonitorRows, renderMonitor, findMonitor
  };`, context);
  return { environment, context, api: context.monitorApi, element };
}

function captureRecord(seq, group = 0, command = "Off", hex = "0100") {
  return {
    seq, classification: "forward frame", raw_report_hex: "1100",
    received_utc: "2026-11-21T12:00:00.123+00:00",
    frame: { hex },
    decoded: {
      command_type: command, command_text: command + "(Group(" + group + "))",
      destination: { namespace: "gear", kind: "group", address: group }
    },
    diagnostics: {}, adapter: {}, reply: {}
  };
}

function monitorState(records = []) {
  return {
    epoch: 1, records, columns: new WeakMap(), cursor: 10, captureId: CAPTURE_ID,
    floor: 0, page: 0, selected: null, following: false, paused: false, loading: false,
    expanded: new Set(), matches: [], filtered: [], visible: records,
    notices: new Set(), requestError: null, metadata: { newest: 10 },
    view: "reports", findHex: "", findAddress: "", findCommand: "",
    classFilter: "", addressFilter: "", hideQueries: false, errorsOnly: false
  };
}

test("Clear during a pending poll retains traffic captured after the click", async () => {
  const { context, api } = loadMonitor();
  const state = monitorState([captureRecord(10)]);
  const request = deferred();
  context.fetch = () => request.promise;
  api.attach(state);

  const polling = api.pollMonitor(state);
  assert.equal(state.loading, true);
  api.clearMonitor();
  assert.equal(state.floor, 10);
  assert.equal(state.records.length, 0);

  // The response is first snapshotted after Clear, when a new gesture exists.
  request.resolve({
    ok: true,
    json: async () => ({
      capture_id: CAPTURE_ID, records: [captureRecord(10), captureRecord(11)],
      next: 11, oldest: 1, newest: 11, has_more: false,
      counters: {}, capabilities: {}
    })
  });
  await polling;
  assert.equal(state.floor, 10);
  assert.equal(state.cursor, 11);
  assert.equal(state.requestError, null);
  assert.deepEqual(Array.from(state.records, record => record.seq), [11]);
});

test("Clipboard rejection after navigation does not touch the old monitor", async () => {
  const { environment, context, api } = loadMonitor();
  const state = monitorState([captureRecord(10)]);
  const clipboard = deferred();
  context.navigator.clipboard.writeText = () => clipboard.promise;
  api.attach(state);

  const copying = api.copyMonitorRows();
  environment.active = false;
  clipboard.reject(new Error("Page lost focus"));
  await assert.doesNotReject(copying);
  assert.equal(environment.fallbackCount, 0);
  assert.deepEqual(environment.toasts, []);
});

test("Clipboard success reports the copied snapshot count", async () => {
  const { environment, context, api } = loadMonitor();
  const state = monitorState([captureRecord(10)]);
  const clipboard = deferred();
  context.navigator.clipboard.writeText = () => clipboard.promise;
  api.attach(state);

  const copying = api.copyMonitorRows();
  state.visible = [captureRecord(10), captureRecord(11)];
  clipboard.resolve();
  await copying;
  assert.deepEqual(environment.toasts, ["Copied 1 visible rows"]);
});

test("Find combines normalized hex, address and command without hiding context", () => {
  const { api, element } = loadMonitor();
  const state = monitorState([
    captureRecord(1), captureRecord(2, 0, "On"),
    captureRecord(3, 1), captureRecord(4, 0, "Off", "0200")
  ]);
  state.findHex = "0X01: 00";
  state.findAddress = "GEAR G0";
  state.findCommand = "oFf";
  api.attach(state);
  api.renderMonitor();
  assert.deepEqual(Array.from(state.matches), [1]);
  assert.equal(state.filtered.length, 4);
  api.findMonitor(1);
  assert.equal(state.selected, 1);
  assert.equal(state.following, false);

  state.findHex = "0x";
  api.renderMonitor();
  assert.equal(state.matches.length, 0);
  assert.equal(element("#monFindHex").attributes["aria-invalid"], "true");
  assert.match(element("#monHexError").textContent, /Invalid hex/);
});
