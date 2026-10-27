(function initLocalAgentGithubFabricDispatchModel(root, factory) {
  const api = factory();
  root.LocalAgentGithubFabricDispatchModel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createGithubFabricDispatchModel() {
  "use strict";

  const SCHEMA_VERSION = 1;
  const SPAWN_SCHEMA_VERSION = 1;
  const MAX_CHILDREN = 4;
  const MAX_BOOTSTRAP_CHARS = 32_768;
  const MAX_DISPATCH_BYTES = 128 * 1024;
  const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
  const CHILD_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;
  const CAMPAIGN_ID_RE = /^cf-[0-9a-f]{16}$/;
  const TRANSACTION_RE = /^spawn-[0-9a-f]{64}$/;
  const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
  const CHILD_ROLES = new Set(["research", "implementation", "verification", "integration"]);

  function exactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return actual.length === wanted.length &&
      actual.every((key, index) => key === wanted[index]);
  }

  function canonicalize(value) {
    if (Array.isArray(value)) return value.map(canonicalize);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalize(value[key])])
    );
  }

  function canonicalJson(value) {
    return JSON.stringify(canonicalize(value));
  }

  function utf8Length(value) {
    return new TextEncoder().encode(String(value)).length;
  }

  function canonicalParentUrl(value) {
    if (typeof value !== "string" || !value || value !== value.trim()) return "";
    try {
      const parsed = new URL(value);
      if (
        parsed.protocol !== "https:" ||
        parsed.hostname !== "chatgpt.com" ||
        parsed.username ||
        parsed.password ||
        parsed.port ||
        parsed.search ||
        parsed.hash
      ) return "";
      const match = parsed.pathname.match(/^\/c\/([A-Za-z0-9_-]{1,200})\/?$/);
      if (!match) return "";
      return `https://chatgpt.com/c/${match[1]}`;
    } catch (_error) {
      return "";
    }
  }

  function validateSpawn(spawn) {
    if (!exactKeys(spawn, [
      "schema_version",
      "transaction_id",
      "child_request_digest",
      "bootstrap_digest",
      "bootstrap_text"
    ])) {
      throw new Error("GitHub Fabric child spawn fields do not match schema");
    }
    if (spawn.schema_version !== SPAWN_SCHEMA_VERSION) {
      throw new Error(`GitHub Fabric spawn schema_version must be ${SPAWN_SCHEMA_VERSION}`);
    }
    if (!TRANSACTION_RE.test(String(spawn.transaction_id || ""))) {
      throw new Error("GitHub Fabric spawn transaction_id is invalid");
    }
    if (!DIGEST_RE.test(String(spawn.child_request_digest || ""))) {
      throw new Error("GitHub Fabric spawn child_request_digest is invalid");
    }
    if (!DIGEST_RE.test(String(spawn.bootstrap_digest || ""))) {
      throw new Error("GitHub Fabric spawn bootstrap_digest is invalid");
    }
    if (
      typeof spawn.bootstrap_text !== "string" ||
      !spawn.bootstrap_text.trim() ||
      spawn.bootstrap_text.length > MAX_BOOTSTRAP_CHARS
    ) {
      throw new Error("GitHub Fabric spawn bootstrap_text must be non-empty bounded text");
    }
    return spawn;
  }

  function validateDispatch(dispatch) {
    if (!exactKeys(dispatch, [
      "schema_version",
      "operation",
      "id",
      "request_id",
      "request_digest",
      "campaign_id",
      "parent_conversation_url",
      "children"
    ])) {
      throw new Error("GitHub Fabric dispatch fields do not match schema");
    }
    if (dispatch.schema_version !== SCHEMA_VERSION || dispatch.operation !== "delegate") {
      throw new Error("GitHub Fabric dispatch must be schema_version=1 operation=delegate");
    }
    if (!ID_RE.test(String(dispatch.id || ""))) {
      throw new Error("GitHub Fabric dispatch id is invalid");
    }
    if (!ID_RE.test(String(dispatch.request_id || ""))) {
      throw new Error("GitHub Fabric request_id is invalid");
    }
    if (!DIGEST_RE.test(String(dispatch.request_digest || ""))) {
      throw new Error("GitHub Fabric request_digest is invalid");
    }
    if (!CAMPAIGN_ID_RE.test(String(dispatch.campaign_id || ""))) {
      throw new Error("GitHub Fabric campaign_id is invalid");
    }
    const parent = canonicalParentUrl(dispatch.parent_conversation_url);
    if (!parent || parent !== dispatch.parent_conversation_url) {
      throw new Error("GitHub Fabric parent_conversation_url must be canonical");
    }
    if (!Array.isArray(dispatch.children) || !dispatch.children.length || dispatch.children.length > MAX_CHILDREN) {
      throw new Error(`GitHub Fabric children must contain 1..${MAX_CHILDREN} items`);
    }

    const childIds = new Set();
    const requestIds = new Set();
    const transactions = new Set();
    for (const child of dispatch.children) {
      if (!exactKeys(child, ["id", "request_id", "role", "spawn"])) {
        throw new Error("GitHub Fabric child fields do not match schema");
      }
      if (!CHILD_ID_RE.test(String(child.id || "")) || childIds.has(child.id)) {
        throw new Error("GitHub Fabric child id is invalid or duplicated");
      }
      if (!ID_RE.test(String(child.request_id || "")) || requestIds.has(child.request_id)) {
        throw new Error("GitHub Fabric child request_id is invalid or duplicated");
      }
      if (!CHILD_ROLES.has(child.role)) {
        throw new Error("GitHub Fabric child role is invalid");
      }
      validateSpawn(child.spawn);
      if (transactions.has(child.spawn.transaction_id)) {
        throw new Error("GitHub Fabric spawn transaction_id is duplicated");
      }
      childIds.add(child.id);
      requestIds.add(child.request_id);
      transactions.add(child.spawn.transaction_id);
    }

    if (utf8Length(canonicalJson(dispatch)) > MAX_DISPATCH_BYTES) {
      throw new Error(`GitHub Fabric dispatch exceeds ${MAX_DISPATCH_BYTES} bytes`);
    }
    return dispatch;
  }

  function toConversationSpawnIntent(child) {
    if (!child || typeof child !== "object") {
      throw new Error("GitHub Fabric child is required");
    }
    validateSpawn(child.spawn);
    return {
      schema_version: child.spawn.schema_version,
      transaction_id: child.spawn.transaction_id,
      child_request_digest: child.spawn.child_request_digest,
      bootstrap_digest: child.spawn.bootstrap_digest,
      bootstrap_text: child.spawn.bootstrap_text,
      tab_id: null
    };
  }

  function reconcileDispatch(existing, candidate) {
    validateDispatch(candidate);
    if (existing === null || existing === undefined) return candidate;
    validateDispatch(existing);
    if (existing.id !== candidate.id) {
      throw new Error("GitHub Fabric dispatch id mismatch");
    }
    if (canonicalJson(existing) !== canonicalJson(candidate)) {
      throw new Error("GitHub Fabric same-id dispatch conflict");
    }
    return existing;
  }

  return Object.freeze({
    SCHEMA_VERSION,
    SPAWN_SCHEMA_VERSION,
    MAX_CHILDREN,
    MAX_BOOTSTRAP_CHARS,
    MAX_DISPATCH_BYTES,
    CHILD_ROLES,
    canonicalJson,
    canonicalParentUrl,
    validateDispatch,
    toConversationSpawnIntent,
    reconcileDispatch
  });
});
