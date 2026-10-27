/* Thin GitHub-first child bootstrap injector. No transcript or assistant DOM read. */
(() => {
  "use strict";
  if (globalThis.__localAgentGithubFirstSpawnV1) return;
  globalThis.__localAgentGithubFirstSpawnV1 = true;
  const transactionRe = /^spawn-[0-9a-f]{64}$/;
  const digestRe = /^sha256:[0-9a-f]{64}$/;
  const consumed = new Set();
  const encoder = new TextEncoder();
  function routeIsFresh() {
    try {
      const url = new URL(location.href);
      return url.protocol === "https:" &&
        ["chatgpt.com", "chat.openai.com"].includes(url.hostname) &&
        url.pathname.replace(/\/$/, "") === "" && !url.search && !url.hash;
    } catch (_error) { return false; }
  }
  const editor = globalThis.LocalAgentGithubFirstComposerV1;
  if (!editor) return; // Loader must install the shared helper first.
  function diagnosticPhase(transaction) {
    if (!transactionRe.test(transaction)) return "invalid_transaction";
    return sessionStorage.getItem("local-agent-github-first-submission:" + transaction) || "not_seen";
  }
  async function submit(message) {
    const transaction = String(message.transactionId || "");
    const bootstrapText = message.bootstrapText;
    if (message.protocolVersion !== 1 || !transactionRe.test(transaction) ||
        !digestRe.test(String(message.childRequestDigest || "")) ||
        !digestRe.test(String(message.bootstrapDigest || "")) ||
        typeof bootstrapText !== "string" || !bootstrapText.trim() ||
        bootstrapText.length > 32768) {
      return { ok: false, reason: "spawn_intent_invalid", protocolVersion: 1 };
    }
    const hex = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(bootstrapText))),
      byte => byte.toString(16).padStart(2, "0")).join("");
    if (message.bootstrapDigest !== "sha256:" + hex) {
      return { ok: false, reason: "spawn_digest_mismatch", protocolVersion: 1 };
    }
    const stateKey = "local-agent-github-first-submission:" + transaction;
    if (consumed.has(transaction) || sessionStorage.getItem(stateKey)) {
      return { ok: false, reason: "submission_unknown", protocolVersion: 1 };
    }
    if (!routeIsFresh()) return { ok: false, reason: "spawn_not_fresh", protocolVersion: 1 };
    const result = await editor.submitOnce({
      text: bootstrapText,
      routeAllowed: routeIsFresh,
      beforeMutation: () => {
        sessionStorage.setItem(stateKey, "submission_unknown");
        consumed.add(transaction);
      },
      phase: phase => sessionStorage.setItem(stateKey, phase),
      beforeClick: () => sessionStorage.setItem(stateKey, "send_click_attempted")
    });
    return { ...result, protocolVersion: 1 };
  }
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "bridge:spawn-capabilities") {
      sendResponse({ ok: true, protocolVersion: 1, route: routeIsFresh() ? "fresh" : "unexpected" });
      return false;
    }
    if (message?.type === "bridge:spawn-diagnostic") {
      // Only technical submission phase and route. Never return DOM text,
      // stored prompt, private GitHub data or assistant content.
      const transaction = String(message.transactionId || "");
      sendResponse({ ok: transactionRe.test(transaction), protocolVersion: 1,
        reason: diagnosticPhase(transaction), route: routeIsFresh() ? "fresh" : "other",
        sendReady: Boolean(editor.readyButton(editor.findComposer())) });
      return false;
    }
    if (message?.type === "bridge:spawn-bootstrap") {
      submit(message).then(sendResponse).catch(() => {
        sendResponse({ ok: false, reason: "submission_unknown", protocolVersion: 1 });
      });
      return true;
    }
    return false;
  });
})();
