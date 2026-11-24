"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page() {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) {
      let content = "";
      elements.set(selector, {
        classList: { add() {}, remove() {} },
        get innerHTML() { return content; },
        set innerHTML(value) { content = value; },
        get textContent() { return content; },
        set textContent(value) { content = value; }
      });
    }
    return elements.get(selector);
  };
  const state = { scan: { supported: true, devices: [{ address: 0 }] }, requests: [] };
  const context = vm.createContext({
    document: { querySelector: element },
    setTimeout() {},
    async fetch(url, options) {
      state.requests.push([url, options]);
      if (url === "api/device/0") {
        return await state.pending;
      }
      return { ok: !state.error, json: async () => state.error ? { error: state.error } : state.scan };
    }
  });
  const html = fs.readFileSync(path.join(__dirname, "../app/static/index.html"), "utf8");
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  return { context, element, state, read: expression => vm.runInContext(expression, context) };
}

test("missing selected device clears controls and ignores delayed instance response", async () => {
  const { context, element, state, read } = page();
  await context.scanDevices();
  context.selectDevice(0);
  assert.match(element("#detail").innerHTML, /Read instances/);
  let resolve;
  state.pending = new Promise(done => { resolve = done; });
  const loading = context.loadDevice(0);
  state.scan.devices = [];
  await context.scanDevices();
  assert.equal(read("SELDEV"), null);
  assert.equal(read("DEVICES.length"), 0);
  assert.doesNotMatch(element("#list").innerHTML, /selectDevice\(0\)/);
  const message = "D0 was not found in the latest scan. Saved MQTT profile retained.";
  assert.equal(element("#detail").textContent, message);
  // Missing controller deliberately makes any stale response processing fail.
  resolve({ ok: true, json: async () => ({ instances: [] }) });
  await loading;
  assert.equal(element("#detail").textContent, message);
  assert.match(element("#devInstances").innerHTML, /Reading/);
  assert.ok(state.requests.every(([, options]) => !options));
  assert.equal(element("#btnScanDev").disabled, false);
});

test("present device preserves selected panel and unsaved edits", async () => {
  const { context, element, read } = page();
  await context.scanDevices();
  context.selectDevice(0);
  element("#detail").innerHTML = "unsaved panel";
  element("#mqttProfile").value = "unsaved profile";
  await context.scanDevices();
  assert.equal(read("SELDEV"), 0);
  assert.equal(element("#detail").innerHTML, "unsaved panel");
  assert.equal(element("#mqttProfile").value, "unsaved profile");
});

test("failed and unsupported scans retain results and selection, reset button", async () => {
  const { context, element, state, read } = page();
  await context.scanDevices();
  context.selectDevice(0);
  const panel = element("#detail").innerHTML;
  const list = element("#list").innerHTML;
  state.error = "adapter unavailable";
  await context.scanDevices();
  assert.equal(element("#toast").textContent,
    "Device scan failed: adapter unavailable Previous results retained.");
  state.error = null;
  state.scan = { supported: false, devices: [] };
  await context.scanDevices();
  assert.equal(read("SELDEV"), 0);
  assert.equal(read("DEVICES.length"), 1);
  assert.equal(element("#detail").innerHTML, panel);
  assert.equal(element("#list").innerHTML, list);
  assert.equal(element("#btnScanDev").disabled, false);
  assert.equal(element("#btnScanDev").textContent, "Scan devices");
});

test("empty device scan preserves gear selection and other active views", async () => {
  const { context, element, state, read } = page();
  for (const selection of [6, null]) {
    vm.runInContext(`SEL = ${selection}; SELDEV = null;`, context);
    element("#detail").innerHTML = "active view";
    state.scan.devices = [];
    await context.scanDevices();
    assert.equal(read("SEL"), selection);
    assert.equal(element("#detail").innerHTML, "active view");
  }
});
