"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function page(result) {
  const elements = new Map();
  const timers = [];
  const element = selector => {
    if (!elements.has(selector)) {
      elements.set(selector, {
        textContent: "", classList: { add() {}, remove() {} },
        set innerHTML(value) { throw new Error("Unsafe HTML rendering: " + value); }
      });
    }
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: { querySelector: element },
    setTimeout(callback) { timers.push(callback); },
    confirm: () => true,
    async fetch() { return { ok: true, json: async () => result }; }
  });
  const html = fs.readFileSync(path.join(__dirname, "../app/static/index.html"), "utf8");
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/scan\(\);\s*$/, ""), context);
  context.scan = async () => {};
  return { context, element, timers };
}

test("complete refresh reports success", async () => {
  const { context, element } = page({ ok: true, partial: false, failed: [] });
  await context.reinit();
  assert.equal(element("#operationResult").textContent, "HA entities refreshed");
});

test("partial warning persists after toast expires and renders details as text", async () => {
  const { context, element, timers } = page({ ok: true, partial: true, failed: [
    { kind: "lamp", address: 0, query: "<img onerror=alert(1)>", reason: "mask" },
    { kind: "group", address: 2, query: "QueryActualLevel", reason: "missing" }
  ] });
  await context.reinit();
  timers.forEach(callback => callback());
  const text = element("#operationResult").textContent;
  assert.match(text, /incomplete.*A0.*<img.*G2/);
  assert.match(text, /Previous state preserved.*deferred/);
  assert.doesNotMatch(text, /HA entities refreshed/);
});

for (const refresh of [
  { ok: true, partial: true, failed: [{ kind: "lamp", address: 0, query: "QueryActualLevel", reason: "mask" }] },
  { ok: false, error: "transport failed" }
]) {
  test("commissioning completion survives incomplete entity refresh: " + refresh.ok, async () => {
    const { context, element } = page({ ok: true, refresh });
    await context.commission();
    assert.match(element("#operationResult").textContent, /Addressing completed/);
    assert.match(element("#operationResult").textContent, /Entity refresh (incomplete|failed)/);
    assert.doesNotMatch(element("#toast").textContent, /Commission failed/);
  });
}
