"use strict";

const assert = require("node:assert/strict");
const cryptoModule = require("node:crypto");
const fs = require("node:fs");
if (!globalThis.crypto?.subtle) globalThis.crypto = cryptoModule.webcrypto;
const reader = require("./github_fabric_private_parent_ownership_reader.js");
const effects = require("./github_fabric_private_parent_effects.js");
const API = "https://api.github.com/repos/example-owner/local-agent-fabric-private";
const BASE = "projects/local-agent/parent_ownership/";
const PARENT = "https://chatgpt.com/c/parent-candidate-001";
const DISPATCH_1 = "fabric-" + "a".repeat(32);
const DISPATCH_2 = "fabric-" + "b".repeat(32);
const OWNER_1 = "1".repeat(32);
const OWNER_2 = "2".repeat(32);
const TOKEN = "private_scoped_token_for_synthetic_tests_123";
const HEAD = "a".repeat(40);
const TREE = "b".repeat(40);
const hash = value => cryptoModule.createHash("sha256").update(value).digest("hex");
const PARENT_ID = "parent-" + hash(JSON.stringify([
  "fabric-global-parent-mode-v1", PARENT
])).slice(0, 32);

function shaBlob(text) {
  const bytes = Buffer.from(text, "utf8");
  return cryptoModule.createHash("sha1").update(
    Buffer.concat([Buffer.from("blob " + bytes.length + "\0"), bytes])
  ).digest("hex");
}
function jsonText(value) { return JSON.stringify(value) + "\n"; }
function response(value, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function parent(epoch, phase, dispatchId, ownerId) {
  return {
    schema_version: 1, kind: "private_parent_ownership_candidate_v1",
    id: PARENT_ID, parent_conversation_url: PARENT,
    fence_epoch: epoch, owner_id: ownerId, dispatch_id: dispatchId,
    phase, completion: ({ active: "none", completed: "verified",
      unknown_frozen: "unresolved" })[phase]
  };
}
function epochPath(epoch) {
  return BASE + "epochs/" + PARENT_ID + "/" + String(epoch).padStart(4, "0") + ".json";
}
function fixture(epoch = 1, phase = "active") {
  const records = new Map();
  const current = parent(epoch, phase, epoch === 1 ? DISPATCH_1 : DISPATCH_2,
    epoch === 1 ? OWNER_1 : OWNER_2);
  const history = {
    schema_version: 1, parent_id: PARENT_ID,
    entries: epoch === 1
      ? [{ epoch: 1, dispatch_id: DISPATCH_1, owner_id: OWNER_1 }]
      : [{ epoch: 1, dispatch_id: DISPATCH_1, owner_id: OWNER_1 },
        { epoch: 2, dispatch_id: DISPATCH_2, owner_id: OWNER_2 }]
  };
  records.set(BASE + "index.json", { schema_version: 1, parent_ids: [PARENT_ID] });
  records.set(BASE + PARENT_ID + ".json", current);
  records.set(BASE + "history/" + PARENT_ID + ".json", history);
  records.set(epochPath(1), parent(1, "active", DISPATCH_1, OWNER_1));
  if (epoch === 2) records.set(epochPath(2), parent(2, "active", DISPATCH_2, OWNER_2));
  const calls = [];
  const state = { records, blobs: new Map(), entries: [], calls, failedStatus: 0,
    head: HEAD, tree: TREE, treeTruncated: false, tamper: false };
  function resign() {
    state.blobs.clear();
    for (const [path, value] of state.records) {
      state.blobs.set(path, shaBlob(jsonText(value)));
    }
    const dirs = ["projects/local-agent/parent_ownership",
      "projects/local-agent/parent_ownership/history",
      "projects/local-agent/parent_ownership/epochs",
      "projects/local-agent/parent_ownership/epochs/" + PARENT_ID];
    if ([...state.records.keys()].some(path => path.startsWith(BASE + "effects/"))) {
      dirs.push(BASE + "effects");
    }
    state.entries = [
      ...dirs.map(path => ({ path, type: "tree", mode: "040000", sha: "f".repeat(40) })),
      ...[...state.blobs].map(([path, sha]) =>
        ({ path, type: "blob", mode: "100644", sha }))
    ];
  }
  resign();
  async function fetchImpl(url, options) {
    calls.push(url);
    assert.ok(url.startsWith(API + "/"));
    assert.equal(options.method, "GET");
    assert.equal(options.credentials, "omit");
    assert.equal(options.redirect, "error");
    assert.equal(options.cache, "no-store");
    assert.equal(options.headers.Authorization, "Bearer " + TOKEN);
    assert.ok(options.signal instanceof AbortSignal);
    if (state.failedStatus) return response({ message: "denied" }, state.failedStatus);
    if (url === API + "/git/ref/heads/fabric-data") {
      return response({ ref: "refs/heads/fabric-data",
        object: { type: "commit", sha: state.head } });
    }
    if (url === API + "/git/commits/" + HEAD) {
      return response({ tree: { sha: state.tree } });
    }
    if (url === API + "/git/trees/" + TREE + "?recursive=1") {
      return response({ sha: state.tree, truncated: state.treeTruncated, tree: state.entries });
    }
    const prefix = API + "/contents/";
    if (url.startsWith(prefix) && url.endsWith("?ref=" + HEAD)) {
      const path = url.slice(prefix.length, -("?ref=" + HEAD).length);
      const value = state.records.get(path);
      if (value === undefined) throw Error("unexpected Contents path");
      const bytes = Buffer.from(jsonText(value), "utf8");
      return response({
        type: "file", path, encoding: "base64",
        sha: state.blobs.get(path), size: bytes.length,
        content: bytes.toString("base64")
      });
    }
    throw Error("Unexpected GitHub request");
  }
  const read = (extra = {}) => reader.readPrivateParentOwnershipSnapshot({
    enabled: true, readToken: TOKEN, parentConversationUrl: PARENT,
    expectedOwnerId: epoch === 1 ? OWNER_1 : OWNER_2,
    expectedDispatchId: epoch === 1 ? DISPATCH_1 : DISPATCH_2,
    expectedEpoch: epoch, fetchImpl, ...extra
  });
  return { state, read, resign };
}

async function effectFixture(epoch = 1) {
  const f = fixture(epoch);
  const requestDigest = "sha256:" + "4".repeat(64);
  const requestId = "operator-request-001";
  const dispatchId = "fabric-" + hash(JSON.stringify([
    "github-fabric-dispatch-v1", requestDigest
  ])).slice(0, 32);
  const campaignId = "cf-" + hash(JSON.stringify([
    "github-fabric-campaign-v1", requestId, requestDigest, PARENT
  ])).slice(0, 16);
  const childRecords = ["child-request-001", "child-request-002"].map((requestId, i) => {
    const bootstrapText = "verified private bootstrap " + i;
    const childRequestDigest = "sha256:" + String(i + 5).repeat(64);
    return {
      id: "child-" + (i + 1), request_id: requestId,
      role: i ? "verification" : "research",
      spawn: {
        schema_version: 1,
        transaction_id: "spawn-" + hash(JSON.stringify([childRequestDigest, 1])),
        child_request_digest: childRequestDigest,
        bootstrap_digest: "sha256:" + hash(bootstrapText),
        bootstrap_text: bootstrapText
      }
    };
  });
  const dispatch = {
    schema_version: 1, operation: "delegate", id: dispatchId,
    request_id: "operator-request-001", request_digest: requestDigest,
    campaign_id: campaignId, parent_conversation_url: PARENT,
    children: childRecords
  };
  const expectedChildren = childRecords.map(child => ({
    child_request_id: child.request_id,
    spawn_transaction_id: child.spawn.transaction_id
  }));
  const owner = f.state.records.get(BASE + PARENT_ID + ".json");
  owner.dispatch_id = dispatchId;
  const history = f.state.records.get(BASE + "history/" + PARENT_ID + ".json");
  history.entries[epoch - 1].dispatch_id = dispatchId;
  f.state.records.get(epochPath(epoch)).dispatch_id = dispatchId;
  const scope = "projects/local-agent/workflows/workflow-001/dispatches/";
  const dispatchPath = scope + dispatchId + ".json";
  f.state.records.set("projects/index.json", {
    schema_version: 1, project_ids: ["local-agent"]
  });
  f.state.records.set("projects/local-agent/workflows/index.json", {
    schema_version: 1, workflow_ids: ["workflow-001"]
  });
  f.state.records.set(scope + "index.json", {
    schema_version: 1, dispatch_ids: [dispatchId]
  });
  f.state.records.set(dispatchPath, dispatch);
  const make = async (index, phase = "send_unknown") => ({
    effect_id: await effects.expectedEffectId(owner, expectedChildren[index]),
    child_request_id: expectedChildren[index].child_request_id,
    spawn_transaction_id: expectedChildren[index].spawn_transaction_id,
    phase, result_source_head_sha: phase === "result_verified" ? "c".repeat(40) : ""
  });
  const path = BASE + "effects/" + PARENT_ID + ".json";
  const ledger = {
    schema_version: 1, parent_id: PARENT_ID,
    parent_conversation_url: PARENT, fence_epoch: epoch,
    owner_id: owner.owner_id, dispatch_id: owner.dispatch_id,
    revision: 1, effects: [await make(0)]
  };
  f.state.records.set(path, ledger);
  f.resign();
  const read = (extra = {}) => f.read({ expectedDispatchId: dispatchId, ...extra });
  return { ...f, path, ledger, make, expectedChildren, read, dispatch, dispatchPath, scope };
}

async function run() {
  assert.equal(await reader.parentId(PARENT), PARENT_ID);
  const one = fixture();
  assert.equal((await one.read()).status, "matching_active_candidate");
  assert.equal((await one.read()).observed_epoch, 1);
  const approved = await one.read();
  assert.equal(approved.source_head_sha, HEAD);
  assert.equal(approved.parent_id, PARENT_ID);
  assert.equal(approved.browser_effects_permitted, false);
  assert.equal(approved.source, "authenticated_private_parent_candidate_observation");
  assert.equal(Object.keys(approved).includes("owner_id"), false);
  assert.ok(!JSON.stringify(approved).includes(TOKEN));
  assert.equal((await one.read({ expectedOwnerId: OWNER_2 })).status,
    "competing_or_stale_candidate");
  assert.equal((await one.read({ expectedEpoch: 2 })).status,
    "competing_or_stale_candidate");
  assert.equal((await one.read({ expectedDispatchId: DISPATCH_2 })).status,
    "competing_or_stale_candidate");
  const two = fixture(2);
  assert.equal((await two.read()).status, "matching_active_candidate");
  assert.equal((await two.read({ expectedOwnerId: OWNER_1 })).status,
    "competing_or_stale_candidate");
  assert.equal((await two.read({ expectedEpoch: 1 })).status,
    "competing_or_stale_candidate");
  assert.equal((await fixture(1, "completed").read()).status,
    "matching_completed_candidate");
  assert.equal((await fixture(2, "unknown_frozen").read()).status,
    "matching_frozen_candidate");

  const armed = await effectFixture();
  assert.equal((await armed.read()).status, "matching_active_candidate");
  const observedEffect = await armed.read();
  assert.equal(observedEffect.effect_status, "reconcile_only_no_send_replay");
  assert.equal(observedEffect.recorded_effects, 1);
  assert.equal(observedEffect.browser_effects_permitted, false);
  assert.equal(observedEffect.source_head_sha, HEAD);
  assert.ok(!JSON.stringify(observedEffect).includes("spawn-" + "1".repeat(64)),
    "returned evidence must be redacted");
  // Caller-supplied expectedChildren is not an input anymore. The private
  // reader independently derives identities from the SAME pinned dispatch.
  assert.equal((await armed.read({ expectedChildren: [] })).effect_status,
    "reconcile_only_no_send_replay");

  const badProjectIndex = await effectFixture();
  badProjectIndex.state.records.get("projects/index.json").project_ids = [];
  badProjectIndex.resign();
  await assert.rejects(badProjectIndex.read(), /dispatch not indexed/);
  const missingDispatchIndex = await effectFixture();
  missingDispatchIndex.state.records.delete(missingDispatchIndex.scope + "index.json");
  missingDispatchIndex.resign();
  await assert.rejects(missingDispatchIndex.read(), /dispatch tree path invalid/);
  const forgedDispatch = await effectFixture();
  forgedDispatch.dispatch.request_digest = "sha256:" + "0".repeat(64);
  forgedDispatch.resign();
  await assert.rejects(forgedDispatch.read(), /dispatch digest mismatch/);
  const forgedBootstrap = await effectFixture();
  forgedBootstrap.dispatch.children[0].spawn.bootstrap_text = "tampered bootstrap";
  forgedBootstrap.resign();
  await assert.rejects(forgedBootstrap.read(), /child digest mismatch/);
  const forgedChildIdentity = await effectFixture();
  forgedChildIdentity.dispatch.children[0].request_id = "different-private-child";
  forgedChildIdentity.resign();
  await assert.rejects(forgedChildIdentity.read(), /immutable child identity or phase invalid/);
  const tamperUnsignedDispatch = await effectFixture();
  tamperUnsignedDispatch.dispatch.children[0].spawn.bootstrap_text = "unsigned tamper";
  await assert.rejects(tamperUnsignedDispatch.read(), /pinned blob digest mismatch/);
  const competingBranch = await effectFixture();
  competingBranch.state.records.get(competingBranch.scope + "index.json")
    .dispatch_ids = ["fabric-" + "0".repeat(32)];
  competingBranch.resign();
  await assert.rejects(competingBranch.read(), /dispatch not indexed/);
  const duplicatedDispatchPath = await effectFixture();
  duplicatedDispatchPath.state.entries.push({
    ...duplicatedDispatchPath.state.entries.find(entry =>
      entry.path === duplicatedDispatchPath.dispatchPath)
  });
  await assert.rejects(duplicatedDispatchPath.read(), /dispatch tree path invalid/);

  const completedEffect = await effectFixture();
  completedEffect.ledger.effects[0] = await completedEffect.make(0, "result_verified");
  completedEffect.ledger.revision = 2;
  completedEffect.resign();
  assert.equal((await completedEffect.read()).effect_status, "verified_no_send_replay");
  completedEffect.ledger.effects.push(await completedEffect.make(1));
  completedEffect.ledger.revision = 3;
  completedEffect.resign();
  assert.equal((await completedEffect.read()).effect_status, "reconcile_only_no_send_replay");
  assert.equal((await completedEffect.read()).recorded_effects, 2);

  const frozenEffect = await effectFixture();
  frozenEffect.ledger.effects[0].phase = "frozen_unknown";
  frozenEffect.ledger.revision = 2;
  frozenEffect.resign();
  assert.equal((await frozenEffect.read()).effect_status, "blocked_unknown_no_takeover");
  frozenEffect.ledger.effects.push(await frozenEffect.make(1));
  frozenEffect.ledger.revision = 3;
  frozenEffect.resign();
  await assert.rejects(frozenEffect.read(), /immutable child identity or phase invalid/);

  const forgedEffect = await effectFixture();
  forgedEffect.ledger.effects[0].effect_id = "effect-" + "0".repeat(32);
  forgedEffect.resign();
  await assert.rejects(forgedEffect.read(), /immutable child identity or phase invalid/);
  const badRevision = await effectFixture();
  badRevision.ledger.revision = 2;
  badRevision.resign();
  await assert.rejects(badRevision.read(), /revision invalid/);
  const wrongEpochLedger = await effectFixture(2);
  wrongEpochLedger.ledger.fence_epoch = 1;
  wrongEpochLedger.resign();
  await assert.rejects(wrongEpochLedger.read(), /ledger header invalid/);
  const alteredBlob = await effectFixture();
  alteredBlob.ledger.revision = 2; // Unsigned Contents tamper.
  await assert.rejects(alteredBlob.read(), /pinned blob digest mismatch/);
  const duplicateEffectPath = await effectFixture();
  duplicateEffectPath.state.entries.push({
    ...duplicateEffectPath.state.entries.find(entry => entry.path === duplicateEffectPath.path)
  });
  await assert.rejects(duplicateEffectPath.read(), /unexpected or duplicate tree entry/);
  const noEffectDirectory = await effectFixture();
  noEffectDirectory.state.entries = noEffectDirectory.state.entries.filter(
    entry => entry.path !== BASE + "effects"
  );
  await assert.rejects(noEffectDirectory.read(), /effects directory missing/);

  const notEnabled = fixture();
  await assert.rejects(notEnabled.read({ enabled: false }), /default-disabled/);
  await assert.rejects(notEnabled.read({ readToken: null }), /read token invalid/);
  for (const url of ["http://chatgpt.com/c/id", PARENT + "/",
    PARENT + "?token=unsafe", "https://chatgpt.com/c/../escape"]) {
    await assert.rejects(notEnabled.read({ parentConversationUrl: url }),
      /expected identity invalid/);
  }
  await assert.rejects(notEnabled.read({ expectedOwnerId: [OWNER_1] }),
    /expected identity invalid/);
  await assert.rejects(notEnabled.read({ expectedEpoch: true }),
    /expected identity invalid/);
  await assert.rejects(notEnabled.read({ expectedDispatchId: "bad" }),
    /expected identity invalid/);
  assert.equal(notEnabled.state.calls.length, 0, "bad admission never uses GitHub");

  const missing = fixture();
  missing.state.records.clear();
  missing.state.entries = [];
  assert.equal((await missing.read()).status, "unregistered");

  const unrelated = fixture();
  unrelated.state.records.set(BASE + "index.json", {
    schema_version: 1, parent_ids: ["parent-" + "0".repeat(32)]
  });
  const other = "parent-" + "0".repeat(32);
  unrelated.state.records.delete(BASE + PARENT_ID + ".json");
  unrelated.state.records.delete(BASE + "history/" + PARENT_ID + ".json");
  unrelated.state.records.delete(epochPath(1));
  unrelated.state.records.set(BASE + other + ".json",
    { ...parent(1, "active", DISPATCH_1, OWNER_1), id: other });
  unrelated.state.records.set(BASE + "history/" + other + ".json", {
    schema_version: 1, parent_id: other, entries: [{
      epoch: 1, dispatch_id: DISPATCH_1, owner_id: OWNER_1
    }]
  });
  unrelated.resign();
  assert.equal((await unrelated.read()).status, "unregistered");

  async function invalid(mutator, error) {
    const f = fixture(2);
    mutator(f);
    await assert.rejects(f.read(), error);
  }
  await invalid(f => {
    f.state.records.get(BASE + "index.json").parent_ids.push(PARENT_ID);
    f.resign();
  }, /index invalid/);
  await invalid(f => {
    f.state.records.get(BASE + PARENT_ID + ".json").fence_epoch = 1;
    f.resign();
  }, /current owner conflicts/);
  await invalid(f => {
    f.state.records.get(BASE + PARENT_ID + ".json").phase = "completed";
    f.resign();
  }, /ownership record invalid/);
  await invalid(f => {
    f.state.records.get(BASE + "history/" + PARENT_ID + ".json")
      .entries[1].owner_id = OWNER_1;
    f.resign();
  }, /epoch identity conflict/);
  await invalid(f => {
    f.state.records.get(epochPath(1)).owner_id = OWNER_2;
    f.resign();
  }, /immutable epoch witness conflict/);
  await invalid(f => {
    f.state.records.delete(epochPath(1));
    f.resign();
  }, /epoch witness count conflict/);
  await invalid(f => {
    f.state.records.set(epochPath(3), parent(2, "active", DISPATCH_2, OWNER_2));
    f.resign();
  }, /epoch witness count conflict/);
  await invalid(f => {
    f.state.records.delete(BASE + "history/" + PARENT_ID + ".json");
    f.resign();
  }, /indexed record\/history missing/);
  await invalid(f => {
    f.state.records.set(BASE + "parent-" + "0".repeat(32) + ".json",
      parent(1, "active", DISPATCH_1, OWNER_1));
    f.resign();
  }, /orphan owner/);
  await invalid(f => {
    f.state.records.set(BASE + "secrets.json", { unsafe: true });
    f.resign();
  }, /unexpected or duplicate tree entry/);
  await invalid(f => {
    f.state.entries.push({ ...f.state.entries.find(
      entry => entry.path === BASE + PARENT_ID + ".json") });
  }, /unexpected or duplicate tree entry/);
  await invalid(f => {
    f.state.entries.find(entry => entry.path === epochPath(1)).mode = "120000";
  }, /unexpected or duplicate tree entry/);
  await invalid(f => {
    f.state.treeTruncated = true;
  }, /tree incomplete/);
  await invalid(f => {
    f.state.head = "not-a-SHA";
  }, /ref invalid/);
  await invalid(f => {
    f.state.records.get(BASE + PARENT_ID + ".json").owner_id = OWNER_1;
    // Do not update signed tree/Contents blob SHA.
  }, /pinned blob digest mismatch/);
  const denied = fixture();
  denied.state.failedStatus = 403;
  await assert.rejects(denied.read(), /response rejected/);
  const errorFixture = fixture();
  await assert.rejects(errorFixture.read({
    fetchImpl: async () => { throw Error("token " + TOKEN); }
  }), error => !String(error).includes(TOKEN) && /network unavailable/.test(String(error)));

  const responseSource = fs.readFileSync(require("node:path").join(__dirname, "service_worker.js"), "utf8");
  assert.ok(!responseSource.includes("github_fabric_private_parent_ownership_reader.js"),
    "candidate reader must not be imported by installed service worker");
  assert.equal(typeof reader.authorizeSend, "undefined");
  assert.equal(typeof reader.commit, "undefined");
  assert.equal(typeof reader.spawn, "undefined");
  assert.equal(typeof reader.publish, "undefined");
  console.log("Private parent ownership pinned read-only candidate tests passed.");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
