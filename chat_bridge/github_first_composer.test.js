"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness({ clearOnClick = true, delayed = 0, alwaysDisabled = false,
                   route = "https://chatgpt.com/c/test-parent", changeOnInject = false,
                   submitEvent = false, changeRouteAfterClick = false } = {}) {
  const values = new Map();
  const calls = { clicks: 0, waits: 0, injected: null, beforeClick: 0, phases: [], submitObserved: 0 };
  let listener, boundary = null;
  class Area {
    constructor() { this._value = ""; this.isConnected = true; }
    focus() {}
    dispatchEvent() {}
    get value() { return this._value; }
    set value(text) { this._value = text; calls.injected = text;
      if (changeOnInject) this._value = "Changed by editor"; }
    getClientRects() { return [1]; }
    closest() { return form; }
  }
  class Button {
    constructor(name) { this.name = name; this.isConnected = true; }
    get disabled() { return this.name === "stale" || alwaysDisabled || calls.waits < delayed; }
    getClientRects() { return [1]; }
    getAttribute() { return null; }
    closest() { return form; }
    click() {
      calls.clicks++;
      if (submitEvent && boundary) boundary();
      if (clearOnClick) area._value = "";
      if (changeRouteAfterClick) location.href = "https://chatgpt.com/c/a-created-child";
    }
  }
  const area = new Area(), stale = new Button("stale"), ready = new Button("ready");
  const query = selector => {
    if (selector === "#prompt-textarea") return [area];
    if (selector === "#composer-submit-button") return [stale];
    if (selector === 'button[aria-label="Send"]') return [ready];
    return [];
  };
  const form = { querySelector: selector => query(selector)[0] || null,
    querySelectorAll: query,
    addEventListener: (_type, callback) => { boundary = callback; },
    removeEventListener: () => { boundary = null; }
  };
  const document = { querySelectorAll: query, querySelector: selector => query(selector)[0] || null, execCommand: () => false };
  const location = { href: route };
  const context = vm.createContext({
    document, location, Date: { now: () => calls.waits * 100 },
    HTMLTextAreaElement: Area, HTMLInputElement: Area, HTMLButtonElement: Button,
    HTMLElement: Area, HTMLBRElement: class Break {}, Node: { TEXT_NODE: 3 },
    Event: class Event {}, InputEvent: class InputEvent {},
    sessionStorage: { getItem: key => values.get(key) || null,
      setItem: (key, value) => values.set(key, value) },
    chrome: { runtime: { onMessage: { addListener: callback => { listener = callback; } } } },
    setTimeout: callback => { calls.waits++; Promise.resolve().then(callback); }
  });
  const read = name => fs.readFileSync(require.resolve("./" + name), "utf8");
  vm.runInContext(read("github_first_composer.js"), context);
  function loadParent() { vm.runInContext(read("github_first_prompt_content.js"), context); }
  async function wake(wakeId = "wake-" + "1".repeat(64)) {
    return new Promise(resolve => listener({ type: "bridge:github-first-parent-wake",
      parentUrl: route, wakeId, text: "Read the exact GitHub result pointer." }, {}, resolve));
  }
  async function send() { return context.LocalAgentGithubFirstComposerV1.submitOnce({
    text: "Send the verified immutable prompt", routeAllowed: () => location.href === route,
    beforeMutation: () => calls.phases.push("armed"),
    beforeClick: () => { calls.beforeClick++; calls.phases.push("before_click"); },
    phase: phase => calls.phases.push(phase)
  }); }
  return { context, area, calls, values, loadParent, wake, send };
}

test("shared composer selects the live ready Send control, clears and never clicks twice", async () => {
  const h = harness();
  const s = await h.send();
  assert.equal(s.reason, "composer_cleared_after_click");
  assert.equal(s.submitted, true);
  assert.equal(h.calls.beforeClick, 1);
  assert.equal(h.calls.clicks, 1);
  assert.deepEqual(h.calls.phases.slice(0, 3), ["armed", "composer_injected_no_click", "before_click"]);
});

test("historical bridge Send accepts the already-present exact owned draft", async () => {
  const h = harness();
  h.area._value = "Send the verified immutable prompt";
  const result = await h.send();
  assert.equal(result.reason, "composer_cleared_after_click");
  assert.equal(h.calls.clicks, 1);
  assert.equal(h.calls.injected, null, "existing draft is reused, not rewritten");
  assert.equal(h.calls.beforeClick, 1);
});

test("parent wake waits for editor hydration and ACKs only after clear", async () => {
  const h = harness({ delayed: 48 });
  h.loadParent();
  const result = await h.wake();
  assert.equal(result.submitted, true);
  assert.equal(result.reason, "composer_cleared_after_click");
  assert.equal(h.calls.clicks, 1);
  assert.ok(h.calls.waits >= 48);
  assert.equal(h.values.get("local-agent-github-first-parent-wake:wake-" + "1".repeat(64)),
    "composer_cleared_after_click");
  assert.equal((await h.wake()).reason, "invalid_wake");
  assert.equal(h.calls.clicks, 1);
});

test("a click without submit or clear stays UNKNOWN and never retries", async () => {
  const h = harness({ clearOnClick: false });
  h.loadParent();
  const result = await h.wake();
  assert.equal(result.ok, false);
  assert.equal(result.submitted, false);
  assert.equal(result.reason, "send_click_unknown");
  assert.equal((await h.wake()).ok, false);
  assert.equal(h.calls.clicks, 1);
});

test("submit event alone cannot ACK a message that is still in editor", async () => {
  const h = harness({ clearOnClick: false, submitEvent: true });
  const result = await h.send();
  assert.equal(result.ok, false);
  assert.equal(result.reason, "send_click_unknown");
  assert.equal(h.calls.beforeClick, 1);
  assert.equal(h.calls.clicks, 1);
});

test("parent does not click when draft changes during React hydration", async () => {
  const h = harness({ changeOnInject: true });
  h.loadParent();
  const result = await h.wake();
  assert.equal(result.ok, false);
  assert.equal(h.calls.clicks, 0);
});

test("still-disabled button cannot cause click or hidden automatic Enter", async () => {
  const h = harness({ alwaysDisabled: true });
  h.loadParent();
  assert.equal((await h.wake()).reason, "send_not_attempted");
  assert.equal(h.calls.clicks, 0);
  assert.equal((await h.wake()).ok, false);
});

test("a child route change requires a separate pinned-tab ACK, never retries", async () => {
  const h = harness({ clearOnClick: false, changeRouteAfterClick: true });
  const result = await h.send();
  assert.equal(result.reason, "route_changed_after_click");
  assert.equal(result.ok, false);
  assert.equal(h.calls.clicks, 1);
});

function proseMirrorHarness({ execWorks = true, clearOnClick = true } = {}) {
  const calls = { inputs: [], exec: 0, clicks: 0, selections: 0, phases: [] };
  const prompt = 'LOCAL AGENT CHILD BOOTSTRAP\n{\n  "digest": "sha256:' + 'a'.repeat(64) + '"\n}\nDONE';
  let text = "", visible = true;
  const composer = {
    isConnected: true, isContentEditable: true, contentEditable: "true",
    classList: { contains: k => k === "ProseMirror" },
    getClientRects: () => visible ? [1] : [],
    checkVisibility: () => visible, focus() {},
    get innerText() { return text; },
    get textContent() { return text.replace(/\n/g, ""); },
    set textContent(v) { text = v; },
    childNodes: [],
    closest: () => form,
    dispatchEvent: event => { calls.inputs.push(event.type); }
  };
  const button = {
    isConnected: true, disabled: false, getClientRects: () => [1],
    checkVisibility: () => true, getAttribute: () => null,
    closest: () => form,
    click: () => { calls.clicks++; if (clearOnClick) { text = ""; composer.childNodes = []; } }
  };
  const form = {
    querySelector: selector => selector === 'button[aria-label="Send"]' ? button : null,
    querySelectorAll: selector => selector.includes('button') ? [button] : [],
    addEventListener() {}, removeEventListener() {}
  };
  const queries = selector => selector === 'form [contenteditable="true"]' ? [composer] : [];
  const context = vm.createContext({
    document: {
      querySelectorAll: queries,
      querySelector: selector => queries(selector)[0] || null,
      createRange: () => ({ selectNodeContents() {} }),
      execCommand: (_, __, value) => { calls.exec++;
        if (!execWorks) return false;
        text = value;
        composer.childNodes = value.split("\n").map(line => ({ tagName: "P", childNodes: [{ nodeType: 3, nodeValue: line }], innerText: line, textContent: line }));
        return true;
      }
    },
    window: { getSelection: () => ({ removeAllRanges() {}, addRange() { calls.selections++; } }) },
    HTMLTextAreaElement: class Area {}, HTMLInputElement: class Input {},
    HTMLElement: class Element { static [Symbol.hasInstance](node) { return node === composer || Boolean(node?.tagName); } },
    HTMLBRElement: class Break {}, Node: { TEXT_NODE: 3 },
    HTMLButtonElement: class Button { [Symbol.hasInstance]() { return true; } },
    Event: class Event { constructor(type) { this.type = type; } },
    InputEvent: class InputEvent { constructor(type) { this.type = type; } },
    setTimeout: callback => Promise.resolve().then(callback)
  });
  // Use a real instanceof match inside the VM, with no DOM-only text fallback.
  context.HTMLButtonElement = class Button { static [Symbol.hasInstance](v) { return v === button; } };
  vm.runInContext(fs.readFileSync(require.resolve("./github_first_composer.js"), "utf8"), context);
  return { prompt, calls, context, composer, button, setVisible: v => { visible = v; } };
}

test("ProseMirror requires selection and a successful native insertion command", async () => {
  const h = proseMirrorHarness();
  const result = await h.context.LocalAgentGithubFirstComposerV1.submitOnce({
    text: h.prompt, routeAllowed: () => true,
    beforeMutation: () => h.calls.phases.push("armed"),
    beforeClick: () => h.calls.phases.push("click_armed"),
    phase: value => h.calls.phases.push(value)
  });
  assert.equal(result.submitted, true);
  assert.equal(h.calls.clicks, 1);
  assert.equal(h.calls.exec, 1);
  assert.equal(h.calls.selections, 1);
  assert.deepEqual(h.calls.inputs, ["beforeinput", "input"]);
  assert.equal(h.calls.phases[0], "armed");
});

test("historical ProseMirror fallback dispatches input and follows native Send", async () => {
  const h = proseMirrorHarness({ execWorks: false });
  const result = await h.context.LocalAgentGithubFirstComposerV1.submitOnce({
    text: h.prompt, routeAllowed: () => true,
    beforeMutation: () => h.calls.phases.push("armed"),
    beforeClick: () => h.calls.phases.push("click_armed"),
    phase: value => h.calls.phases.push(value)
  });
  assert.equal(result.reason, "composer_cleared_after_click");
  assert.equal(h.calls.clicks, 1);
  assert.equal(h.calls.exec, 1);
  assert.ok(h.calls.inputs.includes("input"));
  assert.ok(h.calls.phases.includes("click_armed"));
});

// Offline reproduction of the October 12 saved real ChatGPT ProseMirror draft:
// paragraph.innerText strips JSON leading spaces while paragraph.textContent
// preserves the exact immutable bootstrap bytes and terminal newline.
test("ProseMirror multiline JSON preserves indentation and reaches exactly one Send", async () => {
  const draft = 'LOCAL AGENT CHILD BOOTSTRAP\n{\n  "bootstrap_contract_version": 1,\n' +
    '  "child_request_digest": "sha256:abcdef",\n  "instructions": [\n' +
    '    "Do not replay",\n  ]\n}\n';
  const calls = { clicks: 0, phases: [], waits: 0 };
  let submitListener;
  class ProseMirror {
    constructor() { this.isConnected = true; this.isContentEditable = true; this.contentEditable = "true"; this.childNodes = []; }
    get innerText() { return this.childNodes.map(p => p.innerText).join("\n\n"); }
    get textContent() { return this.childNodes.map(p => p.textContent).join(""); }
    getClientRects() { return [1]; }
    closest() { return form; }
    focus() {}
    dispatchEvent() { return true; }
    replace(value) { this.childNodes = value.split("\n").map(line => ({
      tagName: "P", childNodes: [{ nodeType: 3, nodeValue: line }], textContent: line, innerText: line.trimStart()
    })); }
  }
  class Button {
    constructor() { this.isConnected = true; this.disabled = false; }
    getClientRects() { return [1]; }
    getAttribute() { return null; }
    closest() { return form; }
    click() { calls.clicks++; submitListener?.(); editor.childNodes = []; }
  }
  const editor = new ProseMirror(), button = new Button();
  const selector = s => {
    if (s === 'form [contenteditable="true"]') return [editor];
    if (s === 'button[aria-label="Send"]') return [button];
    return [];
  };
  const form = { querySelectorAll: selector, querySelector: s => selector(s)[0] || null,
    addEventListener: (_name, cb) => { submitListener = cb; },
    removeEventListener: () => { submitListener = undefined; }
  };
  const document = { querySelectorAll: selector, querySelector: s => selector(s)[0] || null,
    createRange: () => ({ selectNodeContents: () => undefined }),
    execCommand: (_command, _flag, value) => { editor.replace(value); return true; }
  };
  const context = vm.createContext({
    document, window: { getSelection: () => ({ removeAllRanges() {}, addRange() {} }) },
    HTMLTextAreaElement: class TextArea {}, HTMLInputElement: class Input {},
    HTMLElement: class Element { static [Symbol.hasInstance](node) { return node === editor || Boolean(node?.tagName); } },
    HTMLBRElement: class Break {}, Node: { TEXT_NODE: 3 }, HTMLButtonElement: Button,
    Event: class Event {}, InputEvent: class InputEvent {},
    Date: { now: () => calls.waits * 100 },
    setTimeout: callback => { calls.waits++; Promise.resolve().then(callback); }
  });
  vm.runInContext(fs.readFileSync(require.resolve("./github_first_composer.js"), "utf8"), context);
  const outcome = await context.LocalAgentGithubFirstComposerV1.submitOnce({
    text: draft, routeAllowed: () => true,
    beforeMutation: () => calls.phases.push("durable_claim"),
    beforeClick: () => calls.phases.push("click_claim"),
    phase: p => calls.phases.push(p)
  });
  assert.equal(outcome.ok, true);
  assert.equal(outcome.submitted, true);
  assert.equal(calls.clicks, 1);
  assert.deepEqual(calls.phases.slice(0, 3),
    ["durable_claim", "composer_injected_no_click", "click_claim"]);
});
