"use strict";

const assert = require("node:assert/strict");
const model = require("./github_fabric_intake_model.js");
const runtimeUrl =
  "https://raw.githubusercontent.com/example-owner/local-agent/chat-bridge-state/chat_bridge/runtime.json";
const id = "fabric-" + "a".repeat(32);
const next = "fabric-" + "b".repeat(32);
const digest = "c".repeat(64);

assert.equal(
  model.indexUrl(runtimeUrl),
  "https://raw.githubusercontent.com/example-owner/local-agent/chat-bridge-state/.agent/conversation/browser_dispatches/index.json"
);
assert.equal(
  model.dispatchUrl(runtimeUrl, id),
  "https://raw.githubusercontent.com/example-owner/local-agent/chat-bridge-state/.agent/conversation/browser_dispatches/" + id + ".json"
);

assert.deepEqual(
  model.validateIndex({ schema_version: 1, dispatch_ids: [id, next] }),
  { schema_version: 1, dispatch_ids: [id, next] }
);
assert.deepEqual(model.validateIndex({ schema_version: 1, dispatch_ids: [] }).dispatch_ids, []);
for (const invalid of [
  { schema_version: 2, dispatch_ids: [] },
  { schema_version: 1, dispatch_ids: [id, id] },
  { schema_version: 1, dispatch_ids: ["../secrets"] },
  { schema_version: 1, dispatch_ids: [id, next, id, next, id] },
  { schema_version: 1, dispatch_ids: [], repository: "execution" }
]) {
  assert.throws(() => model.validateIndex(invalid));
}
assert.throws(() => model.dispatchUrl(runtimeUrl, "../escape"));
assert.throws(() => model.indexUrl(runtimeUrl + "?token=secret"));
assert.throws(() => model.indexUrl(runtimeUrl.replace("example-owner/local-agent", "other/repository")));
assert.throws(() => model.indexUrl(runtimeUrl.replace("chat-bridge-state", "main")));

const admitted = model.reconcileSeenDispatch({}, id, digest);
assert.equal(admitted.status, "stored");
assert.deepEqual(admitted.seen, { [id]: digest });
const replayed = model.reconcileSeenDispatch(admitted.seen, id, digest);
assert.equal(replayed.status, "replayed");
assert.strictEqual(replayed.seen, admitted.seen);
const conflict = model.reconcileSeenDispatch(admitted.seen, id, "d".repeat(64));
assert.equal(conflict.status, "conflict");
assert.strictEqual(conflict.seen, admitted.seen);
const full = Object.fromEntries(
  Array.from({ length: model.MAX_SEEN_DISPATCHES }, (_, index) => [
    "fabric-" + index.toString(16).padStart(32, "0"),
    digest
  ])
);
assert.equal(model.reconcileSeenDispatch(full, id, digest).status, "capacity");
assert.deepEqual(full, model.reconcileSeenDispatch(full, id, digest).seen);
assert.throws(() => model.reconcileSeenDispatch({}, id, "BAD"));
assert.throws(() => model.reconcileSeenDispatch({ invalid: digest }, id, digest));

console.log("GitHub Fabric intake model tests passed.");
