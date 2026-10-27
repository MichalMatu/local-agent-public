"use strict";

const assert = require("node:assert/strict");
const model = require("./github_fabric_dispatch_model.js");
const journal = require("./github_fabric_private_batch_journal.js");

const PARENT = "https://chatgpt.com/c/private-batch-parent";
const CHILD_A = "https://chatgpt.com/c/child-batch-a";
const CHILD_B = "https://chatgpt.com/c/child-batch-b";
const SHA = "a".repeat(40);
const OWNER_A = "1".repeat(32);
const OWNER_B = "2".repeat(32);
const data = {
  schema_version: 1, operation: "delegate",
  id: "fabric-" + "3".repeat(32),
  request_id: "request-batch-1", request_digest: "sha256:" + "4".repeat(64),
  campaign_id: "cf-" + "5".repeat(16), parent_conversation_url: PARENT,
  children: ["research", "verification"].map((role, index) => ({
    id: "child-" + (index + 1),
    request_id: "request-" + (index + 1), role,
    spawn: {
      schema_version: 1, transaction_id: "spawn-" + String(index + 6).repeat(64),
      child_request_digest: "sha256:" + "8".repeat(64),
      bootstrap_digest: "sha256:" + "9".repeat(64),
      bootstrap_text: "PRIVATE CHILD CONTENT MUST NEVER APPEAR IN JOURNAL"
    }
  }))
};

function memoryStorage(store = {}) {
  const stats = { writes: 0, reads: 0 };
  return {
    store, stats,
    async get(key) {
      stats.reads++;
      return Object.hasOwn(store, key)
        ? { [key]: structuredClone(store[key]) } : {};
    },
    async set(value) {
      stats.writes++;
      Object.assign(store, structuredClone(value));
    }
  };
}

async function test() {
  model.validateDispatch(data);
  const storage = memoryStorage();
  let v = journal.initializeJournal(data, SHA);
  assert.equal(v.children.length, 2);
  assert.equal(v.revision, 0);
  assert.equal(journal.eligibleChild(v, data), "request-1");
  assert.equal((await journal.persist(storage, data, v)).revision, 0);
  assert.ok(!JSON.stringify(storage.store).includes("PRIVATE CHILD CONTENT"));
  assert.deepEqual(journal.recoveryPlan(v, data).children.map(c => c.action),
    ["operator_arm_required", "operator_arm_required"]);

  const original = v;
  v = journal.transition(v, data, "request-1", "claim_intent", { owner_id: OWNER_A });
  assert.equal(original.children[0].phase, "pending", "journal transition is immutable");
  await journal.persist(storage, data, v, original);
  assert.equal(journal.eligibleChild(v, data), null);
  assert.equal(journal.recoveryPlan(v, data).children[0].action, "claim_reconciliation_required");
  await assert.rejects(journal.persist(storage, data, original, null), /already exists/);
  assert.throws(() =>
    journal.transition(v, data, "request-1", "claim_intent", { owner_id: OWNER_A }),
  /no replay/);
  assert.throws(() =>
    journal.transition(v, data, "request-2", "claim_intent", { owner_id: OWNER_B }),
  /previous child unresolved/);

  let old = v;
  v = journal.transition(v, data, "request-1", "claimed");
  await journal.persist(storage, data, v, old);
  old = v;
  v = journal.transition(v, data, "request-1", "tab_open_intent");
  await journal.persist(storage, data, v, old);
  assert.equal(journal.recoveryPlan(v, data).children[0].action, "manual_tab_reconciliation_required");
  // A tab may have been created before the worker suspended. Never open a
  // second tab from the tab-open-intent state without reconciliation.
  assert.throws(() => journal.transition(v, data, "request-1", "tab_open_intent"), /no replay/);
  old = v;
  v = journal.transition(v, data, "request-1", "tab_ready", { tab_id: 123 });
  await journal.persist(storage, data, v, old);
  assert.equal(journal.recoveryPlan(v, data).children[0].action,
    "manual_pre_send_authorization_required");

  // This durable state is saved BEFORE calling any composer/UI Send.
  old = v;
  v = journal.transition(v, data, "request-1", "submission_unknown");
  await journal.persist(storage, data, v, old);
  // Simulate MV3 worker teardown and cold token loss; only local storage
  // remains. No action is authorized to send the old bootstrap a second time.
  const restarted = memoryStorage(storage.store);
  v = await journal.load(restarted, data);
  assert.equal(journal.recoveryPlan(v, data).children[0].action, "send_unknown_no_replay");
  assert.throws(() => journal.transition(v, data, "request-1", "submission_unknown"), /no replay/);
  assert.throws(() => journal.transition(v, data, "request-2", "claim_intent", {
    owner_id: OWNER_B
  }), /previous child unresolved/);
  assert.equal(restarted.stats.writes, 0);

  old = v;
  v = journal.transition(v, data, "request-1", "ack_pending", {
    child_conversation_url: CHILD_A
  });
  await journal.persist(storage, data, v, old);
  assert.equal(journal.recoveryPlan(v, data).children[0].action, "receipt_only_reconciliation");
  old = v;
  v = journal.transition(v, data, "request-1", "running");
  await journal.persist(storage, data, v, old);
  old = v;
  v = journal.transition(v, data, "request-1", "result_pending");
  await journal.persist(storage, data, v, old);
  old = v;
  v = journal.transition(v, data, "request-1", "completed", {
    result_path: journal.resultPath(data, "request-1")
  });
  await journal.persist(storage, data, v, old);
  assert.equal(journal.eligibleChild(v, data), "request-2");
  assert.equal(journal.recoveryPlan(v, data).children[0].action, "terminal");

  // The second child has a distinct immutable identity, transaction and owner.
  old = v;
  v = journal.transition(v, data, "request-2", "claim_intent", { owner_id: OWNER_B });
  await journal.persist(storage, data, v, old);
  for (const [phase, evidence] of [
    ["claimed", {}], ["tab_open_intent", {}], ["tab_ready", { tab_id: 124 }],
    ["submission_unknown", {}], ["ack_pending", { child_conversation_url: CHILD_B }],
    ["running", {}], ["result_pending", {}],
    ["completed", { result_path: journal.resultPath(data, "request-2") }]
  ]) {
    old = v;
    v = journal.transition(v, data, "request-2", phase, evidence);
    await journal.persist(storage, data, v, old);
  }
  assert.deepEqual(v.children.map(c => c.phase), ["completed", "completed"]);
  assert.equal(journal.eligibleChild(v, data), null);
  assert.equal(journal.recoveryPlan(await journal.load(storage, data), data).revision, v.revision);

  const invalid = structuredClone(v);
  invalid.children[1].child_conversation_url = CHILD_A;
  assert.throws(() => journal.validateJournal(invalid, data), /duplicated/);
  invalid.children[1].child_conversation_url = CHILD_B;
  invalid.children[1].tab_id = 123;
  assert.throws(() => journal.validateJournal(invalid, data), /tab identity invalid/);
  const corrupt = structuredClone(v);
  corrupt.children[0].bootstrap_digest = "sha256:" + "0".repeat(64);
  assert.throws(() => journal.validateJournal(corrupt, data), /child evidence invalid/);
  assert.throws(() => journal.validateJournal({ ...v, extra: "injected" }, data),
    /header invalid/);
  assert.throws(() => journal.transition(v, data, "missing", "claim_intent"), /not admitted/);
  assert.throws(() => journal.transition(v, data, "request-2", "completed", {
    result_path: journal.resultPath(data, "request-2")
  }), /no replay/);

  // Do not let a caller bypass the transition function with a forged
  // yet internally well-shaped later journal snapshot.
  const forged = structuredClone(v);
  forged.revision++;
  forged.children[0].child_conversation_url = "https://chatgpt.com/c/forged-same-child";
  assert.doesNotThrow(() => journal.validateJournal(forged, data));
  assert.equal(forged.revision, v.revision + 1);
  await assert.rejects(journal.persist(storage, data, forged, v), /no replay/);
  const duplicateOwner = structuredClone(v);
  duplicateOwner.children[1].owner_id = OWNER_A;
  assert.throws(() => journal.validateJournal(duplicateOwner, data), /owner identity invalid or duplicated/);

  // Storage failure never authorizes continuing with an unpersisted effect.
  const neverCommitted = journal.initializeJournal(data, SHA);
  const broken = memoryStorage();
  broken.set = async () => { throw Error("storage write unavailable"); };
  await assert.rejects(journal.persist(broken, data, neverCommitted), /write unavailable/);

  // A crash AFTER a storage write that reports failure remains fenced on restart.
  const uncertain = memoryStorage();
  uncertain.set = async value => {
    Object.assign(uncertain.store, structuredClone(value));
    throw Error("lost storage ACK");
  };
  await assert.rejects(journal.persist(uncertain, data, neverCommitted), /lost storage ACK/);
  assert.equal((await journal.load(memoryStorage(uncertain.store), data)).revision, 0);

  const partial = journal.initializeJournal(data, SHA);
  const started = journal.transition(partial, data, "request-1", "claim_intent", {
    owner_id: OWNER_A
  });
  const ambiguous = journal.transition(started, data, "request-1", "claim_ambiguous");
  assert.equal(journal.eligibleChild(ambiguous, data), null,
    "a claim ambiguity cannot silently advance to the next child");
  const paused = journal.transition(partial, data, "request-1", "claim_intent", {
    owner_id: OWNER_A
  });
  const claimed = journal.transition(paused, data, "request-1", "claimed");
  const opening = journal.transition(claimed, data, "request-1", "tab_open_intent");
  const blocked = journal.transition(opening, data, "request-1", "blocked");
  assert.equal(journal.eligibleChild(blocked, data), null);
  const ready = journal.transition(opening, data, "request-1", "tab_ready", { tab_id: 177 });
  const sent = journal.transition(ready, data, "request-1", "submission_unknown");
  const abandoned = journal.transition(sent, data, "request-1", "abandoned");
  assert.equal(journal.eligibleChild(abandoned, data), null,
    "abandonment must never make other children automatic");

  console.log("Private Fabric candidate batch journal v2 crash/restart/no-replay tests passed.");
}

test().catch(error => { console.error(error); process.exitCode = 1; });
