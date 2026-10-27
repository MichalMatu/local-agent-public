"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");
const read = name => fs.readFileSync(require.resolve("./" + name), "utf8");

function harness() {
  const values = {}, alarms = new Map(), calls = [];
  let now = 10000000;
  let activeUrl = "https://chatgpt.com/c/parent-1";
  let tabs = [{ id: 4, url: activeUrl }];
  let sendReply = { ok: true, submitted: true };
  const storage = {
    get: async name => ({ [name]: values[name] }),
    set: async patch => Object.assign(values, structuredClone(patch))
  };
  const chrome = {
    storage: { local: storage },
    alarms: {
      create: async (name, options) => alarms.set(name, options.when),
      clear: async name => alarms.delete(name)
    },
    tabs: {
      query: async filter => filter.active ? [{ id: 4, url: activeUrl }] : tabs,
      sendMessage: async (_id, message) => { calls.push(message); return sendReply; }
    },
    scripting: { executeScript: async (_args) => undefined }
  };
  const ctx = vm.createContext({ chrome, crypto, URL, console, Date: { now: () => now },
    normalizeConversationUrl: raw => /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]+$/.test(raw) ? raw : "" });
  vm.runInContext(read("auto_wake.js"), ctx);
  return { ctx, values, alarms, calls,
    setNow: value => { now = value; },
    setActive: value => { activeUrl = value; },
    setTabs: value => { tabs = value; },
    setReply: value => { sendReply = value; } };
}
const settings = { intervalMinutes: 10, prompt: "Check GitHub and continue." };

test("opt-in target selection, interval and stop are independent of GitHub pairing", async () => {
  const h = harness();
  assert.equal((await h.ctx.autoWakeStatus()).enabled, false);
  h.setActive("https://evil.example/c/parent-1");
  assert.equal((await h.ctx.autoWakeStart(settings)).reason, "open_target_chatgpt_chat");
  h.setActive("https://chatgpt.com/c/parent-1");
  assert.equal((await h.ctx.autoWakeStart({ ...settings, intervalMinutes: 1 })).ok, false);
  assert.equal((await h.ctx.autoWakeStart({ ...settings, prompt: " " })).ok, false);
  const started = await h.ctx.autoWakeStart(settings);
  assert.equal(started.enabled, true);
  assert.equal(started.conversationUrl, "https://chatgpt.com/c/parent-1");
  assert.equal(h.alarms.get("local-agent-optional-auto-wake"), 10000000 + 600000);
  await h.ctx.autoWakeStop();
  assert.equal((await h.ctx.autoWakeStatus()).enabled, false);
  assert.equal(h.alarms.size, 0);
});

test("one due alarm sends once through shared composer and schedules the next interval", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.setNow(10600000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].conversationUrl, "https://chatgpt.com/c/parent-1");
  assert.equal(h.calls[0].text, settings.prompt);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "sent");
  assert.equal(h.alarms.get("local-agent-optional-auto-wake"), 11200000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 1, "early duplicate alarm does not resend");
});

test("unavailable target skips, and unknown Send pauses without replay on restart", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.setTabs([]);
  h.setNow(10600000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 0);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "chat_not_open");
  h.setTabs([{ id: 4, url: "https://chatgpt.com/c/parent-1" }]);
  h.setReply({ ok: false, reason: "send_click_unknown" });
  h.setNow(11200000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 1);
  assert.equal((await h.ctx.autoWakeStatus()).enabled, false);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "paused_send_click_unknown");
  await h.ctx.autoWakeInitialize();
  assert.equal(h.alarms.size, 0);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 1);
});

test("restart skips overdue alarm and crash during Send fails closed", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.setNow(20000000);
  await h.ctx.autoWakeInitialize();
  assert.equal(h.alarms.get("local-agent-optional-auto-wake"), 20600000);
  h.setNow(20600000);
  h.ctx.chrome.storage.local.set = async patch => {
    Object.assign(h.values, structuredClone(patch));
    if (patch.localAgentOptionalAutoWakeV1?.lastStatus === "sending") {
      throw Error("simulated persistent-write acknowledgement lost");
    }
  };
  await assert.rejects(() => h.ctx.autoWakeOnAlarm(), /simulated/);
  assert.equal(h.values.localAgentOptionalAutoWakeV1.enabled, false);
  assert.equal(h.calls.length, 0);
});

test("duplicate matching tabs never cause an ambiguous browser Send", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.setTabs([{ id: 4, url: "https://chatgpt.com/c/parent-1" },
    { id: 9, url: "https://chatgpt.com/c/parent-1" }]);
  h.setNow(10600000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal(h.calls.length, 0);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "chat_ambiguous");
});

test("Auto Wake content only accepts an exact owned tab and consumes each ID once", async () => {
  const values = new Map(), messages = [];
  let listener;
  const context = vm.createContext({
    globalThis: {}, location: { href: "https://chatgpt.com/c/parent-1" },
    sessionStorage: { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v) },
    chrome: { runtime: { onMessage: { addListener: cb => { listener = cb; } } } }
  });
  let draft = "";
  context.globalThis.LocalAgentGithubFirstComposerV1 = {
    findComposer: () => ({ textContent: draft }),
    submitOnce: async ({ beforeMutation, beforeClick, phase, routeAllowed }) => {
      assert.equal(routeAllowed(), true);
      beforeMutation(); beforeClick(); phase("composer_cleared_after_click");
      messages.push("sent");
      return { ok: true, submitted: true, reason: "composer_cleared_after_click" };
    }
  };
  vm.runInContext(read("auto_wake_content.js"), context);
  const message = { type: "bridge:optional-auto-wake", attemptId: crypto.randomUUID(),
    conversationUrl: "https://chatgpt.com/c/parent-1", text: "Keep going" };
  const deliver = m => new Promise(resolve => listener(m, {}, resolve));
  assert.equal((await deliver(message)).submitted, true);
  assert.equal((await deliver(message)).ok, false);
  assert.deepEqual(messages, ["sent"]);
  draft = "Do not send my draft";
  assert.equal((await deliver({ ...message, attemptId: crypto.randomUUID() })).reason, "composer_busy");
  assert.deepEqual(messages, ["sent"], "an operator draft must never be submitted");
  draft = "";
  assert.equal((await deliver({ ...message, attemptId: crypto.randomUUID(),
    conversationUrl: "https://chatgpt.com/c/other" })).ok, false);
});


test("corrupt persisted schedule pauses safely instead of creating invalid alarms", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.values.localAgentOptionalAutoWakeV1.intervalMinutes = "ten";
  await h.ctx.autoWakeInitialize();
  assert.equal(h.alarms.size, 0);
  assert.equal((await h.ctx.autoWakeStatus()).enabled, false);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "invalid_configuration");
});

test("a pre-existing composer is skipped without an uncertain Send claim", async () => {
  const h = harness();
  await h.ctx.autoWakeStart(settings);
  h.setReply({ ok: false, reason: "composer_busy" });
  h.setNow(10600000);
  await h.ctx.autoWakeOnAlarm();
  assert.equal((await h.ctx.autoWakeStatus()).enabled, true);
  assert.equal((await h.ctx.autoWakeStatus()).lastStatus, "composer_busy");
  assert.equal(h.alarms.get("local-agent-optional-auto-wake"), 11200000);
});
