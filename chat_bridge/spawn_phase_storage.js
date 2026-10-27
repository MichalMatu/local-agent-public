(function initLocalAgentSpawnPhaseStorage(root, factory) {
  const model = root.LocalAgentSpawnPhaseModel ||
    (typeof require === "function" ? require("./spawn_phase_model.js") : null);
  const api = factory(model);
  root.LocalAgentSpawnPhaseStorage = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createSpawnPhaseStorage(model) {
  "use strict";

  // Not imported by the production extension. Integration requires an
  // explicit feature gate plus page-local failure-injection acceptance.
  if (!model) throw new Error("spawn phase model is required");
  const PREFIX = "local-agent-spawn-phase-v2:";

  function requireStorage(storage) {
    if (!storage || typeof storage.getItem !== "function" ||
        typeof storage.setItem !== "function") {
      throw new TypeError("page-local storage must implement getItem/setItem");
    }
    return storage;
  }

  function keyFor(intent) {
    const validated = model.newPreparedClaim(intent);
    return PREFIX + validated.transaction_id;
  }

  function read(storage, intent) {
    const key = keyFor(intent);
    const raw = requireStorage(storage).getItem(key);
    if (raw === null) return null;
    if (typeof raw !== "string" || raw.length > 4096) {
      throw new Error("invalid page-local claim encoding");
    }
    let existing;
    try {
      existing = JSON.parse(raw);
    } catch (_error) {
      throw new Error("page-local spawn claim is corrupted");
    }
    model.validateClaim(existing);
    // A same-ID, different-payload claim is not a missing claim.
    model.reconcileClaim(existing, intent);
    return existing;
  }

  function persist(storage, intent, claim) {
    model.validateClaim(claim);
    model.reconcileClaim(claim, intent);
    requireStorage(storage).setItem(keyFor(intent), JSON.stringify(claim));
    const proof = read(storage, intent);
    if (!proof || JSON.stringify(proof) !== JSON.stringify(claim)) {
      throw new Error("page-local spawn claim readback mismatch");
    }
    return proof;
  }

  function prepare(storage, intent) {
    const current = read(storage, intent);
    const resolution = model.reconcileClaim(current, intent);
    return resolution.action === "prepare_new"
      ? { action: "prepare_new", claim: persist(storage, intent, resolution.claim) }
      : resolution;
  }

  function advance(storage, intent, phase) {
    const existing = read(storage, intent);
    if (!existing) throw new Error("page-local spawn claim is missing");
    return persist(storage, intent, model.advanceClaim(existing, phase));
  }

  function retry(storage, intent) {
    const existing = read(storage, intent);
    if (!existing) throw new Error("page-local spawn claim is missing");
    return persist(storage, intent, model.retryClaim(existing));
  }

  function mutatePreparedComposer(storage, intent, mutation) {
    if (typeof mutation !== "function" ||
        mutation.constructor?.name === "AsyncFunction") {
      throw new TypeError("composer mutation callback must be synchronous");
    }
    const claim = read(storage, intent);
    model.assertBeforeComposerMutation(claim, intent);
    // Callback must be synchronous, so there is no asynchronous gap between
    // the readback guard and the first DOM mutation.
    const value = mutation();
    if (value && typeof value.then === "function") {
      throw new Error("composer mutation callback must be synchronous");
    }
    return value;
  }

  async function armAndSend(storage, intent, send) {
    if (typeof send !== "function") throw new TypeError("Send must be a function");
    const current = read(storage, intent);
    if (!current || current.phase !== "draft_verified") {
      throw new Error("Send requires fresh draft_verified claim; reconcile after arm");
    }
    const armed = persist(storage, intent, model.advanceClaim(current, "submit_armed"));
    model.assertBeforeSend(armed, intent);
    // After this boundary, any thrown exception is ambiguous. Never attempt
    // a second Send for this transaction, including after page restart.
    const result = await send();
    persist(storage, intent, model.advanceClaim(armed, "submitted"));
    return result;
  }

  return Object.freeze({
    PREFIX,
    keyFor,
    read,
    prepare,
    advance,
    retry,
    mutatePreparedComposer,
    armAndSend
  });
});
