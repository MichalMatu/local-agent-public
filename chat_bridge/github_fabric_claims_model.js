(function initGithubFabricClaimModel(root, factory) {
  const api = factory();
  root.LocalAgentGithubFabricClaimModel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createClaimModel() {
  "use strict";

  // Strictly observation-only. Not imported into the production MV3 worker.
  const SCHEMA_VERSION = 1;
  const MODE = "synthetic_observation_only";
  const PHASE = "admission_preview";
  const MAX_CLAIMS = 4;
  const ROOT = ".agent/conversation/browser_claims/";
  const INDEX_PATH = ROOT + "index.json";
  const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
  const CLAIM_ID = /^claim-[0-9a-f]{32}$/;
  const DIGEST = /^sha256:[0-9a-f]{64}$/;
  const SHA_TX = /^spawn-[0-9a-f]{64}$/;
  const DISPATCH = /^fabric-[0-9a-f]{32}$/;
  const PARENT = /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]{1,200}$/;
  const FIELDS = [
    "schema_version", "id", "workflow_id", "workflow_node_id",
    "parent_conversation_url", "operator_request_id",
    "operator_request_digest", "dispatch_id", "child_request_id",
    "child_request_digest", "spawn_transaction_id", "bootstrap_digest",
    "mode", "phase"
  ];

  function exactFields(value, fields) {
    return value !== null && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join(",") === fields.slice().sort().join(",");
  }
  function bounded(value, maximum) {
    return new TextEncoder().encode(JSON.stringify(value)).length <= maximum;
  }
  function matches(value, regex) {
    return typeof value === "string" && regex.test(value);
  }
  function validateIndex(index) {
    if (!exactFields(index, ["schema_version", "claim_ids"]) ||
        index.schema_version !== SCHEMA_VERSION ||
        !Array.isArray(index.claim_ids) ||
        index.claim_ids.length > MAX_CLAIMS ||
        index.claim_ids.some(id => typeof id !== "string" || !CLAIM_ID.test(id)) ||
        index.claim_ids.join(",") !== [...new Set(index.claim_ids)].sort().join(",") ||
        !bounded(index, 4096)) {
      throw new Error("Fabric semantic claim index is invalid");
    }
    return index;
  }
  async function deriveClaimId(workflowId, workflowNodeId) {
    if (typeof workflowId !== "string" || !ID.test(workflowId) ||
        typeof workflowNodeId !== "string" || !ID.test(workflowNodeId)) {
      throw new Error("Fabric semantic claim identity is invalid");
    }
    if (!globalThis.crypto?.subtle) {
      throw new Error("SHA-256 is unavailable for Fabric semantic claim");
    }
    const material = JSON.stringify(["fabric-semantic-node-v1", workflowId, workflowNodeId]);
    const bytes = new TextEncoder().encode(material);
    const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
    return "claim-" + Array.from(digest, b => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
  }
  async function validateClaim(record) {
    if (!exactFields(record, FIELDS) ||
        record.schema_version !== SCHEMA_VERSION ||
        typeof record.id !== "string" || !CLAIM_ID.test(record.id) ||
        !matches(record.workflow_id, ID) ||
        !matches(record.workflow_node_id, ID) ||
        !matches(record.parent_conversation_url, PARENT) ||
        !matches(record.operator_request_id, ID) ||
        !matches(record.child_request_id, ID) ||
        !matches(record.operator_request_digest, DIGEST) ||
        !matches(record.child_request_digest, DIGEST) ||
        !matches(record.bootstrap_digest, DIGEST) ||
        !matches(record.dispatch_id, DISPATCH) ||
        !matches(record.spawn_transaction_id, SHA_TX) ||
        record.mode !== MODE || record.phase !== PHASE || !bounded(record, 4096)) {
      throw new Error("Fabric semantic claim is invalid");
    }
    if (record.id !== await deriveClaimId(record.workflow_id, record.workflow_node_id)) {
      throw new Error("Fabric semantic claim identity mismatch");
    }
    return record;
  }
  async function reconcileReadOnly(index, records) {
    validateIndex(index);
    if (!Array.isArray(records) || records.length !== index.claim_ids.length) {
      throw new Error("Fabric semantic claim snapshot incomplete");
    }
    const byId = new Map();
    for (const record of records) {
      await validateClaim(record);
      if (byId.has(record.id)) throw new Error("Fabric semantic claim duplicated");
      byId.set(record.id, record);
    }
    if (index.claim_ids.some(id => !byId.has(id))) {
      throw new Error("Fabric semantic claim index missing immutable record");
    }
    // No spawn permission or writable action is exposed by this model.
    return Object.freeze({
      kind: "synthetic_observation_only",
      claim_ids: Object.freeze(index.claim_ids.slice()),
      all_phases: Object.freeze(records.map(record => byId.get(record.id).phase))
    });
  }
  return Object.freeze({
    SCHEMA_VERSION, INDEX_PATH, ROOT, validateIndex,
    deriveClaimId, validateClaim, reconcileReadOnly
  });
});
