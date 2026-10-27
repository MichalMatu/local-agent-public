"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function harness(outcomes, sharedSession = {}) {
  const calls = [];
  const stored = {};
  const context = vm.createContext({
    console, URL, AbortController, setTimeout, clearTimeout,
    chrome: { storage: { local: {
      get: async key => key === null ? { ...stored } : ({ [key]: stored[key] }),
      set: async changes => Object.assign(stored, changes)
    }, session: {
      get: async key => ({ [key]: sharedSession[key] }),
      set: async changes => Object.assign(sharedSession, changes)
    } } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      const result = outcomes.shift() || { ok: true, launch: null };
      if (result instanceof Error) throw result;
      return { ok: true, redirected: false, json: async () => result, text: async () => JSON.stringify(result) };
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "worker_github_first_local_bridge.js"), "utf8"), context);
  return { context, stored, calls, session: sharedSession };
}

test("operator approved pairing writes secret to Chrome storage, not popup response", async () => {
  const ticket = "T".repeat(43);
  const secret = "S".repeat(64);
  const h = harness([{ ok: true, ticket, code: "031415" },
    { ok: true, status: "pending" }, { ok: true, status: "paired", key: secret }]);
  const start = await h.context.githubFirstLocalPairStart();
  assert.equal(start.code, "031415");
  assert.equal(start.ticket, ticket);
  assert.equal(await h.context.githubFirstLocalPairFinish(ticket).then(x => x.status), "pending");
  const completed = await h.context.githubFirstLocalPairFinish(ticket);
  assert.equal(completed.paired, true);
  assert.equal(JSON.stringify(completed).includes(secret), false);
  assert.equal(h.stored.githubFirstLocalBridgeKeyV1, secret);
  assert.deepEqual(h.calls.map(x => new URL(x.url).pathname),
    ["/v1/pair/start", "/v1/pair/finish", "/v1/pair/finish"]);
  assert.equal(h.calls.every(x => x.options.credentials === "omit" && x.options.redirect === "error"), true);
  assert.equal((await h.context.githubFirstLocalPollOnce({ allowSend: true })).status, "idle");
});

test("denied or unavailable pairing stores nothing and rejects invalid tickets", async () => {
  const h = harness([{ ok: true, ticket: "R".repeat(43), code: "999999" },
    { ok: false, status: "denied" }]);
  const start = await h.context.githubFirstLocalPairStart();
  assert.equal((await h.context.githubFirstLocalPairFinish(start.ticket)).status, "denied");
  assert.deepEqual(h.stored, {});
  await assert.rejects(() => h.context.githubFirstLocalPairFinish("invalid"), /Invalid pairing ticket/);
  const offline = harness([new Error("offline")]);
  await assert.rejects(() => offline.context.githubFirstLocalPairStart(), /offline/);
});


test("native approval survives popup closure and MV3 worker restart", async () => {
  const savedSession = {};
  const ticket = "P".repeat(43);
  const secret = "K".repeat(64);
  const starting = harness([{ ok: true, ticket, code: "001201" }], savedSession);
  assert.equal((await starting.context.githubFirstLocalPairStart()).code, "001201");
  // Simulate native macOS dialog stealing focus and destroying the popup/worker.
  const resumed = harness([
    { ok: true, status: "pending" },
    { ok: true, status: "paired", key: secret }
  ], savedSession);
  const retry = await resumed.context.githubFirstLocalPairStart();
  assert.equal(retry.resumed, true);
  assert.equal(retry.ticket, ticket);
  assert.equal(resumed.calls.length, 0);
  const waiting = await resumed.context.githubFirstLocalPairReconcile();
  assert.equal(waiting.status, "pending");
  assert.equal(waiting.code, "001201");
  const done = await resumed.context.githubFirstLocalPairReconcile();
  assert.equal(done.status, "paired");
  assert.equal(resumed.stored.githubFirstLocalBridgeKeyV1, secret);
  assert.equal(savedSession.githubFirstPairSessionV1, null);
  assert.equal(JSON.stringify(done).includes(secret), false);
  assert.equal((await resumed.context.githubFirstLocalPollOnce({ allowSend: true })).status, "idle");
});

test("denied native approval is distinct from expiration and never stores a key", async () => {
  const h = harness([{ ok: true, ticket: "R".repeat(43), code: "999999" },
    { ok: false, status: "denied" }]);
  await h.context.githubFirstLocalPairStart();
  assert.equal((await h.context.githubFirstLocalPairReconcile()).status, "denied");
  assert.equal((await h.context.githubFirstLocalPairReconcile()).status, "denied");
  assert.equal(h.stored.githubFirstLocalBridgeKeyV1, undefined);
  assert.equal(h.session.githubFirstPairSessionV1.status, "denied");
});
