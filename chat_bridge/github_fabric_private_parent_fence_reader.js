(function initPrivateParentFenceReader(root, factory) {
  const api = factory();
  root.LocalAgentPrivateParentFenceReader = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateParentFenceReader() {
  "use strict";

  // Unimported by the production extension. Read-only, explicitly enabled
  // observation of synthetic previews; this cannot authorize browser effects.
  const API = "https://api.github.com/repos/example-owner/local-agent-fabric-private";
  const BRANCH = "fabric-data";
  const REF = "refs/heads/" + BRANCH;
  const MAX_REF_BYTES = 4096;
  const MAX_COMMIT_BYTES = 8192;
  const MAX_TREE_BYTES = 512 * 1024;
  const MAX_TREE_ITEMS = 4096;
  const MAX_INDEX_BYTES = 4096;
  const MAX_RECORD_BYTES = 4096;
  const MAX_CONTENTS_RESPONSE = 8192;
  const MAX_PARENTS = 16;
  const SHA_RE = /^[0-9a-f]{40}$/;
  const TOKEN_RE = /^[A-Za-z0-9_-]{12,256}$/;
  const PARENT_RE = /^parent-[0-9a-f]{32}$/;
  const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
  const REQUEST_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
  const DISPATCH_RE = /^fabric-[0-9a-f]{32}$/;
  const PARENT_FIELDS = [
    "schema_version", "kind", "id", "project_id",
    "parent_conversation_url", "workflow_id", "operator_request_id",
    "operator_request_digest", "dispatch_id", "transport_mode",
    "fence_epoch", "phase", "browser_send_authorized", "ack_state"
  ].sort();

  function exactFields(value, keys) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join(",") === keys.join(",");
  }

  function canonicalParent(value) {
    if (typeof value !== "string" || !value || value !== value.trim()) return "";
    try {
      const url = new URL(value);
      const match = url.pathname.match(/^\/c\/([A-Za-z0-9_-]{1,200})\/?$/);
      if (url.protocol !== "https:" || url.hostname !== "chatgpt.com" ||
          url.port || url.username || url.password || url.search || url.hash ||
          !match) return "";
      return "https://chatgpt.com/c/" + match[1];
    } catch (_error) { return ""; }
  }

  async function parentId(parentUrl) {
    if (!parentUrl || canonicalParent(parentUrl) !== parentUrl) {
      throw new Error("Parent fence canonical conversation identity required");
    }
    if (!globalThis.crypto?.subtle) {
      throw new Error("Parent fence SHA-256 unavailable");
    }
    const payload = JSON.stringify(["fabric-global-parent-mode-v1", parentUrl]);
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(payload)
    );
    return "parent-" + Array.from(new Uint8Array(digest), byte =>
      byte.toString(16).padStart(2, "0")).join("").slice(0, 32);
  }

  function encodedLength(value) {
    return new TextEncoder().encode(JSON.stringify(value)).byteLength;
  }

  function validateIndex(value) {
    if (!exactFields(value, ["parent_ids", "schema_version"]) ||
        value.schema_version !== 1 || !Array.isArray(value.parent_ids) ||
        value.parent_ids.length > MAX_PARENTS ||
        value.parent_ids.some(id => typeof id !== "string" || !PARENT_RE.test(id)) ||
        value.parent_ids.join(",") !== [...new Set(value.parent_ids)].sort().join(",") ||
        encodedLength(value) > MAX_INDEX_BYTES) {
      throw new Error("Parent fence index invalid");
    }
    return value;
  }

  async function validateRecord(value) {
    if (!exactFields(value, PARENT_FIELDS) ||
        value.schema_version !== 1 ||
        value.kind !== "synthetic_parent_transport_arbitration_preview" ||
        typeof value.parent_conversation_url !== "string" ||
        value.id !== await parentId(value.parent_conversation_url) ||
        value.project_id !== "local-agent" ||
        typeof value.workflow_id !== "string" ||
        !REQUEST_RE.test(value.workflow_id) ||
        typeof value.operator_request_id !== "string" ||
        !REQUEST_RE.test(value.operator_request_id) ||
        typeof value.operator_request_digest !== "string" ||
        !DIGEST_RE.test(value.operator_request_digest) ||
        typeof value.dispatch_id !== "string" ||
        !DISPATCH_RE.test(value.dispatch_id) ||
        !["github_first", "legacy_dom"].includes(value.transport_mode) ||
        value.fence_epoch !== 1 ||
        value.phase !== "unattested_no_browser_authority" ||
        value.browser_send_authorized !== false ||
        value.ack_state !== "not_attested" ||
        encodedLength(value) > MAX_RECORD_BYTES) {
      throw new Error("Parent fence preview record invalid");
    }
    return value;
  }

  async function getJson(fetchImpl, token, path, limit) {
    const url = API + path; // Fixed host/repository; paths only from trusted templates.
    const abort = new AbortController();
    // Timer stays active while streaming the body, not merely until headers.
    const timeout = setTimeout(() => abort.abort(), 5000);
    try {
      let response;
      try {
        response = await fetchImpl(url, {
          method: "GET", redirect: "error", credentials: "omit", cache: "no-store",
          signal: abort.signal,
          headers: {
            Authorization: "Bearer " + token,
            Accept: "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
          }
        });
      } catch (_error) {
        throw new Error("Parent fence private GitHub network read failed");
      }
      if (response?.status !== 200 || response.redirected ||
          (response.url && response.url !== url) ||
          !response.body || typeof response.body.getReader !== "function") {
        throw new Error("Parent fence private GitHub source response rejected");
      }
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!(value instanceof Uint8Array)) throw new Error("Parent fence response bytes invalid");
          size += value.byteLength;
          if (size > limit) throw new Error("Parent fence response exceeds bound");
          chunks.push(value);
        }
      } catch (_error) {
        await reader.cancel().catch(() => undefined);
        throw new Error("Parent fence response incomplete or exceeds bound");
      } finally {
        try { reader.releaseLock(); } catch (_error) {}
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      try {
        const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
        if (!data || typeof data !== "object" || Array.isArray(data)) throw Error();
        return data;
      } catch (_error) {
        throw new Error("Parent fence source JSON invalid");
      }
    } finally {
      clearTimeout(timeout);
    }
  }

  async function decodeContents(metadata, path, limit, blobSha) {
    if (!metadata || metadata.type !== "file" || metadata.path !== path ||
        metadata.encoding !== "base64" || metadata.sha !== blobSha ||
        !Number.isSafeInteger(metadata.size) || metadata.size < 0 ||
        metadata.size > limit || !SHA_RE.test(String(blobSha || "")) ||
        typeof metadata.content !== "string") {
      throw new Error("Parent fence pinned Contents metadata invalid");
    }
    const encoded = metadata.content.replace(/[\r\n]/g, "");
    if (encoded.length % 4 !== 0 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      throw new Error("Parent fence Contents base64 invalid");
    }
    let bytes;
    try { bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0)); }
    catch (_error) { throw new Error("Parent fence Contents base64 invalid"); }
    if (bytes.length !== metadata.size) throw new Error("Parent fence Contents size mismatch");
    // A matching API metadata SHA is only a claim. Verify the actual Git
    // blob SHA-1 over the exact decoded bytes before parsing JSON.
    if (!globalThis.crypto?.subtle) throw new Error("Parent fence blob digest unavailable");
    const header = new TextEncoder().encode("blob " + bytes.length + "\0");
    const preimage = new Uint8Array(header.length + bytes.length);
    preimage.set(header);
    preimage.set(bytes, header.length);
    const digest = await globalThis.crypto.subtle.digest("SHA-1", preimage);
    const observedSha = Array.from(new Uint8Array(digest), byte =>
      byte.toString(16).padStart(2, "0")).join("");
    if (observedSha !== blobSha) {
      throw new Error("Parent fence Contents blob digest mismatch");
    }
    try {
      const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (!data || typeof data !== "object" || Array.isArray(data)) throw Error();
      return data;
    } catch (_error) {
      throw new Error("Parent fence Contents JSON invalid");
    }
  }

  async function readPrivateParentFenceSnapshot({
    enabled = false, readToken, parentConversationUrl, expectedTransportMode,
    fetchImpl = globalThis.fetch
  } = {}) {
    if (enabled !== true) throw new Error("Parent fence reader is default-disabled");
    if (typeof readToken !== "string" || !TOKEN_RE.test(readToken)) {
      throw new Error("Parent fence read authorization missing");
    }
    if (typeof fetchImpl !== "function") throw new Error("Parent fence fetch unavailable");
    if (!parentConversationUrl || canonicalParent(parentConversationUrl) !== parentConversationUrl) {
      throw new Error("Parent fence canonical conversation identity required");
    }
    if (!["github_first", "legacy_dom"].includes(expectedTransportMode)) {
      throw new Error("Parent fence expected transport mode invalid");
    }
    const id = await parentId(parentConversationUrl);
    const ref = await getJson(fetchImpl, readToken,
      "/git/ref/heads/" + BRANCH, MAX_REF_BYTES);
    const head = ref?.object?.sha;
    if (ref.ref !== REF || ref.object?.type !== "commit" ||
        typeof head !== "string" || !SHA_RE.test(head)) {
      throw new Error("Parent fence origin ref invalid");
    }
    const commit = await getJson(fetchImpl, readToken,
      "/git/commits/" + head, MAX_COMMIT_BYTES);
    const treeSha = commit?.tree?.sha;
    if (typeof treeSha !== "string" || !SHA_RE.test(treeSha)) {
      throw new Error("Parent fence origin commit invalid");
    }
    const listing = await getJson(fetchImpl, readToken,
      "/git/trees/" + treeSha + "?recursive=1", MAX_TREE_BYTES);
    if (listing.sha !== treeSha || listing.truncated !== false ||
        !Array.isArray(listing.tree) || listing.tree.length > MAX_TREE_ITEMS) {
      throw new Error("Parent fence origin tree incomplete");
    }
    const entries = new Map();
    let rootSeen = false;
    for (const entry of listing.tree) {
      if (!entry || typeof entry.path !== "string") {
        throw new Error("Parent fence origin tree entry invalid");
      }
      if (entry.path === "parents") {
        if (rootSeen || entry.type !== "tree" || entry.mode !== "040000" ||
            typeof entry.sha !== "string" || !SHA_RE.test(entry.sha)) {
          throw new Error("Parent fence root invalid or duplicated");
        }
        rootSeen = true;
        continue;
      }
      if (!entry.path.startsWith("parents/")) continue;
      if (entries.has(entry.path) ||
          (entry.path !== "parents/index.json" &&
           !/^parents\/parent-[0-9a-f]{32}\.json$/.test(entry.path)) ||
          entry.type !== "blob" || entry.mode !== "100644" ||
          typeof entry.sha !== "string" || !SHA_RE.test(entry.sha)) {
        throw new Error("Parent fence unexpected or duplicate tree entry");
      }
      entries.set(entry.path, entry.sha);
    }
    if (entries.size && !rootSeen) {
      throw new Error("Parent fence root missing from tree");
    }
    const indexPath = "parents/index.json";
    if (!entries.has(indexPath)) {
      if (entries.size !== 0) throw new Error("Parent fence orphan record");
      return Object.freeze({
        source: "authenticated_private_github_preview_only",
        source_head_sha: head, parent_id: id, status: "unregistered",
        browser_effects_permitted: false
      });
    }
    const indexMetadata = await getJson(fetchImpl, readToken,
      "/contents/" + indexPath + "?ref=" + head, MAX_CONTENTS_RESPONSE);
    const index = validateIndex(await decodeContents(
      indexMetadata, indexPath, MAX_INDEX_BYTES, entries.get(indexPath)
    ));
    const indexedPaths = new Set(index.parent_ids.map(pid => "parents/" + pid + ".json"));
    if (entries.size !== index.parent_ids.length + 1 ||
        [...entries.keys()].some(p => p !== indexPath && !indexedPaths.has(p))) {
      throw new Error("Parent fence orphan or dangling record");
    }
    let selected = null;
    for (const pid of index.parent_ids) {
      const recordPath = "parents/" + pid + ".json";
      if (!entries.has(recordPath)) throw new Error("Parent fence dangling record");
      const metadata = await getJson(fetchImpl, readToken,
        "/contents/" + recordPath + "?ref=" + head, MAX_CONTENTS_RESPONSE);
      const record = await validateRecord(await decodeContents(
        metadata, recordPath, MAX_RECORD_BYTES, entries.get(recordPath)
      ));
      if (record.id !== pid) throw new Error("Parent fence record/path identity conflict");
      if (pid === id) selected = record;
    }
    if (selected && selected.transport_mode !== expectedTransportMode) {
      throw new Error("Parent fence competing transport mode already occupied");
    }
    return Object.freeze({
      source: "authenticated_private_github_preview_only",
      source_head_sha: head, parent_id: id,
      status: selected ? "unattested_preview" : "unregistered",
      browser_effects_permitted: false,
      ...(selected ? { record: Object.freeze(selected) } : {})
    });
  }

  return Object.freeze({
    readPrivateParentFenceSnapshot, parentId, validateIndex, validateRecord,
    MAX_INDEX_BYTES, MAX_RECORD_BYTES, MAX_TREE_ITEMS
  });
});
