"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { test } = require("node:test");

function launch() {
  const bootstrap_text = "Publish your completion via the GitHub Connector; never through DOM.";
  return {
    schema_version: 1, dispatch_id: "fabric-" + "a".repeat(32), child_request_id: "child-1",
    intent: {
      schema_version: 1, transaction_id: "spawn-" + "b".repeat(64),
      child_request_digest: "sha256:" + "c".repeat(64),
      bootstrap_digest: "sha256:" + crypto.createHash("sha256").update(bootstrap_text).digest("hex"),
      bootstrap_text, tab_id: null
    }
  };
}
function harness() {
  const data = {};
  const store = {
    get: async key => key === null ? { ...data } : { [key]: data[key] },
    set: async values => { Object.assign(data, values); }
  };
  const calls = [];
  const context = vm.createContext({
    console, URL, AbortController, setTimeout, clearTimeout,
    chrome: { storage: { local: store }, tabs: { get: async () => ({ url: "https://chatgpt.com/c/child-1" }) } },
    validateConversationSpawnBrowserIntent: value => assert.equal(value.schema_version, 1),
    requireConversationSpawnBootstrapDigest: async value => assert.equal(
      "sha256:" + crypto.createHash("sha256").update(value.bootstrap_text).digest("hex"), value.bootstrap_digest
    ),
    createConversationSpawnTab: async () => ({ ok: true, tabId: 10 }),
    submitConversationSpawnBootstrap: async () => { calls.push("submit"); return { ok: true, childConversationUrl: "https://chatgpt.com/c/child-1" }; },
    ensureConversationSpawnContent: async () => ({ ok: true }),
    normalizeConversationUrl: url => /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]+$/.test(url) ? url : "",
    conversationSpawnTabClaimMatches: async () => true
  });
  const code = fs.readFileSync(require.resolve("./worker_github_first_local_bridge.js"), "utf8");
  vm.runInContext(code, context);
  return { context, data, store, calls };
}
test("new path injects once and never reads assistant DOM text", async () => {
  const h = harness();
  const item = launch();
  const first = await h.context.githubFirstLocalRunLaunch(item);
  assert.equal(first.status, "ack_pending");
  assert.deepEqual(h.calls, ["submit"]);
  const second = await h.context.githubFirstLocalRunLaunch(item);
  assert.equal(second.status, "already_consumed");
  assert.deepEqual(h.calls, ["submit"]);
  assert.equal(h.data["githubFirstLocalSubmitV1:" + item.intent.transaction_id].phase, "ack_pending");
});
test("unknown Submit blocks replay through worker reload", async () => {
  const h = harness();
  const item = launch();
  await h.context.githubFirstLocalRunLaunch(item, {
    submit: async () => { h.calls.push("submit"); throw new Error("lost response"); }
  });
  const afterReload = harness();
  const persisted = h.data["githubFirstLocalSubmitV1:" + item.intent.transaction_id];
  await afterReload.store.set({ ["githubFirstLocalSubmitV1:" + item.intent.transaction_id]: persisted });
  assert.equal((await afterReload.context.githubFirstLocalRunLaunch(item)).status, "already_consumed");
  assert.equal(afterReload.calls.length, 0);
});
test("collision and invalid bootstrap fail before browser effect", async () => {
  const h = harness();
  const first = launch();
  first.intent.bootstrap_digest = "sha256:" + "d".repeat(64);
  await assert.rejects(() => h.context.githubFirstLocalRunLaunch(first), /equal/);
  assert.equal(h.calls.length, 0);
  const item = launch();
  await h.context.githubFirstLocalRunLaunch(item);
  item.dispatch_id = "fabric-" + "f".repeat(32);
  await assert.rejects(() => h.context.githubFirstLocalRunLaunch(item), /collision/);
});
test("released browser gate requires an actual paired key; does not launch without it", async () => {
  const h = harness();
  assert.equal((await h.context.githubFirstLocalPollOnce({ allowSend: true })).status, "unpaired");
  assert.equal(h.calls.length, 0);
});

test("parent wake injects only an indexed GitHub pointer and never retries", async () => {
  const h = harness();
  h.context.chrome.tabs.query = async () => [{ id: 32, url: "https://chatgpt.com/c/parent-1" }];
  const wake = { kind: "parent_wake", schema_version: 1,
    wake_id: "wake-" + "9".repeat(64),
    dispatch_id: "fabric-" + "a".repeat(32),
    parent_url: "https://chatgpt.com/c/parent-1", child_request_ids: ["child-1", "child-2"] };
  let submissions = 0;
  const send = async ({ text }) => {
    submissions++;
    assert.match(text, /fabric-data/);
    assert.doesNotMatch(text, /assistant_text|DOM scrape/i);
    return { ok: true, submitted: true };
  };
  assert.equal((await h.context.githubFirstLocalRunParentWake(wake, { send })).status, "ack_pending");
  assert.equal((await h.context.githubFirstLocalRunParentWake(wake, { send })).status, "already_consumed");
  assert.equal(submissions, 1);
});

test("select parent reads only active browser URL and posts it through authenticated localhost", async () => {
  const h = harness();
  const parent = "https://chatgpt.com/c/6acbb714-b818-83ed-9c99-138c5bbbf7d9";
  let posted = null;
  await h.store.set({ githubFirstLocalBridgeKeyV1: "K".repeat(48) });
  h.context.chrome.tabs.query = async () => [{ id: 12, url: parent }];
  h.context.fetch = async (url, opts) => {
    assert.equal(url, "http://127.0.0.1:8768/v1/parent");
    assert.equal(opts.headers["X-Local-Agent-Bridge-Key"], "K".repeat(48));
    posted = JSON.parse(opts.body);
    return { ok: true, redirected: false, text: async () => JSON.stringify({ ok: true, parent_url: parent }) };
  };
  const response = await h.context.githubFirstLocalSelectParent();
  assert.equal(response.ok, true);
  assert.deepEqual(posted, { parent_url: parent });
  h.context.chrome.tabs.query = async () => [{ id: 12, url: "https://evil.test/" }];
  assert.equal((await h.context.githubFirstLocalSelectParent()).ok, false);
});
