"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function harness({ wake, transport } = {}) {
  const html = fs.readFileSync(require.resolve("./popup.html"), "utf8");
  const css = fs.readFileSync(require.resolve("./popup.css"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]);
  const elements = new Map(ids.map(id => ["#" + id, {
    textContent: "", value: "", disabled: false, hidden: false, title: "", className: "",
    handlers: {}, classes: new Set(), addEventListener(event, callback) { this.handlers[event] = callback; },
    removeAttribute(key) { delete this[key]; },
    classList: null
  }]));
  for (const element of elements.values()) {
    element.classList = {
      add: name => element.classes.add(name),
      toggle: (name, on) => on ? element.classes.add(name) : element.classes.delete(name)
    };
  }
  const state = wake || { ok: true, enabled: false, intervalMinutes: 10,
    prompt: "Check GitHub and continue.", conversationUrl: "", lastStatus: "stopped" };
  const link = transport || { ok: true, paired: false, reachable: false,
    transactions: 0, unknown: 0, sendReleased: true, extensionVersion: "0.9.8" };
  const calls = [];
  const context = vm.createContext({
    document: { querySelector: selector => {
      const e = elements.get(selector);
      if (!e) throw new Error("Unknown popup element " + selector);
      return e;
    } },
    chrome: { runtime: { sendMessage: async payload => {
      calls.push(payload);
      if (payload.type === "bridge:auto-wake-status") return state;
      if (payload.type === "bridge:github-first-local-status") return link;
      if (payload.type === "bridge:auto-wake-start") return { ...state, ...payload.settings,
        ok: true, enabled: true, conversationUrl: "https://chatgpt.com/c/parent-1",
        nextWakeAt: Date.now() + 600000, lastStatus: "scheduled" };
      if (payload.type === "bridge:auto-wake-stop") return { ...state, ok: true, enabled: false,
        lastStatus: "stopped", nextWakeAt: null };
      return { ok: false };
    } } },
    setInterval: () => 1,
    Date, Promise, Number
  });
  vm.runInContext(fs.readFileSync(require.resolve("./popup.js"), "utf8"), context);
  const element = id => elements.get("#" + id);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return { html, css, elements, calls, element, settle,
    click: id => element(id).handlers.click(),
    input: id => element(id).handlers.input() };
}

test("popup retains semantic controls and readable minimum type hierarchy", async () => {
  const h = harness();
  await h.settle();
  assert.match(h.html, /<h1>Chat Bridge<\/h1>/);
  assert.match(h.html, /<h2 id="wakeTitle">Auto Wake<\/h2>/);
  assert.match(h.html, /<details class="advanced-settings">/);
  assert.match(h.css, /body \{[^}]*font-size: 12px;/);
  assert.doesNotMatch(h.css, /font-size:\s*[789]px/);
  assert.equal(h.element("useParent").disabled, true);
  assert.equal(h.element("autoWakeStop").disabled, true);
});

test("active wake shows target and next deadline and does not overwrite edits", async () => {
  const h = harness({ wake: { ok: true, enabled: true, intervalMinutes: 15,
    prompt: "Resume work.", conversationUrl: "https://chatgpt.com/c/parent-xyz-123",
    nextWakeAt: Date.now() + 600000, lastStatus: "sent" } });
  await h.settle();
  assert.equal(h.element("wakeState").textContent, "Active");
  assert.match(h.element("autoWakeTarget").textContent, /Chat …/);
  assert.match(h.element("autoWakeStatus").textContent, /Next wake:/);
  assert.equal(h.element("autoWakeInterval").value, 15);
  h.element("autoWakePrompt").value = "Unsaved edited message";
  h.input("autoWakePrompt");
  await h.click("autoWakeStart");
  assert.equal(h.calls.at(-1).type, "bridge:auto-wake-start");
  assert.equal(h.calls.at(-1).settings.prompt, "Unsaved edited message");
  assert.equal(h.element("wakeState").textContent, "Active");
});

test("uncertain wake is human-readable and allows Stop without automatic replay", async () => {
  const h = harness({ wake: { ok: true, enabled: false, intervalMinutes: 10,
    prompt: "Continue.", conversationUrl: "https://chatgpt.com/c/parent-1",
    lastStatus: "paused_send_click_unknown", nextWakeAt: null } });
  await h.settle();
  assert.equal(h.element("wakeState").textContent, "Needs attention");
  assert.match(h.element("autoWakeStatus").textContent, /Check the chat/);
  assert.doesNotMatch(h.element("autoWakeStatus").textContent, /send_click_unknown/);
  assert.equal(h.element("autoWakeStop").disabled, false);
  await h.click("autoWakeStop");
  assert.equal(h.element("wakeState").textContent, "Off");
  assert.equal(h.element("autoWakeStop").disabled, true);
  assert.equal(h.calls.filter(x => x.type === "bridge:auto-wake-start").length, 0);
});
