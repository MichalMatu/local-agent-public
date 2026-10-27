(function initPrivateFabricTransport(root, factory) {
  const dispatchModel = root.LocalAgentGithubFabricDispatchModel ||
    (typeof require === "function" ? require("./github_fabric_dispatch_model.js") : null);
  const intakeModel = root.LocalAgentGithubFabricIntakeModel ||
    (typeof require === "function" ? require("./github_fabric_intake_model.js") : null);
  const api = factory(dispatchModel, intakeModel);
  root.LocalAgentPrivateFabricTransport = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPrivateFabricTransport(
  dispatchModel, intakeModel
) {
  "use strict";

  // NOT imported by the production extension. The configured repository is
  // a private-data repo; only the read contract is implemented here.
  // No browser token store, worker integration or live child execution exists.
  const REPOSITORY = "example-owner/local-agent-fabric-private";
  const BRANCH = "fabric-data";
  const API = "https://api.github.com/repos/" + REPOSITORY;
  const REF = "refs/heads/" + BRANCH;
  const MAX_REF_BYTES = 4096;
  const MAX_INDEX_RESPONSE = 16384;
  const MAX_RECORD_RESPONSE = 192 * 1024;
  const MAX_RECORD_BYTES = 128 * 1024;
  const SHA_RE = /^[0-9a-f]{40}$/;
  const TOKEN_RE = /^[A-Za-z0-9_-]{12,256}$/;
  const PROJECT_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
  const WORKFLOW_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;

  // Never accept raw paths from a prompt or page. Every Contents path is
  // derived from bounded project/workflow/dispatch IDs.
  function permittedPath(path) {
    if (path === "projects/index.json") return true;
    return /^projects\/[a-z0-9][a-z0-9-]{0,63}\/workflows\/(?:index\.json|[A-Za-z0-9][A-Za-z0-9._-]{0,119}\/dispatches\/(?:index\.json|fabric-[0-9a-f]{32}\.json))$/.test(path);
  }

  function validateCatalog(value, field, pattern, maximum) {
    const fields = Object.keys(value || {}).sort();
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        fields.join(",") !== [field, "schema_version"].sort().join(",") ||
        value.schema_version !== 1 || !Array.isArray(value[field]) ||
        value[field].length > maximum ||
        value[field].some(id => typeof id !== "string" || !pattern.test(id)) ||
        value[field].join(",") !== [...new Set(value[field])].sort().join(",")) {
      throw new Error("Private GitHub Fabric project catalog invalid");
    }
    return value[field];
  }

  function requireAuthorizedToken(readToken) {
    if (typeof readToken !== "string" || !TOKEN_RE.test(readToken)) {
      throw new Error("Private GitHub Fabric read authorization missing");
    }
  }

  async function boundedJsonResponse(response, maximum, requestedUrl) {
    if (!response || response.status !== 200 || response.redirected ||
        (response.url && response.url !== requestedUrl) ||
        !response.body || typeof response.body.getReader !== "function") {
      throw new Error("Private GitHub Fabric source response rejected");
    }
    const reader = response.body.getReader();
    const parts = [];
    let total = 0;
    try {
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        if (!(next.value instanceof Uint8Array)) {
          throw new Error("Private GitHub Fabric source bytes are invalid");
        }
        total += next.value.byteLength;
        if (total > maximum) {
          throw new Error("Private GitHub Fabric response exceeds bound");
        }
        parts.push(next.value);
      }
    } catch (error) {
      await reader.cancel().catch(() => undefined);
      throw error;
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      bytes.set(part, offset);
      offset += part.byteLength;
    }
    let value;
    try {
      value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch (_error) {
      throw new Error("Private GitHub Fabric response JSON invalid");
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("Private GitHub Fabric source payload must be an object");
    }
    return value;
  }

  async function requestJson({ readToken, fetchImpl, path, maximum }) {
    const url = API + path;
    let result;
    try {
      result = await fetchImpl(url, {
        method: "GET",
        headers: Object.freeze({
          "Authorization": "Bearer " + readToken,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28"
        }),
        redirect: "error",
        cache: "no-store",
        credentials: "omit"
      });
    } catch (_error) {
      // Never propagate fetch errors that may echo request headers or a token.
      throw new Error("Private GitHub Fabric network read failed");
    }
    return boundedJsonResponse(result, maximum, url);
  }

  function decodeContents(contents, expectedPath, maximum) {
    if (!contents || contents.type !== "file" ||
        contents.encoding !== "base64" ||
        contents.path !== expectedPath ||
        !Number.isSafeInteger(contents.size) || contents.size < 0 ||
        contents.size > maximum || typeof contents.content !== "string") {
      throw new Error("Private GitHub Fabric contents metadata rejected");
    }
    const encoded = contents.content.replace(/[\r\n]/g, "");
    if (encoded.length % 4 !== 0 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      throw new Error("Private GitHub Fabric contents base64 malformed");
    }
    let binary;
    try {
      binary = atob(encoded);
    } catch (_error) {
      throw new Error("Private GitHub Fabric contents base64 rejected");
    }
    if (binary.length !== contents.size || binary.length > maximum) {
      throw new Error("Private GitHub Fabric contents size conflict");
    }
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    let payload;
    try {
      payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch (_error) {
      throw new Error("Private GitHub Fabric contents JSON invalid");
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Private GitHub Fabric record is not an object");
    }
    return payload;
  }

  async function pinnedContents({ readToken, fetchImpl, head, path, maximum }) {
    if (!SHA_RE.test(head) || !permittedPath(path)) {
      throw new Error("Private GitHub Fabric pinned path is invalid");
    }
    const metadata = await requestJson({
      readToken, fetchImpl,
      path: "/contents/" + path + "?ref=" + head,
      maximum: maximum === MAX_RECORD_BYTES ? MAX_RECORD_RESPONSE : MAX_INDEX_RESPONSE
    });
    return decodeContents(metadata, path, maximum);
  }

  async function digestText(text) {
    if (!globalThis.crypto?.subtle) {
      throw new Error("Private GitHub Fabric SHA-256 unavailable");
    }
    const bytes = new TextEncoder().encode(text);
    const hash = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
    return Array.from(hash, b => b.toString(16).padStart(2, "0")).join("");
  }

  async function validateSourceIntegrity(dispatch) {
    const id = await digestText(JSON.stringify([
      "github-fabric-dispatch-v1", dispatch.request_digest
    ]));
    if (dispatch.id !== "fabric-" + id.slice(0, 32)) {
      throw new Error("Private GitHub Fabric dispatch ID integrity conflict");
    }
    const campaign = await digestText(JSON.stringify([
      "github-fabric-campaign-v1", dispatch.request_id,
      dispatch.request_digest, dispatch.parent_conversation_url
    ]));
    if (dispatch.campaign_id !== "cf-" + campaign.slice(0, 16)) {
      throw new Error("Private GitHub Fabric campaign ID integrity conflict");
    }
    for (const child of dispatch.children) {
      const expected = "sha256:" + await digestText(child.spawn.bootstrap_text);
      if (child.spawn.bootstrap_digest !== expected) {
        throw new Error("Private GitHub Fabric bootstrap digest conflict");
      }
      const transaction = "spawn-" + await digestText(JSON.stringify([
        child.spawn.child_request_digest, 1
      ]));
      if (child.spawn.transaction_id !== transaction) {
        throw new Error("Private GitHub Fabric spawn transaction conflict");
      }
    }
  }

  async function readPrivateDispatchSnapshot({
    enabled = false, readToken, fetchImpl = globalThis.fetch, expectedId, projectId, workflowId
  } = {}) {
    if (enabled !== true) {
      throw new Error("Private GitHub Fabric reader is default-disabled");
    }
    requireAuthorizedToken(readToken);
    if (typeof fetchImpl !== "function") {
      throw new Error("Private GitHub Fabric fetch is unavailable");
    }
    if (typeof expectedId !== "string" ||
        !/^fabric-[0-9a-f]{32}$/.test(expectedId)) {
      throw new Error("Private GitHub Fabric expected dispatch identity invalid");
    }
    if (typeof projectId !== "string" || !PROJECT_RE.test(projectId) ||
        typeof workflowId !== "string" || !WORKFLOW_RE.test(workflowId)) {
      throw new Error("Private GitHub Fabric project/workflow identity invalid");
    }
    if (!dispatchModel || !intakeModel) {
      throw new Error("Private GitHub Fabric validators are not available");
    }
    const ref = await requestJson({
      readToken, fetchImpl, path: "/git/ref/heads/" + BRANCH, maximum: MAX_REF_BYTES
    });
    const head = ref?.object?.sha;
    if (ref.ref !== REF || ref.object?.type !== "commit" ||
        typeof head !== "string" || !SHA_RE.test(head)) {
      throw new Error("Private GitHub Fabric origin ref rejected");
    }
    const catalog = await pinnedContents({
      readToken, fetchImpl, head,
      path: "projects/index.json", maximum: intakeModel.MAX_INDEX_BYTES
    });
    if (!validateCatalog(catalog, "project_ids", PROJECT_RE, 32).includes(projectId)) {
      throw new Error("Private GitHub Fabric project not indexed");
    }
    const scope = "projects/" + projectId + "/workflows/";
    const workflows = await pinnedContents({
      readToken, fetchImpl, head,
      path: scope + "index.json", maximum: intakeModel.MAX_INDEX_BYTES
    });
    if (!validateCatalog(workflows, "workflow_ids", WORKFLOW_RE, 128).includes(workflowId)) {
      throw new Error("Private GitHub Fabric workflow not indexed");
    }
    const dispatchRoot = scope + workflowId + "/dispatches/";
    const index = await pinnedContents({
      readToken, fetchImpl, head,
      path: dispatchRoot + "index.json", maximum: intakeModel.MAX_INDEX_BYTES
    });
    intakeModel.validateIndex(index);
    if (!index.dispatch_ids.includes(expectedId)) {
      throw new Error("Private GitHub Fabric dispatch not indexed");
    }
    const dispatch = await pinnedContents({
      readToken, fetchImpl, head,
      path: dispatchRoot + expectedId + ".json",
      maximum: MAX_RECORD_BYTES
    });
    dispatchModel.validateDispatch(dispatch);
    if (dispatch.id !== expectedId) {
      throw new Error("Private GitHub Fabric record identity conflict");
    }
    await validateSourceIntegrity(dispatch);
    return Object.freeze({
      source: "authenticated_private_github",
      source_head_sha: head,
      project_id: projectId,
      workflow_id: workflowId,
      dispatch_id: expectedId,
      dispatch
    });
  }

  return Object.freeze({
    REPOSITORY, BRANCH, readPrivateDispatchSnapshot
  });
});
