"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("control-device page assigns and removes the selected knob's MQTT profile", async () => {
  const html = fs.readFileSync(path.join(__dirname, "../app/static/index.html"), "utf8");
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) {
      elements.set(selector, { classList: { add() {}, remove() {} } });
    }
    return elements.get(selector);
  };
  const writes = [];
  const context = vm.createContext({
    document: { querySelector: element },
    setTimeout() {},
    async fetch(url, options) {
      let data = {};
      if (url === "api/scan_devices") {
        data = { supported: true, devices: [{ address: 1 }, { address: 2 }] };
      } else if (options?.method === "POST") {
        data = JSON.parse(options.body);
        writes.push([url, data.profile]);
      }
      return { ok: true, json: async () => data };
    }
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await context.scanDevices();
  context.selectDevice(1);
  assert.match(element("#detail").innerHTML, /Home Assistant MQTT profile/);
  element("#mqttProfile").value = "jung_1745_dste_absolute";
  await context.saveProfile(1, {});
  context.selectDevice(2);
  assert.equal(element("#mqttProfile").value, "");
  context.selectDevice(1);
  assert.equal(element("#mqttProfile").value, "jung_1745_dste_absolute");
  element("#mqttProfile").value = "";
  await context.saveProfile(1, {});
  assert.deepEqual(writes, [
    ["api/device/1/profile", "jung_1745_dste_absolute"],
    ["api/device/1/profile", null]
  ]);
});
