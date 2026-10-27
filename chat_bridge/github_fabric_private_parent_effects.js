(function initPrivateParentEffects(root, factory) {
  const api = factory();
  root.LocalAgentPrivateParentEffects = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateParentEffects() {
  "use strict";

  // Read-only candidate evidence. NO SEND AUTHORIZATION, recovery permit,
  // private token access, GitHub write, or browser effect.
  const PARENT_RE = /^parent-[0-9a-f]{32}$/;
  const OWNER_RE = /^[0-9a-f]{32}$/;
  const DISPATCH_RE = /^fabric-[0-9a-f]{32}$/;
  const REQUEST_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
  const TRANSACTION_RE = /^spawn-[0-9a-f]{64}$/;
  const SHA_RE = /^[0-9a-f]{40}$/;
  const EFFECT_RE = /^effect-[0-9a-f]{32}$/;
  const MAX_EFFECTS = 4;
  const MAX_RECORD_BYTES = 8192;
  const HEADER_FIELDS = [
    "schema_version", "parent_id", "parent_conversation_url",
    "fence_epoch", "owner_id", "dispatch_id", "revision", "effects"
  ];
  const ENTRY_FIELDS = [
    "effect_id", "child_request_id", "spawn_transaction_id",
    "phase", "result_source_head_sha"
  ];

  function exact(value, fields) {
    return value !== null && typeof value === "object" &&
      !Array.isArray(value) &&
      Object.keys(value).sort().join("|") === [...fields].sort().join("|");
  }

  function hex(bytes) {
    return Array.from(new Uint8Array(bytes), n =>
      n.toString(16).padStart(2, "0")).join("");
  }

  function validateExpectedChildren(children) {
    if (!Array.isArray(children) || children.length < 1 ||
        children.length > MAX_EFFECTS) {
      throw new Error("Private parent effects expected children required");
    }
    const seenIds = new Set(), seenTransactions = new Set();
    for (const child of children) {
      if (!exact(child, ["child_request_id", "spawn_transaction_id"]) ||
          typeof child.child_request_id !== "string" ||
          !REQUEST_RE.test(child.child_request_id) ||
          typeof child.spawn_transaction_id !== "string" ||
          !TRANSACTION_RE.test(child.spawn_transaction_id) ||
          seenIds.has(child.child_request_id) ||
          seenTransactions.has(child.spawn_transaction_id)) {
        throw new Error("Private parent effects expected child identities invalid");
      }
      seenIds.add(child.child_request_id);
      seenTransactions.add(child.spawn_transaction_id);
    }
    return children;
  }

  async function expectedEffectId(parent, child) {
    if (!globalThis.crypto?.subtle) {
      throw new Error("Private parent effects SHA-256 unavailable");
    }
    const values = [
      "fabric-parent-send-intent-v1", parent.id, parent.fence_epoch,
      parent.owner_id, parent.dispatch_id,
      child.child_request_id, child.spawn_transaction_id
    ];
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(JSON.stringify(values))
    );
    return "effect-" + hex(digest).slice(0, 32);
  }

  async function validateEffectLedger(value, parent, expectedChildren) {
    // Expected children MUST come from a separately authenticated immutable
    // dispatch. Without them, fail closed rather than infer child admission
    // from an attacker-chosen effects record.
    validateExpectedChildren(expectedChildren);
    if (!exact(parent, [
      "schema_version", "kind", "id", "parent_conversation_url",
      "fence_epoch", "owner_id", "dispatch_id", "phase", "completion"
    ]) || parent.schema_version !== 1 ||
        !PARENT_RE.test(parent.id) ||
        !OWNER_RE.test(parent.owner_id) ||
        !DISPATCH_RE.test(parent.dispatch_id) ||
        !Number.isSafeInteger(parent.fence_epoch) ||
        parent.fence_epoch < 1 || parent.fence_epoch > 32) {
      throw new Error("Private parent effects parent evidence invalid");
    }
    if (!exact(value, HEADER_FIELDS) || value.schema_version !== 1 ||
        value.parent_id !== parent.id ||
        value.parent_conversation_url !== parent.parent_conversation_url ||
        value.fence_epoch !== parent.fence_epoch ||
        value.owner_id !== parent.owner_id ||
        value.dispatch_id !== parent.dispatch_id ||
        !Number.isSafeInteger(value.revision) ||
        !Array.isArray(value.effects) ||
        value.effects.length < 1 ||
        value.effects.length > expectedChildren.length ||
        new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_RECORD_BYTES) {
      throw new Error("Private parent effects ledger header invalid");
    }
    let confirmations = 0, freezes = 0;
    for (let i = 0; i < value.effects.length; i++) {
      const entry = value.effects[i], expected = expectedChildren[i];
      if (!exact(entry, ENTRY_FIELDS) ||
          entry.child_request_id !== expected.child_request_id ||
          entry.spawn_transaction_id !== expected.spawn_transaction_id ||
          typeof entry.effect_id !== "string" ||
          !EFFECT_RE.test(entry.effect_id) ||
          entry.effect_id !== await expectedEffectId(parent, expected) ||
          !["send_unknown", "result_verified", "frozen_unknown"].includes(entry.phase) ||
          typeof entry.result_source_head_sha !== "string" ||
          (entry.phase === "result_verified"
            ? !SHA_RE.test(entry.result_source_head_sha)
            : entry.result_source_head_sha !== "") ||
          (i > 0 && value.effects[i - 1].phase !== "result_verified")) {
        throw new Error("Private parent effects immutable child identity or phase invalid");
      }
      if (entry.phase === "result_verified") confirmations++;
      if (entry.phase === "frozen_unknown") freezes++;
    }
    if (value.revision !== value.effects.length + confirmations + freezes) {
      throw new Error("Private parent effects revision invalid");
    }
    return Object.freeze({
      status: value.effects[value.effects.length - 1].phase === "send_unknown"
        ? "reconcile_only_no_send_replay"
        : value.effects[value.effects.length - 1].phase === "frozen_unknown"
          ? "blocked_unknown_no_takeover" : "verified_no_send_replay",
      revision: value.revision,
      recorded_effects: value.effects.length,
      browser_effects_permitted: false
    });
  }

  return Object.freeze({
    validateEffectLedger, validateExpectedChildren, expectedEffectId,
    MAX_EFFECTS, MAX_RECORD_BYTES
  });
});
