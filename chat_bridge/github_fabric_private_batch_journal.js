(function initPrivateFabricBatchJournal(root, factory) {
  const dispatchModel = root.LocalAgentGithubFabricDispatchModel ||
    (typeof require === "function" ? require("./github_fabric_dispatch_model.js") : null);
  const api = factory(dispatchModel);
  root.LocalAgentPrivateFabricBatchJournal = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateFabricBatchJournal(model) {
  "use strict";

  // Candidate-only. NOT imported into the live MV3 service worker until a
  // remote global parent fence and operator-approved browser gate exist.
  // Chrome local storage is not a cross-device lease or an atomic CAS.
  const STORAGE_KEY = "privateFabricBatchJournalV2";
  const SHA_RE = /^[0-9a-f]{40}$/;
  const OWNER_RE = /^[0-9a-f]{32}$/;
  const CHILD_URL_RE = /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]{1,200}$/;
  const PHASES = new Set([
    "pending", "claim_intent", "claim_ambiguous", "claimed",
    "tab_open_intent", "tab_ready", "submission_unknown",
    "ack_pending", "running", "result_pending", "completed",
    "blocked", "abandoned"
  ]);
  const NEXT = Object.freeze({
    pending: ["claim_intent"],
    claim_intent: ["claimed", "claim_ambiguous"],
    claim_ambiguous: [],
    claimed: ["tab_open_intent"],
    tab_open_intent: ["tab_ready", "blocked"],
    tab_ready: ["submission_unknown", "blocked"],
    submission_unknown: ["ack_pending", "abandoned"],
    ack_pending: ["running"],
    running: ["result_pending"],
    result_pending: ["completed"],
    completed: [],
    blocked: [],
    abandoned: []
  });
  const JOURNAL_FIELDS = [
    "schema_version", "dispatch_id", "source_head_sha",
    "parent_conversation_url", "revision", "children"
  ];
  const CHILD_FIELDS = [
    "child_request_id", "child_id", "spawn_transaction_id", "bootstrap_digest",
    "phase", "owner_id", "tab_id", "child_conversation_url", "result_path"
  ];

  function exact(value, fields) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("|") === [...fields].sort().join("|");
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function resultPath(dispatch, childId) {
    return "projects/local-agent/workflows/workflow-001/receipts/" +
      dispatch.id + "/result/" + childId + ".json";
  }

  function validateJournal(journal, dispatch) {
    if (!model) throw new Error("Private Fabric dispatch model missing");
    model.validateDispatch(dispatch);
    if (!exact(journal, JOURNAL_FIELDS) || journal.schema_version !== 2 ||
        journal.dispatch_id !== dispatch.id ||
        journal.parent_conversation_url !== dispatch.parent_conversation_url ||
        !SHA_RE.test(String(journal.source_head_sha || "")) ||
        !Number.isSafeInteger(journal.revision) || journal.revision < 0 ||
        !Array.isArray(journal.children) ||
        journal.children.length !== dispatch.children.length) {
      throw new Error("Private Fabric batch journal header invalid");
    }
    const tabIds = new Set();
    const childUrls = new Set();
    const ownerIds = new Set();
    let active = 0;
    for (let i = 0; i < dispatch.children.length; i++) {
      const entry = journal.children[i];
      const expected = dispatch.children[i];
      if (!exact(entry, CHILD_FIELDS) ||
          entry.child_request_id !== expected.request_id ||
          entry.child_id !== expected.id ||
          entry.spawn_transaction_id !== expected.spawn.transaction_id ||
          entry.bootstrap_digest !== expected.spawn.bootstrap_digest ||
          !PHASES.has(entry.phase) ||
          typeof entry.owner_id !== "string" ||
          typeof entry.child_conversation_url !== "string" ||
          typeof entry.result_path !== "string") {
        throw new Error("Private Fabric batch child evidence invalid");
      }
      const hasOwner = entry.owner_id.length > 0;
      const hasTab = entry.tab_id !== null;
      const hasUrl = entry.child_conversation_url.length > 0;
      const hasResult = entry.result_path.length > 0;
      if (hasOwner && (!OWNER_RE.test(entry.owner_id) || ownerIds.has(entry.owner_id))) {
        throw new Error("Private Fabric batch owner identity invalid or duplicated");
      }
      if (hasTab && (!Number.isSafeInteger(entry.tab_id) || entry.tab_id < 1 ||
          tabIds.has(entry.tab_id))) {
        throw new Error("Private Fabric batch tab identity invalid");
      }
      if (hasUrl && (!CHILD_URL_RE.test(entry.child_conversation_url) ||
          childUrls.has(entry.child_conversation_url))) {
        throw new Error("Private Fabric batch child URL invalid or duplicated");
      }
      if (hasResult && entry.result_path !== resultPath(dispatch, entry.child_request_id)) {
        throw new Error("Private Fabric batch result path invalid");
      }
      if (entry.phase === "pending" && (hasOwner || hasTab || hasUrl || hasResult)) {
        throw new Error("Private Fabric batch pending child has side-effect evidence");
      }
      if (entry.phase !== "pending" && !hasOwner) {
        throw new Error("Private Fabric batch claimed child missing owner");
      }
      if (hasTab && !["tab_ready", "submission_unknown", "ack_pending", "running",
          "result_pending", "completed", "blocked", "abandoned"].includes(entry.phase)) {
        throw new Error("Private Fabric batch tab exists before confirmed tab phase");
      }
      if (["tab_ready", "submission_unknown", "ack_pending", "running",
          "result_pending", "completed", "abandoned"].includes(entry.phase) && !hasTab) {
        throw new Error("Private Fabric batch tab identity missing");
      }
      if (hasUrl && !["ack_pending", "running", "result_pending", "completed"].includes(entry.phase)) {
        throw new Error("Private Fabric batch URL exists before observed submission");
      }
      if (["ack_pending", "running", "result_pending", "completed"].includes(entry.phase) && !hasUrl) {
        throw new Error("Private Fabric batch observed child URL missing");
      }
      if (hasResult !== (entry.phase === "completed")) {
        throw new Error("Private Fabric batch terminal result state inconsistent");
      }
      if (hasOwner) ownerIds.add(entry.owner_id);
      if (hasTab) tabIds.add(entry.tab_id);
      if (hasUrl) childUrls.add(entry.child_conversation_url);
      if (!["pending", "completed", "blocked", "abandoned", "claim_ambiguous"].includes(entry.phase)) active++;
    }
    if (active > 1) {
      throw new Error("Private Fabric batch concurrent UI side effects not admitted");
    }
    return journal;
  }

  function initializeJournal(dispatch, sourceHeadSha) {
    model.validateDispatch(dispatch);
    const journal = {
      schema_version: 2, dispatch_id: dispatch.id,
      source_head_sha: sourceHeadSha,
      parent_conversation_url: dispatch.parent_conversation_url,
      revision: 0,
      children: dispatch.children.map(child => ({
        child_request_id: child.request_id,
        child_id: child.id,
        spawn_transaction_id: child.spawn.transaction_id,
        bootstrap_digest: child.spawn.bootstrap_digest,
        phase: "pending", owner_id: "", tab_id: null,
        child_conversation_url: "", result_path: ""
      }))
    };
    return validateJournal(journal, dispatch);
  }

  function eligibleChild(journal, dispatch) {
    validateJournal(journal, dispatch);
    // A partial or uncertain batch must not silently arm its next sibling.
    if (journal.children.some(c => !["pending", "completed"].includes(c.phase))) return null;
    return journal.children.find(c => c.phase === "pending")?.child_request_id || null;
  }

  function transition(journal, dispatch, requestId, nextPhase, evidence = {}) {
    validateJournal(journal, dispatch);
    if (!exact(evidence, Object.keys(evidence))) {
      throw new Error("Private Fabric transition evidence must be an object");
    }
    const index = journal.children.findIndex(c => c.child_request_id === requestId);
    if (index < 0) throw new Error("Private Fabric batch child request not admitted");
    const previous = journal.children[index];
    if (!NEXT[previous.phase].includes(nextPhase)) {
      throw new Error("Private Fabric batch transition refused; no replay");
    }
    const required = nextPhase === "claim_intent" ? ["owner_id"] :
      nextPhase === "tab_ready" ? ["tab_id"] :
      nextPhase === "ack_pending" ? ["child_conversation_url"] :
      nextPhase === "completed" ? ["result_path"] : [];
    if (!exact(evidence, required)) {
      throw new Error("Private Fabric batch transition evidence fields invalid");
    }
    if (nextPhase === "claim_intent" && eligibleChild(journal, dispatch) !== requestId) {
      throw new Error("Private Fabric batch previous child unresolved");
    }
    const updated = clone(journal);
    updated.revision++;
    const child = updated.children[index];
    child.phase = nextPhase;
    Object.assign(child, evidence);
    return validateJournal(updated, dispatch);
  }

  function recoveryPlan(journal, dispatch) {
    validateJournal(journal, dispatch);
    return {
      dispatch_id: journal.dispatch_id,
      source_head_sha: journal.source_head_sha,
      revision: journal.revision,
      children: journal.children.map(child => ({
        child_request_id: child.child_request_id,
        phase: child.phase,
        action: ({
          pending: "operator_arm_required",
          claim_intent: "claim_reconciliation_required",
          claim_ambiguous: "manual_claim_resolution_required",
          claimed: "manual_tab_reconciliation_required",
          tab_open_intent: "manual_tab_reconciliation_required",
          tab_ready: "manual_pre_send_authorization_required",
          submission_unknown: "send_unknown_no_replay",
          ack_pending: "receipt_only_reconciliation",
          running: "receipt_only_reconciliation",
          result_pending: "receipt_only_reconciliation",
          completed: "terminal",
          blocked: "manual_resolution_required",
          abandoned: "terminal"
        })[child.phase]
      }))
    };
  }

  async function load(storage, dispatch) {
    const values = await storage.get(STORAGE_KEY);
    if (!Object.prototype.hasOwnProperty.call(values, STORAGE_KEY)) return null;
    return validateJournal(values[STORAGE_KEY], dispatch);
  }

  function verifySingleTransition(previous, next, dispatch) {
    const changed = [];
    for (let i = 0; i < previous.children.length; i++) {
      if (JSON.stringify(previous.children[i]) !== JSON.stringify(next.children[i])) {
        changed.push(i);
      }
    }
    if (changed.length !== 1) {
      throw new Error("Private Fabric batch persistence requires one child transition");
    }
    const target = next.children[changed[0]];
    const evidence = target.phase === "claim_intent"
      ? { owner_id: target.owner_id }
      : target.phase === "tab_ready" ? { tab_id: target.tab_id }
      : target.phase === "ack_pending"
        ? { child_conversation_url: target.child_conversation_url }
        : target.phase === "completed" ? { result_path: target.result_path } : {};
    const derived = transition(
      previous, dispatch, target.child_request_id, target.phase, evidence
    );
    if (JSON.stringify(derived) !== JSON.stringify(next)) {
      throw new Error("Private Fabric batch persisted state skips an admitted transition");
    }
  }

  async function persist(storage, dispatch, next, expected = null) {
    validateJournal(next, dispatch);
    const current = await load(storage, dispatch);
    if (expected === null) {
      if (current !== null || next.revision !== 0) {
        throw new Error("Private Fabric batch journal already exists or has invalid initial revision");
      }
    } else if (!current || JSON.stringify(current) !== JSON.stringify(expected) ||
        next.revision !== current.revision + 1) {
      throw new Error("Private Fabric batch journal changed; fail closed");
    } else {
      verifySingleTransition(current, next, dispatch);
    }
    await storage.set({ [STORAGE_KEY]: next });
    const verified = await load(storage, dispatch);
    if (JSON.stringify(verified) !== JSON.stringify(next)) {
      throw new Error("Private Fabric batch journal persistence unconfirmed");
    }
    return verified;
  }

  return Object.freeze({
    STORAGE_KEY, initializeJournal, validateJournal, eligibleChild,
    transition, recoveryPlan, load, persist, resultPath
  });
});
