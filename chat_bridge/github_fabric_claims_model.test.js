"use strict";

// Synthetic Python -> JS claim-record contract. No Chrome or machine effects.
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const model = require("./github_fabric_claims_model.js");

const source = [
  "import json",
  "from local_agent.conversation.github_fabric_claims import build_claims",
  "from tests.test_github_fabric_dispatch import operator_request, admitted_children",
  "claims=build_claims(operator_request(),admitted_children())",
  "print(json.dumps({'index': {'schema_version':1,'claim_ids':[c['id'] for c in claims]},'claims':claims}))"
].join("\n");
const python = spawnSync(process.env.PYTHON || "python", ["-c", source], {
  cwd: path.resolve(__dirname, ".."), encoding: "utf8",
  maxBuffer: 65536, timeout: 30000
});
assert.equal(python.status, 0, python.stderr || "Python synthetic claim derivation failed");
const fixture = JSON.parse(python.stdout.trim());

async function check() {
  assert.deepEqual(model.validateIndex(fixture.index), fixture.index);
  for (const claim of fixture.claims) {
    assert.deepEqual(await model.validateClaim(claim), claim);
    assert.equal(await model.deriveClaimId(claim.workflow_id, claim.workflow_node_id), claim.id);
    assert.equal(claim.phase, "admission_preview");
    assert.equal(claim.mode, "synthetic_observation_only");
  }
  const view = await model.reconcileReadOnly(fixture.index, fixture.claims);
  assert.equal(view.kind, "synthetic_observation_only");
  assert.deepEqual(view.claim_ids, fixture.index.claim_ids);
  assert.deepEqual(view.all_phases, ["admission_preview", "admission_preview"]);
  assert.equal(typeof model.spawn, "undefined");
  assert.equal(typeof model.claimExecution, "undefined");

  const malformedIndex = [
    { schema_version: 1, claim_ids: [fixture.index.claim_ids[0], fixture.index.claim_ids[0]] },
    { schema_version: 1, claim_ids: ["../bad"] },
    { schema_version: true, claim_ids: [] },
    { schema_version: 1, claim_ids: [], secret: "not a field" },
    { schema_version: 1, claim_ids: ["claim-" + "f".repeat(32)].concat(fixture.index.claim_ids).concat(fixture.index.claim_ids) }
  ];
  for (const index of malformedIndex) {
    assert.throws(() => model.validateIndex(index), /invalid/);
  }
  await assert.rejects(model.reconcileReadOnly(fixture.index, fixture.claims.slice(0, 1)), /incomplete/);
  await assert.rejects(model.reconcileReadOnly(fixture.index, [fixture.claims[0], fixture.claims[0]]), /duplicated/);
  for (const [key, value] of [
    ["id", "claim-" + "f".repeat(32)],
    ["workflow_id", 3],
    ["parent_conversation_url", "https://attacker.example/c/test"],
    ["operator_request_id", 3],
    ["operator_request_digest", "sha256:" + "G".repeat(64)],
    ["spawn_transaction_id", "spawn-" + "Z".repeat(64)],
    ["phase", "submit_armed"],
    ["mode", "browser_execution_approved"],
    ["bootstrap_digest", "sha256:" + "X".repeat(64)]
  ]) {
    const changed = { ...fixture.claims[0], [key]: value };
    await assert.rejects(model.validateClaim(changed), /invalid|mismatch/);
  }
  const extra = { ...fixture.claims[0], bootstrap_text: "PRIVATE" };
  await assert.rejects(model.validateClaim(extra), /invalid/);
  const reordered = await model.reconcileReadOnly(fixture.index, fixture.claims.slice().reverse());
  assert.deepEqual(reordered.claim_ids, view.claim_ids);
  console.log("Python/JavaScript synthetic semantic claim round trip passed.");
}

check().catch(error => { console.error(error); process.exitCode = 1; });
