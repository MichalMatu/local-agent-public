"use strict";

const assert = require("node:assert/strict");
const nodeCrypto = require("node:crypto");
if (!globalThis.crypto?.subtle) globalThis.crypto = nodeCrypto.webcrypto;
const reader = require("./github_fabric_private_parent_fence_reader.js");

const parentUrl = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa220";
const otherUrl = "https://chatgpt.com/c/another-parent";
const token = "github_pat_synthetic_readonly_only_123456";
const head = "a".repeat(40);
const treeSha = "b".repeat(40);
const API = "https://api.github.com/repos/example-owner/local-agent-fabric-private";

function sha(value) {
  return nodeCrypto.createHash("sha256").update(value).digest("hex");
}

function gitBlobSha(value) {
  const bytes = Buffer.from(JSON.stringify(value), "utf8");
  return nodeCrypto.createHash("sha1")
    .update(Buffer.from("blob " + bytes.length + "\0"))
    .update(bytes).digest("hex");
}

function response(data, status = 200) {
  return new Response(JSON.stringify(data), { status });
}

function metadata(path, value, blobSha) {
  const bytes = Buffer.from(JSON.stringify(value), "utf8");
  return {
    type: "file", path, sha: blobSha, size: bytes.length,
    encoding: "base64", content: bytes.toString("base64")
  };
}

async function fixture() {
  const id = "parent-" + sha(JSON.stringify([
    "fabric-global-parent-mode-v1", parentUrl
  ])).slice(0, 32);
  const path = "parents/" + id + ".json";
  const record = {
    schema_version: 1,
    kind: "synthetic_parent_transport_arbitration_preview",
    id,
    project_id: "local-agent",
    parent_conversation_url: parentUrl,
    workflow_id: "workflow-001",
    operator_request_id: "operator-request-001",
    operator_request_digest: "sha256:" + "e".repeat(64),
    dispatch_id: "fabric-" + "f".repeat(32),
    transport_mode: "github_first",
    fence_epoch: 1,
    phase: "unattested_no_browser_authority",
    browser_send_authorized: false,
    ack_state: "not_attested"
  };
  const state = {
    ref: { ref: "refs/heads/fabric-data", object: { sha: head, type: "commit" } },
    commit: { tree: { sha: treeSha } },
    index: { schema_version: 1, parent_ids: [id] },
    record,
    tree: {
      sha: treeSha, truncated: false, tree: [
        { path: "parents", type: "tree", mode: "040000", sha: "f".repeat(40) },
        { path: "parents/index.json", type: "blob", mode: "100644", sha: gitBlobSha({ schema_version: 1, parent_ids: [id] }) },
        { path, type: "blob", mode: "100644", sha: gitBlobSha(record) }
      ]
    }
  };
  const resign = () => {
    const indexEntry = state.tree.tree.find(entry => entry.path === "parents/index.json");
    const recordEntry = state.tree.tree.find(entry => entry.path === path);
    if (indexEntry) indexEntry.sha = gitBlobSha(state.index);
    if (recordEntry) recordEntry.sha = gitBlobSha(state.record);
  };
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    assert.ok(url.startsWith(API + "/"), "token cannot go to an arbitrary host");
    assert.equal(init.method, "GET");
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    assert.equal(init.credentials, "omit");
    assert.equal(init.headers.Authorization, "Bearer " + token);
    assert.ok(init.signal instanceof AbortSignal,
      "private reader must attach an abortable deadline to every GET");
    if (url === API + "/git/ref/heads/fabric-data") return response(state.ref);
    if (url === API + "/git/commits/" + head) return response(state.commit);
    if (url === API + "/git/trees/" + treeSha + "?recursive=1") return response(state.tree);
    if (url === API + "/contents/parents/index.json?ref=" + head) {
      return response(metadata("parents/index.json", state.index, state.tree.tree[1].sha));
    }
    if (url === API + "/contents/" + path + "?ref=" + head) {
      return response(metadata(path, state.record, state.tree.tree[2].sha));
    }
    throw new Error("unexpected test URL");
  };
  return { id, path, state, calls, fetchImpl, resign };
}

async function read(f, options = {}) {
  return reader.readPrivateParentFenceSnapshot({
    enabled: true, readToken: token, parentConversationUrl: parentUrl,
    expectedTransportMode: "github_first", fetchImpl: f.fetchImpl, ...options
  });
}

async function run() {
  const f = await fixture();
  assert.equal(await reader.parentId(parentUrl), f.id);
  assert.equal(await reader.parentId(otherUrl),
    "parent-" + sha(JSON.stringify(["fabric-global-parent-mode-v1", otherUrl])).slice(0, 32));
  await assert.rejects(read(f, { enabled: false }), /default-disabled/);
  await assert.rejects(read(f, { readToken: null }), /authorization missing/);
  await assert.rejects(read(f, { expectedTransportMode: "auto" }), /transport mode invalid/);
  for (const url of [parentUrl + "/", parentUrl + "?secret=x", "http://chatgpt.com/c/id",
    "https://chat.openai.com/c/id", "https://chatgpt.com/c/../id"]) {
    await assert.rejects(read(f, { parentConversationUrl: url }),
      /canonical conversation identity required/);
  }
  assert.equal(f.calls.length, 0);

  const observed = await read(f);
  assert.equal(observed.status, "unattested_preview");
  assert.equal(observed.source_head_sha, head);
  assert.equal(observed.parent_id, f.id);
  assert.equal(observed.source, "authenticated_private_github_preview_only");
  assert.equal(observed.browser_effects_permitted, false);
  assert.equal(observed.record.browser_send_authorized, false);
  assert.equal(observed.record.ack_state, "not_attested");
  assert.deepEqual(f.calls.map(c => c.url), [
    API + "/git/ref/heads/fabric-data",
    API + "/git/commits/" + head,
    API + "/git/trees/" + treeSha + "?recursive=1",
    API + "/contents/parents/index.json?ref=" + head,
    API + "/contents/" + f.path + "?ref=" + head
  ]);
  const retry = await read(f);
  assert.deepEqual(retry, observed);
  assert.equal(f.calls.length, 10);

  const competing = await fixture();
  competing.state.record.transport_mode = "legacy_dom";
  competing.resign();
  await assert.rejects(read(competing), /competing transport mode/);
  assert.equal((await read(competing, { expectedTransportMode: "legacy_dom" }))
    .browser_effects_permitted, false);

  const missing = await fixture();
  missing.state.tree.tree = [];
  assert.equal((await read(missing)).status, "unregistered");
  assert.equal((await read(missing)).browser_effects_permitted, false);
  const notIndexed = await fixture();
  notIndexed.state.tree.tree = notIndexed.state.tree.tree.slice(0, 2);
  notIndexed.state.index.parent_ids = [];
  notIndexed.resign();
  assert.equal((await read(notIndexed)).status, "unregistered");

  const dangling = await fixture();
  dangling.state.tree.tree = dangling.state.tree.tree.slice(0, 2);
  await assert.rejects(read(dangling), /dangling record/);
  const orphan = await fixture();
  orphan.state.index.parent_ids = [];
  orphan.resign();
  await assert.rejects(read(orphan), /orphan or dangling/);
  const badPath = await fixture();
  badPath.state.tree.tree.push({
    path: "parents/unexpected.json", type: "blob",
    mode: "100644", sha: "e".repeat(40)
  });
  await assert.rejects(read(badPath), /unexpected or duplicate/);
  const duplicate = await fixture();
  duplicate.state.tree.tree.push(duplicate.state.tree.tree[2]);
  await assert.rejects(read(duplicate), /unexpected or duplicate/);
  const symlink = await fixture();
  symlink.state.tree.tree[2].mode = "120000";
  await assert.rejects(read(symlink), /unexpected or duplicate/);
  const missingParentRoot = await fixture();
  missingParentRoot.state.tree.tree.shift();
  await assert.rejects(read(missingParentRoot), /root missing from tree/);
  const duplicateParentRoot = await fixture();
  duplicateParentRoot.state.tree.tree.unshift({
    ...duplicateParentRoot.state.tree.tree[0]
  });
  await assert.rejects(read(duplicateParentRoot), /root invalid or duplicated/);
  const parentRootSymlink = await fixture();
  parentRootSymlink.state.tree.tree[0].mode = "120000";
  await assert.rejects(read(parentRootSymlink), /root invalid or duplicated/);
  const parentRootInvalidSha = await fixture();
  parentRootInvalidSha.state.tree.tree[0].sha = "malformed";
  await assert.rejects(read(parentRootInvalidSha), /root invalid or duplicated/);
  const parentRootWrongType = await fixture();
  parentRootWrongType.state.tree.tree[0].type = "blob";
  await assert.rejects(read(parentRootWrongType), /root invalid or duplicated/);
  const arrayRootSha = await fixture();
  arrayRootSha.state.tree.tree[0].sha = ["f".repeat(40)];
  await assert.rejects(read(arrayRootSha), /root invalid or duplicated/);
  const arrayRecordSha = await fixture();
  arrayRecordSha.state.tree.tree[2].sha = [arrayRecordSha.state.tree.tree[2].sha];
  await assert.rejects(read(arrayRecordSha), /unexpected or duplicate/);
  const truncated = await fixture();
  truncated.state.tree.truncated = true;
  await assert.rejects(read(truncated), /origin tree incomplete/);
  const treeChanged = await fixture();
  treeChanged.state.tree.sha = "e".repeat(40);
  await assert.rejects(read(treeChanged), /origin tree incomplete/);
  const refChanged = await fixture();
  refChanged.state.ref.object.sha = "badsha";
  await assert.rejects(read(refChanged), /origin ref invalid/);

  // Python requires literal string IDs. Numeric values must not become
  // apparently valid IDs through JS String(...) coercion, even with a
  // perfectly matching, signed private tree/Contents fixture.
  for (const field of ["workflow_id", "operator_request_id"]) {
    for (const invalid of [123, true, null, ["workflow-001"], { id: "workflow-001" }]) {
      const malformed = await fixture();
      malformed.state.record[field] = invalid;
      malformed.resign();
      await assert.rejects(read(malformed), /preview record invalid/,
        field + " must be a literal string, not " + typeof invalid);
    }
  }

  // A one-element array stringifies to the valid digest/dispatch label in
  // JavaScript, but the trusted Python schema requires literal JSON strings.
  for (const field of ["operator_request_digest", "dispatch_id"]) {
    const reference = (await fixture()).state.record[field];
    for (const invalid of [[reference], { value: reference }, null, true]) {
      const malformed = await fixture();
      malformed.state.record[field] = invalid;
      malformed.resign();
      await assert.rejects(read(malformed), /preview record invalid/,
        field + " must never be accepted via JS String coercion");
    }
  }

  const forged = await fixture();
  forged.state.record.browser_send_authorized = true;
  forged.resign();
  await assert.rejects(read(forged), /preview record invalid/);
  const forgedAck = await fixture();
  forgedAck.state.record.ack_state = "received";
  forgedAck.resign();
  await assert.rejects(read(forgedAck), /preview record invalid/);
  const epoch = await fixture();
  epoch.state.record.fence_epoch = 2;
  epoch.resign();
  await assert.rejects(read(epoch), /preview record invalid/);
  const identity = await fixture();
  identity.state.record.id = "parent-" + "0".repeat(32);
  identity.resign();
  await assert.rejects(read(identity), /preview record invalid/);
  const additionalField = await fixture();
  additionalField.state.record.token = "never-accept";
  additionalField.resign();
  await assert.rejects(read(additionalField), /preview record invalid/);
  const wrongIndex = await fixture();
  wrongIndex.state.index.parent_ids = [f.id, f.id];
  wrongIndex.resign();
  await assert.rejects(read(wrongIndex), /index invalid/);
  // A changed JSON body is not trusted merely because the Contents SHA
  // metadata still matches the Git tree entry.
  const silentlyChangedRecord = await fixture();
  silentlyChangedRecord.state.record.operator_request_id = "tampered-request";
  await assert.rejects(read(silentlyChangedRecord), /blob digest mismatch/);
  const silentlyChangedIndex = await fixture();
  silentlyChangedIndex.state.index.parent_ids = [];
  await assert.rejects(read(silentlyChangedIndex), /blob digest mismatch/);
  const forgedTreeClaim = await fixture();
  forgedTreeClaim.state.tree.tree[2].sha = "0".repeat(40);
  await assert.rejects(read(forgedTreeClaim), /blob digest mismatch/);

  const metadataConflict = await fixture();
  metadataConflict.fetchImpl = async (url, init) => {
    const responseValue = await f.fetchImpl(url, init);
    if (url.includes("/contents/parents/index.json")) {
      const data = await responseValue.json();
      data.sha = "0".repeat(40);
      return response(data);
    }
    return responseValue;
  };
  await assert.rejects(read(metadataConflict), /Contents metadata invalid/);

  const denied = await fixture();
  denied.fetchImpl = async () => response({ message: "Permission denied" }, 403);
  await assert.rejects(read(denied), /source response rejected/);
  const network = await fixture();
  network.fetchImpl = async () => { throw new Error("token " + token); };
  await assert.rejects(read(network), error => !String(error).includes(token) &&
    /network read failed/.test(String(error)));
  const redirected = await fixture();
  redirected.fetchImpl = async () => ({
    status: 200, redirected: true, url: "https://evil.invalid",
    body: response({}).body
  });
  await assert.rejects(read(redirected), /source response rejected/);
  // Deliberately replace the timer only inside these deterministic fake
  // transport tests; neither request may hang or disclose the secret.
  const nativeSetTimeout = globalThis.setTimeout;
  try {
    globalThis.setTimeout = (callback, _delay) => nativeSetTimeout(callback, 0);
    const stuckHeaders = await fixture();
    stuckHeaders.fetchImpl = async (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => {
        reject(new Error("secret " + token));
      }, { once: true });
    });
    await assert.rejects(read(stuckHeaders), error =>
      !String(error).includes(token) && /network read failed/.test(String(error)));

    const stuckBody = await fixture();
    stuckBody.fetchImpl = async (_url, init) => ({
      status: 200, redirected: false, url: "",
      body: new ReadableStream({
        start(controller) {
          init.signal.addEventListener("abort", () => {
            controller.error(new Error("secret " + token));
          }, { once: true });
        }
      })
    });
    await assert.rejects(read(stuckBody), error =>
      !String(error).includes(token) && /response incomplete/.test(String(error)));
  } finally {
    globalThis.setTimeout = nativeSetTimeout;
  }

  const oversized = await fixture();
  oversized.fetchImpl = async () => ({
    status: 200, redirected: false, url: "",
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array(525000));
        controller.close();
      }
    })
  });
  await assert.rejects(read(oversized), /incomplete or exceeds bound/);

  assert.equal(typeof reader.authorizeSend, "undefined");
  assert.equal(typeof reader.publish, "undefined");
  assert.equal(typeof reader.spawn, "undefined");
  console.log("Default-disabled private parent fence pinned read tests passed.");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
