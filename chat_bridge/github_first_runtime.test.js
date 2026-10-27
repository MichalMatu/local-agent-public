"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const read = name => fs.readFileSync(path.join(__dirname, name), "utf8");
const manifest = JSON.parse(read("manifest.json"));
const production = read("service_worker.js");

test("production bridge has only GitHub-first transport and no DOM answer reader", () => {
  assert.equal(manifest.version, "0.9.8");
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.host_permissions.includes("https://api.github.com/*"), false);
  assert.equal(manifest.host_permissions.includes("http://127.0.0.1:8768/*"), true);
  assert.match(production, /worker_github_first_local_bridge\.js/);
  assert.match(production, /worker_github_first_events\.js/);
  for (const forbidden of [
    "worker_conversation_fabric", "worker_github_fabric_private_live",
    "conversation_fabric_content", "worker_delivery", "worker_schedule",
    "worker_github_fabric_intake", "worker_github_control"
  ]) assert.doesNotMatch(production, new RegExp(forbidden));
  assert.doesNotMatch(read("worker_github_first_local_bridge.js"), /stableConversationFabricResult|assistantText|assistantIdentity/);
  assert.match(read("worker_spawn.js"), /files: \["github_first_composer\.js", "spawn_content\.js"\]/);
  assert.match(read("worker_github_first_local_bridge.js"),
    /files: \["github_first_composer\.js", "github_first_prompt_content\.js"\]/);
  for (const source of [read("github_first_composer.js"), read("github_first_prompt_content.js")]) {
    assert.doesNotMatch(source, /data-message-author-role|assistant-message|assistantText/);
  }

});

test("extension starts without injecting DOM scripts or GitHub credentials", async () => {
  const loaded = [];
  const listeners = {};
  const values = {};
  const context = vm.createContext({
    console, URL, setTimeout, clearTimeout, AbortController,
    fetch: async (url, options) => {
      assert.equal(url, "http://127.0.0.1:8768/v1/status");
      assert.equal(options.headers["X-Local-Agent-Bridge-Key"], "Z".repeat(48));
      return { ok: true, redirected: false, text: async () => JSON.stringify({ ok: true, items: {} }) };
    },
    chrome: {
      runtime: {
        id: "valid-extension", getURL: p => "chrome-extension://valid-extension/" + p,
        getManifest: () => manifest,
        onInstalled: { addListener: callback => { listeners.installed = callback; } },
        onStartup: { addListener: callback => { listeners.startup = callback; } },
        onMessage: { addListener: callback => { listeners.message = callback; } },
      },
      alarms: { create: async () => true, clear: async () => true, onAlarm: { addListener: callback => { listeners.alarm = callback; } } },
      storage: {
        local: {
          get: async key => key === null ? { ...values } : { [key]: values[key] },
          set: async patch => { Object.assign(values, patch); }
        },
        session: { get: async () => ({}), set: async () => undefined }
      }
    }
  });
  context.importScripts = (...names) => {
    for (const name of names) { loaded.push(name); vm.runInContext(read(name), context); }
  };
  vm.runInContext(production, context);
  assert.equal(loaded.length, 6); // GitHub-first transport plus optional local timer.
  assert.equal(typeof listeners.alarm, "function");
  assert.equal(typeof listeners.message, "function");
  assert.equal(loaded.some(x => /conversation_fabric_(worker|content)|worker_delivery|worker_conversation_fabric/.test(x)), false);
  assert.equal(loaded.includes("worker_spawn.js"), true);
  assert.equal(loaded.includes("auto_wake.js"), true);
  assert.equal(loaded.includes("github_first_prompt_content.js"), false);
  await new Promise(resolve => {
    listeners.message({ type: "bridge:github-first-local-pair", key: "Z".repeat(48) },
      { id: "valid-extension", url: "chrome-extension://valid-extension/popup.html" },
      reply => { assert.equal(reply.ok, true); resolve(); });
  });
  assert.equal(values.githubFirstLocalBridgeKeyV1, "Z".repeat(48));
  await new Promise(resolve => {
    listeners.message({ type: "bridge:github-first-local-status" },
      { id: "valid-extension", url: "chrome-extension://valid-extension/popup.html" },
      reply => { assert.equal(reply.paired, true); assert.equal(reply.reachable, true); assert.equal(reply.sendReleased, true); resolve(); });
  });
});
