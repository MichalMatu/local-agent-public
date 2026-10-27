(function initPrivateParentOwnershipReader(root, factory) {
  const effects = root.LocalAgentPrivateParentEffects ||
    (typeof require === "function" ? require("./github_fabric_private_parent_effects.js") : null);
  const pinnedDispatch = root.LocalAgentPrivateParentPinnedDispatch ||
    (typeof require === "function" ? require("./github_fabric_private_parent_pinned_dispatch.js") : null);
  const api = factory(effects, pinnedDispatch);
  root.LocalAgentPrivateParentOwnershipReader = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateParentOwnershipReader(effectsModel, pinnedDispatchModel) {
  "use strict";

  // Candidate-only: NOT imported by the installed MV3 worker. This reads
  // parent ownership evidence; it is NEVER a browser Send grant. A pinned
  // snapshot is stale immediately after reading and cannot fence a race.
  const API = "https://api.github.com/repos/example-owner/local-agent-fabric-private";
  const BRANCH = "fabric-data";
  const BASE = "projects/local-agent/parent_ownership/";
  const INDEX_PATH = BASE + "index.json";
  const MAX_PARENTS = 16;
  const MAX_EPOCHS = 32;
  const MAX_TREE_ITEMS = 4096;
  const MAX_TREE_RESPONSE = 512 * 1024;
  const MAX_JSON_RESPONSE = 20 * 1024;
  const MAX_RECORD_BYTES = 4096;
  const MAX_HISTORY_BYTES = 8192;
  const MAX_EFFECT_BYTES = 8192;
  const MAX_INDEX_BYTES = 4096;
  const SHA_RE = /^[0-9a-f]{40}$/;
  const OWNER_RE = /^[0-9a-f]{32}$/;
  const PARENT_RE = /^parent-[0-9a-f]{32}$/;
  const DISPATCH_RE = /^fabric-[0-9a-f]{32}$/;
  const TOKEN_RE = /^[A-Za-z0-9_-]{12,256}$/;
  const EXACT_PARENT = [
    "schema_version", "kind", "id", "parent_conversation_url",
    "fence_epoch", "owner_id", "dispatch_id", "phase", "completion"
  ];
  const PHASE_COMPLETION = Object.freeze({
    active: "none", completed: "verified", unknown_frozen: "unresolved"
  });

  function exact(value, fields) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("|") === [...fields].sort().join("|");
  }

  function canonicalParent(value) {
    if (typeof value !== "string" || !value || value !== value.trim()) return "";
    try {
      const u = new URL(value);
      const match = u.pathname.match(/^\/c\/([A-Za-z0-9_-]{1,200})\/?$/);
      if (u.protocol !== "https:" || u.hostname !== "chatgpt.com" ||
          u.port || u.username || u.password || u.search || u.hash || !match) {
        return "";
      }
      return "https://chatgpt.com/c/" + match[1];
    } catch (_error) { return ""; }
  }

  function hex(bytes) {
    return Array.from(new Uint8Array(bytes), value =>
      value.toString(16).padStart(2, "0")).join("");
  }

  async function parentId(url) {
    if (!canonicalParent(url) || canonicalParent(url) !== url ||
        !globalThis.crypto?.subtle) {
      throw new Error("Private parent candidate canonical identity or digest unavailable");
    }
    const encoded = new TextEncoder().encode(JSON.stringify([
      "fabric-global-parent-mode-v1", url
    ]));
    const hashed = await globalThis.crypto.subtle.digest("SHA-256", encoded);
    return "parent-" + hex(hashed).slice(0, 32);
  }

  function recordPath(id) { return BASE + id + ".json"; }
  function historyPath(id) { return BASE + "history/" + id + ".json"; }
  function effectsPath(id) { return BASE + "effects/" + id + ".json"; }
  function witnessPath(id, epoch) {
    return BASE + "epochs/" + id + "/" + String(epoch).padStart(4, "0") + ".json";
  }

  function validateIndex(value) {
    if (!exact(value, ["schema_version", "parent_ids"]) ||
        value.schema_version !== 1 || !Array.isArray(value.parent_ids) ||
        value.parent_ids.length > MAX_PARENTS ||
        value.parent_ids.some(id => typeof id !== "string" || !PARENT_RE.test(id)) ||
        value.parent_ids.join("|") !== [...new Set(value.parent_ids)].sort().join("|")) {
      throw new Error("Private parent candidate index invalid");
    }
    return value;
  }

  async function validateParent(record) {
    if (!exact(record, EXACT_PARENT) || record.schema_version !== 1 ||
        record.kind !== "private_parent_ownership_candidate_v1" ||
        typeof record.parent_conversation_url !== "string" ||
        record.parent_conversation_url !== canonicalParent(record.parent_conversation_url) ||
        record.id !== await parentId(record.parent_conversation_url) ||
        typeof record.owner_id !== "string" || !OWNER_RE.test(record.owner_id) ||
        typeof record.dispatch_id !== "string" || !DISPATCH_RE.test(record.dispatch_id) ||
        !Number.isSafeInteger(record.fence_epoch) ||
        record.fence_epoch < 1 || record.fence_epoch > MAX_EPOCHS ||
        typeof record.phase !== "string" ||
        !Object.prototype.hasOwnProperty.call(PHASE_COMPLETION, record.phase) ||
        record.completion !== PHASE_COMPLETION[record.phase]) {
      throw new Error("Private parent candidate ownership record invalid");
    }
    return record;
  }

  function validateHistory(history, id) {
    if (!exact(history, ["schema_version", "parent_id", "entries"]) ||
        history.schema_version !== 1 || history.parent_id !== id ||
        !Array.isArray(history.entries) ||
        history.entries.length < 1 || history.entries.length > MAX_EPOCHS) {
      throw new Error("Private parent candidate epoch history invalid");
    }
    const dispatches = new Set(), owners = new Set();
    for (let index = 0; index < history.entries.length; index++) {
      const entry = history.entries[index];
      if (!exact(entry, ["epoch", "dispatch_id", "owner_id"]) ||
          entry.epoch !== index + 1 ||
          typeof entry.dispatch_id !== "string" || !DISPATCH_RE.test(entry.dispatch_id) ||
          typeof entry.owner_id !== "string" || !OWNER_RE.test(entry.owner_id) ||
          owners.has(entry.owner_id) || dispatches.has(entry.dispatch_id)) {
        throw new Error("Private parent candidate epoch identity conflict");
      }
      owners.add(entry.owner_id);
      dispatches.add(entry.dispatch_id);
    }
    return history;
  }

  // Every GitHub request has a whole-body deadline and a strict byte cap.
  async function requestJson(fetchImpl, token, path, maximum) {
    const url = API + path;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 5000);
    try {
      let response;
      try {
        response = await fetchImpl(url, {
          method: "GET", redirect: "error", cache: "no-store", credentials: "omit",
          signal: abort.signal,
          headers: {
            Authorization: "Bearer " + token,
            Accept: "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
          }
        });
      } catch (_error) {
        throw new Error("Private parent candidate read network unavailable");
      }
      if (!response || response.status !== 200 || response.redirected ||
          (response.url && response.url !== url) ||
          !response.body || typeof response.body.getReader !== "function") {
        throw new Error("Private parent candidate response rejected");
      }
      const reader = response.body.getReader(), parts = [];
      let total = 0;
      try {
        while (true) {
          const next = await reader.read();
          if (next.done) break;
          if (!(next.value instanceof Uint8Array) ||
              (total += next.value.byteLength) > maximum) {
            throw new Error("Private parent candidate response exceeds bound");
          }
          parts.push(next.value);
        }
      } catch (_error) {
        await reader.cancel().catch(() => undefined);
        throw new Error("Private parent candidate response incomplete");
      } finally {
        try { reader.releaseLock(); } catch (_error) {}
      }
      const bytes = new Uint8Array(total);
      let offset = 0;
      for (const part of parts) { bytes.set(part, offset); offset += part.length; }
      try {
        const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
        if (!value || typeof value !== "object" || Array.isArray(value)) throw Error();
        return value;
      } catch (_error) {
        throw new Error("Private parent candidate response JSON invalid");
      }
    } finally {
      clearTimeout(timer);
    }
  }

  async function pinnedFile(fetchImpl, token, head, path, blobSha, bound) {
    const responseBound = Math.max(MAX_JSON_RESPONSE, Math.ceil(bound * 4 / 3) + 8192);
    const metadata = await requestJson(fetchImpl, token,
      "/contents/" + path + "?ref=" + head, responseBound);
    if (!metadata || metadata.type !== "file" || metadata.path !== path ||
        metadata.sha !== blobSha || metadata.encoding !== "base64" ||
        !Number.isSafeInteger(metadata.size) || metadata.size < 0 ||
        metadata.size > bound || typeof metadata.content !== "string") {
      throw new Error("Private parent candidate pinned file metadata invalid");
    }
    const encoded = metadata.content.replace(/[\r\n]/g, "");
    if (encoded.length % 4 !== 0 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      throw new Error("Private parent candidate base64 invalid");
    }
    let raw;
    try { raw = Uint8Array.from(atob(encoded), c => c.charCodeAt(0)); }
    catch (_error) { throw new Error("Private parent candidate base64 invalid"); }
    if (raw.length !== metadata.size ||
        !globalThis.crypto?.subtle) {
      throw new Error("Private parent candidate pinned byte count/digest unavailable");
    }
    const header = new TextEncoder().encode("blob " + raw.length + "\0");
    const preimage = new Uint8Array(header.length + raw.length);
    preimage.set(header);
    preimage.set(raw, header.length);
    if (hex(await globalThis.crypto.subtle.digest("SHA-1", preimage)) !== blobSha) {
      throw new Error("Private parent candidate pinned blob digest mismatch");
    }
    try {
      const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
      if (!value || typeof value !== "object" || Array.isArray(value)) throw Error();
      return value;
    } catch (_error) { throw new Error("Private parent candidate pinned JSON invalid"); }
  }

  async function readPrivateParentOwnershipSnapshot({
    enabled = false, readToken, parentConversationUrl,
    expectedOwnerId, expectedDispatchId, expectedEpoch,
    fetchImpl = globalThis.fetch
  } = {}) {
    if (enabled !== true) throw new Error("Private parent candidate reader default-disabled");
    if (typeof readToken !== "string" || !TOKEN_RE.test(readToken)) {
      throw new Error("Private parent candidate read token invalid");
    }
    if (typeof fetchImpl !== "function") {
      throw new Error("Private parent candidate fetch unavailable");
    }
    if (typeof parentConversationUrl !== "string" ||
        !canonicalParent(parentConversationUrl) ||
        canonicalParent(parentConversationUrl) !== parentConversationUrl ||
        typeof expectedOwnerId !== "string" || !OWNER_RE.test(expectedOwnerId) ||
        typeof expectedDispatchId !== "string" || !DISPATCH_RE.test(expectedDispatchId) ||
        !Number.isSafeInteger(expectedEpoch) ||
        expectedEpoch < 1 || expectedEpoch > MAX_EPOCHS) {
      throw new Error("Private parent candidate expected identity invalid");
    }
    const id = await parentId(parentConversationUrl);
    const ref = await requestJson(fetchImpl, readToken,
      "/git/ref/heads/" + BRANCH, 4096);
    const head = ref?.object?.sha;
    if (ref.ref !== "refs/heads/" + BRANCH ||
        ref.object?.type !== "commit" || typeof head !== "string" ||
        !SHA_RE.test(head)) {
      throw new Error("Private parent candidate ref invalid");
    }
    const commit = await requestJson(fetchImpl, readToken,
      "/git/commits/" + head, 8192);
    const treeSha = commit?.tree?.sha;
    if (typeof treeSha !== "string" || !SHA_RE.test(treeSha)) {
      throw new Error("Private parent candidate commit invalid");
    }
    const tree = await requestJson(fetchImpl, readToken,
      "/git/trees/" + treeSha + "?recursive=1", MAX_TREE_RESPONSE);
    if (tree.sha !== treeSha || tree.truncated !== false ||
        !Array.isArray(tree.tree) || tree.tree.length > MAX_TREE_ITEMS) {
      throw new Error("Private parent candidate tree incomplete");
    }
    const blobs = new Map();
    const roots = new Set();
    const allowedDirectories = new Set([
      BASE.slice(0, -1), BASE + "history", BASE + "epochs", BASE + "effects"
    ]);
    for (const entry of tree.tree) {
      if (!entry || typeof entry.path !== "string" ||
          typeof entry.sha !== "string" || !SHA_RE.test(entry.sha)) {
        throw new Error("Private parent candidate tree entry invalid");
      }
      if (allowedDirectories.has(entry.path) ||
          /^projects\/local-agent\/parent_ownership\/epochs\/parent-[0-9a-f]{32}$/.test(entry.path)) {
        if (entry.type !== "tree" || entry.mode !== "040000" ||
            roots.has(entry.path)) {
          throw new Error("Private parent candidate tree directory invalid");
        }
        roots.add(entry.path);
      } else if (entry.path.startsWith(BASE)) {
        if (entry.type !== "blob" || entry.mode !== "100644" ||
            blobs.has(entry.path) ||
            !(entry.path === INDEX_PATH ||
              /^projects\/local-agent\/parent_ownership\/parent-[0-9a-f]{32}\.json$/.test(entry.path) ||
              /^projects\/local-agent\/parent_ownership\/history\/parent-[0-9a-f]{32}\.json$/.test(entry.path) ||
              /^projects\/local-agent\/parent_ownership\/effects\/parent-[0-9a-f]{32}\.json$/.test(entry.path) ||
              /^projects\/local-agent\/parent_ownership\/epochs\/parent-[0-9a-f]{32}\/[0-9]{4}\.json$/.test(entry.path))) {
          throw new Error("Private parent candidate unexpected or duplicate tree entry");
        }
        blobs.set(entry.path, entry.sha);
      }
    }
    if (!blobs.has(INDEX_PATH)) {
      if (blobs.size) throw new Error("Private parent candidate orphan record");
      return Object.freeze({
        source: "authenticated_private_parent_candidate_observation",
        source_head_sha: head, parent_id: id, status: "unregistered",
        browser_effects_permitted: false
      });
    }
    if (!roots.has(BASE.slice(0, -1))) {
      throw new Error("Private parent candidate root directory missing");
    }
    const index = validateIndex(await pinnedFile(
      fetchImpl, readToken, head, INDEX_PATH, blobs.get(INDEX_PATH), MAX_INDEX_BYTES
    ));
    const known = new Set(index.parent_ids);
    const seenParents = new Set(), seenHistory = new Set();
    const seenEffects = new Set(), seenWitness = new Map();
    for (const path of blobs.keys()) {
      if (path === INDEX_PATH) continue;
      let match = path.match(/^projects\/local-agent\/parent_ownership\/(parent-[0-9a-f]{32})\.json$/);
      if (match) {
        if (!known.has(match[1])) throw new Error("Private parent candidate orphan owner");
        seenParents.add(match[1]);
        continue;
      }
      match = path.match(/^projects\/local-agent\/parent_ownership\/history\/(parent-[0-9a-f]{32})\.json$/);
      if (match) {
        if (!known.has(match[1])) throw new Error("Private parent candidate orphan history");
        seenHistory.add(match[1]);
        continue;
      }
      match = path.match(/^projects\/local-agent\/parent_ownership\/effects\/(parent-[0-9a-f]{32})\.json$/);
      if (match) {
        if (!known.has(match[1])) throw new Error("Private parent candidate orphan effects");
        if (!roots.has(BASE + "effects")) {
          throw new Error("Private parent candidate effects directory missing");
        }
        seenEffects.add(match[1]);
        continue;
      }
      match = path.match(/^projects\/local-agent\/parent_ownership\/epochs\/(parent-[0-9a-f]{32})\/([0-9]{4})\.json$/);
      if (!match || !known.has(match[1])) {
        throw new Error("Private parent candidate orphan witness");
      }
      const paths = seenWitness.get(match[1]) || new Set();
      paths.add(path);
      seenWitness.set(match[1], paths);
    }
    if (seenParents.size !== known.size || seenHistory.size !== known.size ||
        [...known].some(pid => !seenParents.has(pid) || !seenHistory.has(pid))) {
      throw new Error("Private parent candidate indexed record/history missing");
    }
    if (!known.has(id)) {
      return Object.freeze({
        source: "authenticated_private_parent_candidate_observation",
        source_head_sha: head, parent_id: id, status: "unregistered",
        browser_effects_permitted: false
      });
    }
    const record = await validateParent(await pinnedFile(
      fetchImpl, readToken, head, recordPath(id), blobs.get(recordPath(id)), MAX_RECORD_BYTES
    ));
    const history = validateHistory(await pinnedFile(
      fetchImpl, readToken, head, historyPath(id), blobs.get(historyPath(id)), MAX_HISTORY_BYTES
    ), id);
    const witnessed = seenWitness.get(id) || new Set();
    if (witnessed.size !== history.entries.length) {
      throw new Error("Private parent candidate epoch witness count conflict");
    }
    for (const entry of history.entries) {
      const path = witnessPath(id, entry.epoch);
      if (!witnessed.has(path)) {
        throw new Error("Private parent candidate epoch witness missing");
      }
      const witness = await validateParent(await pinnedFile(
        fetchImpl, readToken, head, path, blobs.get(path), MAX_RECORD_BYTES
      ));
      if (witness.id !== id || witness.fence_epoch !== entry.epoch ||
          witness.dispatch_id !== entry.dispatch_id ||
          witness.owner_id !== entry.owner_id || witness.phase !== "active") {
        throw new Error("Private parent candidate immutable epoch witness conflict");
      }
    }
    const latest = history.entries[history.entries.length - 1];
    if (record.id !== id || record.fence_epoch !== latest.epoch ||
        record.owner_id !== latest.owner_id ||
        record.dispatch_id !== latest.dispatch_id ||
        record.parent_conversation_url !== parentConversationUrl) {
      throw new Error("Private parent candidate current owner conflicts with epoch history");
    }
    // An optional effect ledger MUST be valid against immutable child
    // identity evidence provided by a separately authenticated dispatch.
    // Without those expected children, a present ledger fails closed.
    let effectObservation = Object.freeze({
      status: "unarmed", recorded_effects: 0, browser_effects_permitted: false
    });
    if (seenEffects.has(id)) {
      if (!effectsModel || typeof effectsModel.validateEffectLedger !== "function") {
        throw new Error("Private parent candidate effect validator unavailable");
      }
      const path = effectsPath(id);
      const ledger = await pinnedFile(
        fetchImpl, readToken, head, path, blobs.get(path), MAX_EFFECT_BYTES
      );
      if (!pinnedDispatchModel ||
          typeof pinnedDispatchModel.readPinnedDispatchChildren !== "function") {
        throw new Error("Private parent candidate pinned dispatch validator unavailable");
      }
      const readPinned = async (dispatchPath, bound) => {
        const blobSha = [...tree.tree].find(entry =>
          entry.path === dispatchPath && entry.type === "blob" &&
          entry.mode === "100644" && typeof entry.sha === "string"
        )?.sha;
        if (!blobSha || !SHA_RE.test(blobSha) ||
            tree.tree.filter(entry => entry.path === dispatchPath).length !== 1) {
          throw new Error("Private parent candidate pinned dispatch tree path invalid");
        }
        return pinnedFile(fetchImpl, readToken, head, dispatchPath, blobSha, bound);
      };
      const children = await pinnedDispatchModel.readPinnedDispatchChildren({
        expectedDispatchId: record.dispatch_id,
        expectedParentUrl: record.parent_conversation_url,
        readPinned
      });
      effectObservation = await effectsModel.validateEffectLedger(
        ledger, record, children
      );
    }
    // Do not use this snapshot as a Send authorization: it may be stale
    // before the next microtask, and legacy DOM drivers are not fenced.
    const matches = record.owner_id === expectedOwnerId &&
      record.dispatch_id === expectedDispatchId &&
      record.fence_epoch === expectedEpoch;
    return Object.freeze({
      source: "authenticated_private_parent_candidate_observation",
      source_head_sha: head, parent_id: id,
      status: matches ? (
        record.phase === "active" ? "matching_active_candidate" :
          record.phase === "completed" ? "matching_completed_candidate" :
            "matching_frozen_candidate"
      ) : "competing_or_stale_candidate",
      observed_phase: record.phase,
      observed_epoch: record.fence_epoch,
      effect_status: effectObservation.status,
      recorded_effects: effectObservation.recorded_effects,
      browser_effects_permitted: false
    });
  }

  return Object.freeze({
    readPrivateParentOwnershipSnapshot, parentId,
    validateIndex, validateParent, validateHistory,
    MAX_PARENTS, MAX_EPOCHS
  });
});
