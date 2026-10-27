(function initLocalAgentGithubFabricIntakeModel(root, factory) {
  const api = factory();
  root.LocalAgentGithubFabricIntakeModel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createGithubFabricIntakeModel() {
  "use strict";

  const INDEX_SCHEMA_VERSION = 1;
  const MAX_INDEX_IDS = 4;
  const MAX_SEEN_DISPATCHES = 128;
  const MAX_INDEX_BYTES = 8192;
  const ID_RE = /^fabric-[0-9a-f]{32}$/;
  const DIGEST_RE = /^[0-9a-f]{64}$/;
  const RUNTIME_URL =
    "https://raw.githubusercontent.com/example-owner/local-agent/chat-bridge-state/chat_bridge/runtime.json";
  const ROOT_URL =
    "https://raw.githubusercontent.com/example-owner/local-agent/chat-bridge-state/";

  function requireTrustedRuntimeUrl(runtimeUrl) {
    if (runtimeUrl !== RUNTIME_URL) {
      throw new Error("GitHub Fabric intake requires the canonical GitHub control runtime URL");
    }
  }

  function indexUrl(runtimeUrl) {
    requireTrustedRuntimeUrl(runtimeUrl);
    return ROOT_URL + ".agent/conversation/browser_dispatches/index.json";
  }

  function dispatchUrl(runtimeUrl, id) {
    requireTrustedRuntimeUrl(runtimeUrl);
    if (typeof id !== "string" || !ID_RE.test(id)) {
      throw new Error("GitHub Fabric dispatch id is invalid");
    }
    return ROOT_URL + ".agent/conversation/browser_dispatches/" + id + ".json";
  }

  function validateIndex(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== "dispatch_ids,schema_version") {
      throw new Error("GitHub Fabric intake index fields do not match schema");
    }
    if (value.schema_version !== INDEX_SCHEMA_VERSION ||
      !Array.isArray(value.dispatch_ids) || value.dispatch_ids.length > MAX_INDEX_IDS) {
      throw new Error("GitHub Fabric intake index version or capacity is invalid");
    }
    const unique = new Set();
    for (const id of value.dispatch_ids) {
      if (typeof id !== "string" || !ID_RE.test(id) || unique.has(id)) {
        throw new Error("GitHub Fabric intake index dispatch id is invalid or duplicated");
      }
      unique.add(id);
    }
    return value;
  }

  function reconcileSeenDispatch(seen, id, fingerprint) {
    if (!seen || typeof seen !== "object" || Array.isArray(seen)) {
      throw new Error("GitHub Fabric intake seen state must be an object");
    }
    if (typeof id !== "string" || !ID_RE.test(id) ||
      typeof fingerprint !== "string" || !DIGEST_RE.test(fingerprint)) {
      throw new Error("GitHub Fabric intake identity is invalid");
    }
    const keys = Object.keys(seen);
    if (keys.length > MAX_SEEN_DISPATCHES ||
      keys.some((key) => !ID_RE.test(key) || !DIGEST_RE.test(seen[key]))) {
      throw new Error("GitHub Fabric intake stored identity state is invalid");
    }
    if (Object.hasOwn(seen, id)) {
      return {
        status: seen[id] === fingerprint ? "replayed" : "conflict",
        seen
      };
    }
    if (keys.length >= MAX_SEEN_DISPATCHES) {
      return { status: "capacity", seen };
    }
    return { status: "stored", seen: { ...seen, [id]: fingerprint } };
  }

  return Object.freeze({
    INDEX_SCHEMA_VERSION,
    MAX_INDEX_IDS,
    MAX_SEEN_DISPATCHES,
    MAX_INDEX_BYTES,
    indexUrl,
    dispatchUrl,
    validateIndex,
    reconcileSeenDispatch
  });
});
