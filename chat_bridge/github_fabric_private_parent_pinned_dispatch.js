(function initPrivateParentPinnedDispatch(root, factory) {
  const dispatchModel = root.LocalAgentGithubFabricDispatchModel ||
    (typeof require === "function" ? require("./github_fabric_dispatch_model.js") : null);
  const api = factory(dispatchModel);
  root.LocalAgentPrivateParentPinnedDispatch = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateParentPinnedDispatch(dispatchModel) {
  "use strict";

  // Read-only candidate. Caller supplies a decoder bound to a SINGLE
  // authenticated Git tree/commit, never a branch-relative Contents path.
  const PROJECT = "local-agent";
  const WORKFLOW = "workflow-001";
  const PROJECTS_PATH = "projects/index.json";
  const WORKFLOWS_PATH = "projects/local-agent/workflows/index.json";
  const DISPATCH_INDEX_PATH = "projects/local-agent/workflows/workflow-001/dispatches/index.json";
  const DISPATCH_ROOT = "projects/local-agent/workflows/workflow-001/dispatches/";
  const MAX_INDEX_BYTES = 8192;
  const MAX_DISPATCH_BYTES = 128 * 1024;
  const DISPATCH_RE = /^fabric-[0-9a-f]{32}$/;
  const PROJECT_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
  const WORKFLOW_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
  const SHA_RE = /^sha256:[0-9a-f]{64}$/;

  function exact(value, fields) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("|") === [...fields].sort().join("|");
  }

  function validateCatalog(value, key, regex, maximum) {
    if (!exact(value, ["schema_version", key]) ||
        value.schema_version !== 1 || !Array.isArray(value[key]) ||
        value[key].length > maximum ||
        value[key].some(x => typeof x !== "string" || !regex.test(x)) ||
        value[key].join("|") !== [...new Set(value[key])].sort().join("|")) {
      throw new Error("Private parent pinned dispatch catalog invalid");
    }
    return value[key];
  }

  async function digest(value) {
    if (!globalThis.crypto?.subtle) {
      throw new Error("Private parent pinned dispatch SHA-256 unavailable");
    }
    const bytes = new TextEncoder().encode(value);
    return Array.from(new Uint8Array(await globalThis.crypto.subtle.digest(
      "SHA-256", bytes
    )), n => n.toString(16).padStart(2, "0")).join("");
  }

  async function readPinnedDispatchChildren({
    expectedDispatchId, expectedParentUrl, readPinned
  } = {}) {
    if (typeof expectedDispatchId !== "string" ||
        !DISPATCH_RE.test(expectedDispatchId) ||
        typeof expectedParentUrl !== "string" ||
        typeof readPinned !== "function" || !dispatchModel) {
      throw new Error("Private parent pinned dispatch input invalid");
    }
    const projects = await readPinned(PROJECTS_PATH, MAX_INDEX_BYTES);
    const workflows = await readPinned(WORKFLOWS_PATH, MAX_INDEX_BYTES);
    const index = await readPinned(DISPATCH_INDEX_PATH, MAX_INDEX_BYTES);
    if (!validateCatalog(projects, "project_ids", PROJECT_RE, 32).includes(PROJECT) ||
        !validateCatalog(workflows, "workflow_ids", WORKFLOW_RE, 128).includes(WORKFLOW) ||
        !validateCatalog(index, "dispatch_ids", DISPATCH_RE, 4).includes(expectedDispatchId)) {
      throw new Error("Private parent pinned dispatch not indexed");
    }
    const dispatch = await readPinned(
      DISPATCH_ROOT + expectedDispatchId + ".json", MAX_DISPATCH_BYTES
    );
    dispatchModel.validateDispatch(dispatch);
    if (dispatch.id !== expectedDispatchId ||
        dispatch.parent_conversation_url !== expectedParentUrl ||
        typeof dispatch.request_digest !== "string" ||
        !SHA_RE.test(dispatch.request_digest)) {
      throw new Error("Private parent pinned dispatch identity mismatch");
    }
    const calculatedId = "fabric-" + (
      await digest(JSON.stringify(["github-fabric-dispatch-v1", dispatch.request_digest]))
    ).slice(0, 32);
    const campaignId = "cf-" + (
      await digest(JSON.stringify(["github-fabric-campaign-v1",
        dispatch.request_id, dispatch.request_digest, dispatch.parent_conversation_url]))
    ).slice(0, 16);
    if (dispatch.id !== calculatedId || dispatch.campaign_id !== campaignId) {
      throw new Error("Private parent pinned dispatch digest mismatch");
    }
    const children = [];
    for (const child of dispatch.children) {
      const spawn = child.spawn;
      const expectedBootstrap = "sha256:" + await digest(spawn.bootstrap_text);
      const expectedTransaction = "spawn-" + await digest(JSON.stringify([
        spawn.child_request_digest, 1
      ]));
      if (spawn.bootstrap_digest !== expectedBootstrap ||
          spawn.transaction_id !== expectedTransaction) {
        throw new Error("Private parent pinned dispatch child digest mismatch");
      }
      children.push(Object.freeze({
        child_request_id: child.request_id,
        spawn_transaction_id: spawn.transaction_id
      }));
    }
    return Object.freeze(children);
  }

  return Object.freeze({
    readPinnedDispatchChildren, MAX_INDEX_BYTES, MAX_DISPATCH_BYTES
  });
});
