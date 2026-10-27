"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { webcrypto, createHash } = require("node:crypto");
const fs = require("node:fs");
const vm = require("node:vm");

function harness({ secondEnabledAt = 0, alwaysDisabled = false } = {}) {
  const values = new Map();
  const calls = { clicked: 0, waited: 0, injected: "" };
  let listener;
  class FakeTextArea {
    constructor() { this._value = ""; this.isConnected = true; }
    getClientRects() { return [1]; }
    closest() { return form; }
    focus() {}
    dispatchEvent() {}
    get value() { return this._value; }
    set value(next) { this._value = next; calls.injected = next; }
  }
  class FakeButton {
    constructor(selector) { this.selector = selector; this.isConnected = true; }
    get disabled() {
      return this.selector === "old" || alwaysDisabled || calls.waited < secondEnabledAt;
    }
    getAttribute() { return null; }
    getClientRects() { return [1]; }
    closest() { return form; }
    click() { calls.clicked++; target._value = ""; }
  }
  const target = new FakeTextArea();
  const oldDisabled = new FakeButton("old");
  const usable = new FakeButton("new");
  const query = selector => ({
    "#prompt-textarea": [target],
    "#composer-submit-button": [oldDisabled],
    'button[data-testid="send-button"]': [usable],
  }[selector] || []);
  const form = { querySelectorAll: query, querySelector: selector => query(selector)[0] || null,
    addEventListener() {}, removeEventListener() {} };
  const document = { querySelectorAll: query, querySelector: selector => query(selector)[0] || null, execCommand: () => false };
  const context = vm.createContext({
    location: { href: "https://chatgpt.com/" }, URL, crypto: webcrypto, TextEncoder,
    Date: { now: () => calls.waited * 100 },
    HTMLTextAreaElement: FakeTextArea, HTMLInputElement: FakeTextArea, HTMLButtonElement: FakeButton,
    HTMLElement: FakeTextArea, HTMLBRElement: class Break {}, Node: {TEXT_NODE:3},
    Event: class Event { constructor() {} },
    InputEvent: class InputEvent { constructor() {} },
    document,
    sessionStorage: {
      getItem: key => values.get(key) || null,
      setItem: (key, value) => values.set(key, value)
    },
    chrome: { runtime: { onMessage: { addListener: callback => { listener = callback; } } } },
    setTimeout: callback => { calls.waited++; Promise.resolve().then(callback); }
  });
  vm.runInContext(fs.readFileSync(require.resolve("./github_first_composer.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(require.resolve("./spawn_content.js"), "utf8"), context);
  const transactionId = "spawn-" + "7".repeat(64);
  const bootstrapText = "Child bootstrap: use GitHub Connector only.";
  const envelope = {
    type: "bridge:spawn-bootstrap", protocolVersion: 1, transactionId,
    bootstrapText, childRequestDigest: "sha256:" + "b".repeat(64),
    bootstrapDigest: "sha256:" + createHash("sha256").update(bootstrapText).digest("hex")
  };
  function message(body) {
    return new Promise(resolve => {
      listener(body, {}, resolve);
    });
  }
  return { message, envelope, calls, values, transactionId, bootstrapText };
}

test("selects the enabled Send button rather than a stale disabled first match", async () => {
  const h = harness();
  const response = await h.message(h.envelope);
  assert.equal(response.reason, "composer_cleared_after_click");
  assert.equal(h.calls.clicked, 1);
  assert.equal(h.calls.injected, h.bootstrapText);
  assert.equal(h.values.get("local-agent-github-first-submission:" + h.transactionId), "composer_cleared_after_click");
  const again = await h.message(h.envelope);
  assert.equal(again.reason, "submission_unknown");
  assert.equal(h.calls.clicked, 1, "no duplicate click");
});

test("waits for a slow-hydrating Send button beyond the old three-second window", async () => {
  const h = harness({ secondEnabledAt: 55 });
  assert.equal((await h.message(h.envelope)).reason, "composer_cleared_after_click");
  assert.ok(h.calls.waited >= 55, "new wait window allows late activation");
  assert.equal(h.calls.clicked, 1);
});

test("records when no click was attempted and never retries an unknown transaction", async () => {
  const h = harness({ alwaysDisabled: true });
  assert.equal((await h.message(h.envelope)).reason, "send_not_attempted");
  assert.equal(h.calls.clicked, 0);
  assert.equal(h.values.get("local-agent-github-first-submission:" + h.transactionId), "send_not_attempted");
  assert.equal((await h.message(h.envelope)).reason, "submission_unknown");
  assert.equal(h.calls.clicked, 0);
});

test("diagnostic reveals a technical phase, not bootstrap contents", async () => {
  const h = harness({ alwaysDisabled: true });
  await h.message(h.envelope);
  const diagnostic = await h.message({ type: "bridge:spawn-diagnostic", transactionId: h.transactionId });
  assert.equal(diagnostic.reason, "send_not_attempted");
  assert.equal(diagnostic.route, "fresh");
  assert.equal(diagnostic.sendReady, false);
  assert.equal(JSON.stringify(diagnostic).includes(h.bootstrapText), false);
});
