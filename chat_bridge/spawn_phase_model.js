(function initLocalAgentSpawnPhaseModel(root, factory) {
  const api = factory();
  root.LocalAgentSpawnPhaseModel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createSpawnPhaseModel() {
  "use strict";

  const SCHEMA_VERSION = 2;
  const PHASES = Object.freeze([
    "prepared",
    "draft_verified",
    "submit_armed",
    "submitted",
    "response_started"
  ]);
  const TX_RE = /^spawn-[0-9a-f]{64}$/;
  const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
  const CLAIM_KEYS = ["schema_version", "transaction_id", "child_request_digest",
    "bootstrap_digest", "phase", "attempt"];

  function exactKeys(value, keys) {
    return value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Object.keys(value).sort().join(",") === [...keys].sort().join(",");
  }

  function validateIntent(intent) {
    if (!intent || typeof intent !== "object" || Array.isArray(intent) ||
        !TX_RE.test(String(intent.transaction_id || "")) ||
        !DIGEST_RE.test(String(intent.child_request_digest || "")) ||
        !DIGEST_RE.test(String(intent.bootstrap_digest || ""))) {
      throw new Error("invalid immutable spawn intent identity");
    }
    return intent;
  }

  function validateClaim(claim) {
    if (!exactKeys(claim, CLAIM_KEYS) ||
        claim.schema_version !== SCHEMA_VERSION ||
        !TX_RE.test(String(claim.transaction_id || "")) ||
        !DIGEST_RE.test(String(claim.child_request_digest || "")) ||
        !DIGEST_RE.test(String(claim.bootstrap_digest || "")) ||
        !PHASES.includes(claim.phase) ||
        !Number.isSafeInteger(claim.attempt) || claim.attempt < 1 || claim.attempt > 32) {
      throw new Error("invalid page-local spawn phase claim");
    }
    return claim;
  }

  function assertSameIdentity(claim, intent) {
    validateClaim(claim);
    validateIntent(intent);
    if (claim.transaction_id !== intent.transaction_id ||
        claim.child_request_digest !== intent.child_request_digest ||
        claim.bootstrap_digest !== intent.bootstrap_digest) {
      throw new Error("permanent spawn transaction identity conflict");
    }
    return claim;
  }

  function newPreparedClaim(intent) {
    validateIntent(intent);
    return Object.freeze({
      schema_version: SCHEMA_VERSION,
      transaction_id: intent.transaction_id,
      child_request_digest: intent.child_request_digest,
      bootstrap_digest: intent.bootstrap_digest,
      phase: "prepared",
      attempt: 1
    });
  }

  function canRetry(claim) {
    validateClaim(claim);
    return claim.phase === "prepared" || claim.phase === "draft_verified";
  }

  function reconcileClaim(existing, intent) {
    validateIntent(intent);
    if (existing === null || existing === undefined) {
      return Object.freeze({ action: "prepare_new", claim: newPreparedClaim(intent) });
    }
    assertSameIdentity(existing, intent);
    return Object.freeze({
      action: canRetry(existing) ? "resume_before_arm" : "reconcile_only",
      claim: existing
    });
  }

  function advanceClaim(existing, nextPhase) {
    validateClaim(existing);
    if (!PHASES.includes(nextPhase)) throw new Error("unknown spawn phase");
    const currentIndex = PHASES.indexOf(existing.phase);
    const nextIndex = PHASES.indexOf(nextPhase);
    if (nextIndex === currentIndex) return existing;
    if (nextIndex !== currentIndex + 1) {
      throw new Error("spawn phase transition must be monotonic and adjacent");
    }
    return Object.freeze({ ...existing, phase: nextPhase });
  }

  function retryClaim(existing) {
    validateClaim(existing);
    if (!canRetry(existing)) {
      throw new Error("spawn retry forbidden after submit_armed");
    }
    if (existing.attempt >= 32) throw new Error("spawn retry limit exceeded");
    return Object.freeze({ ...existing, phase: "prepared", attempt: existing.attempt + 1 });
  }

  // The caller must persist and read back the prepared page-local claim before
  // invoking this guard, and must persist+read back submit_armed before Send.
  // This pure model intentionally performs no DOM or storage operation.
  function assertBeforeComposerMutation(persistedClaim, intent) {
    assertSameIdentity(persistedClaim, intent);
    if (persistedClaim.phase !== "prepared") {
      throw new Error("composer mutation requires persisted prepared claim");
    }
    return true;
  }

  function assertBeforeSend(persistedClaim, intent) {
    assertSameIdentity(persistedClaim, intent);
    if (persistedClaim.phase !== "submit_armed") {
      throw new Error("Send requires persisted submit_armed claim");
    }
    return true;
  }

  return Object.freeze({
    SCHEMA_VERSION,
    PHASES,
    validateClaim,
    newPreparedClaim,
    reconcileClaim,
    advanceClaim,
    retryClaim,
    canRetry,
    assertBeforeComposerMutation,
    assertBeforeSend
  });
});
