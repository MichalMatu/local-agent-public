"use strict";

const assert = require("node:assert/strict");
const model = require("./github_fabric_dispatch_model.js");

const hex = (char) => char.repeat(64);
const parent = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa220";

function child(id, role, nibble) {
  return {
    id,
    request_id: `child-${id}`,
    role,
    spawn: {
      schema_version: 1,
      transaction_id: `spawn-${hex(nibble)}`,
      child_request_digest: `sha256:${hex(nibble)}`,
      bootstrap_digest: `sha256:${hex(nibble === "a" ? "b" : "c")}`,
      bootstrap_text: [
        "LOCAL AGENT CHILD BOOTSTRAP",
        `child=${id}`,
        "Use immutable admitted context only."
      ].join("\n")
    }
  };
}

function fixture() {
  return {
    schema_version: 1,
    operation: "delegate",
    id: "browser-dispatch-001",
    request_id: "operator-request-001",
    request_digest: `sha256:${hex("d")}`,
    campaign_id: "cf-0123456789abcdef",
    parent_conversation_url: parent,
    children: [
      child("research", "research", "a"),
      child("verify", "verification", "e")
    ]
  };
}

{
  const dispatch = fixture();
  assert.equal(model.validateDispatch(dispatch), dispatch);
  assert.equal(model.canonicalParentUrl(parent), parent);
  assert.deepEqual(model.toConversationSpawnIntent(dispatch.children[0]), {
    schema_version: 1,
    transaction_id: `spawn-${hex("a")}`,
    child_request_digest: `sha256:${hex("a")}`,
    bootstrap_digest: `sha256:${hex("b")}`,
    bootstrap_text: dispatch.children[0].spawn.bootstrap_text,
    tab_id: null
  });
}

{
  const dispatch = fixture();
  const reordered = {
    children: dispatch.children,
    parent_conversation_url: dispatch.parent_conversation_url,
    campaign_id: dispatch.campaign_id,
    request_digest: dispatch.request_digest,
    request_id: dispatch.request_id,
    id: dispatch.id,
    operation: dispatch.operation,
    schema_version: dispatch.schema_version
  };
  assert.equal(model.canonicalJson(dispatch), model.canonicalJson(reordered));
  assert.equal(model.reconcileDispatch(dispatch, reordered), dispatch);
}

{
  const existing = fixture();
  const changed = fixture();
  changed.children = changed.children.map((entry, index) => index === 0
    ? { ...entry, spawn: { ...entry.spawn, bootstrap_text: entry.spawn.bootstrap_text + "\nchanged" } }
    : entry);
  assert.throws(
    () => model.reconcileDispatch(existing, changed),
    /same-id dispatch conflict/
  );
}

for (const role of ["research", "implementation", "verification", "integration"]) {
  const dispatch = fixture();
  dispatch.children = [child(role, role, role === "research" ? "1" : role === "implementation" ? "2" : role === "verification" ? "3" : "4")];
  assert.doesNotThrow(() => model.validateDispatch(dispatch));
}

{
  const dispatch = fixture();
  dispatch.children[0] = { ...dispatch.children[0], agent_binding: "forbidden" };
  assert.throws(
    () => model.validateDispatch(dispatch),
    /child fields do not match schema/,
    "browser dispatch must not carry repository execution authority"
  );
}

{
  const dispatch = fixture();
  dispatch.children[0].spawn = { ...dispatch.children[0].spawn, tab_id: 42 };
  assert.throws(
    () => model.validateDispatch(dispatch),
    /spawn fields do not match schema/,
    "GitHub must not select a runtime Chrome tab"
  );
}

{
  const dispatch = fixture();
  dispatch.parent_conversation_url = "https://chat.openai.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa220";
  assert.throws(() => model.validateDispatch(dispatch), /must be canonical/);
}

{
  const dispatch = fixture();
  dispatch.children[0].role = "reviewer";
  assert.throws(() => model.validateDispatch(dispatch), /role is invalid/);
}

{
  const dispatch = fixture();
  dispatch.children[0].spawn.bootstrap_text = "x".repeat(model.MAX_BOOTSTRAP_CHARS + 1);
  assert.throws(() => model.validateDispatch(dispatch), /bootstrap_text must be non-empty bounded text/);
}

{
  const dispatch = fixture();
  dispatch.children.push(child("extra-a", "research", "5"));
  dispatch.children.push(child("extra-b", "research", "6"));
  dispatch.children.push(child("extra-c", "research", "7"));
  assert.throws(() => model.validateDispatch(dispatch), /children must contain 1\.\.4 items/);
}

console.log("GitHub-first Conversation Fabric dispatch model tests passed.");
