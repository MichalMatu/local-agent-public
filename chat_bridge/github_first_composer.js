/* Restored historical ChatGPT editor/Send implementation.
 * Source: f233c8d70cf628d6974d24c78ef08cf5747836ae
 * chat_bridge/spawn_content.js and chat_bridge/content.js.
 * The donor functions below are copied verbatim from Git history.
 * The short GitHub-first adapter below handles dispatch identity and result
 * signalling; it does not replace the original composer or Send behavior.
 */
(() => {
  "use strict";
  if (globalThis.LocalAgentGithubFirstComposerV1) return;

  const COMPOSER_BLOCK_TAGS = new Set([
    "ADDRESS",
    "ARTICLE",
    "ASIDE",
    "BLOCKQUOTE",
    "DIV",
    "FIGCAPTION",
    "FIGURE",
    "FOOTER",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "HEADER",
    "LI",
    "MAIN",
    "NAV",
    "P",
    "PRE",
    "SECTION"
  ]);

  function findComposer() {
    return (
      document.querySelector("#prompt-textarea") ||
      document.querySelector('form [contenteditable="true"][data-lexical-editor="true"]') ||
      document.querySelector('form [contenteditable="true"]') ||
      document.querySelector("form textarea")
    );
  }

  function normalizeContentEditableText(text) {
    return String(text || "").replace(/\u00a0/g, " ");
  }

  function descendantText(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      return normalizeContentEditableText(node.nodeValue || "");
    }
    if (node instanceof HTMLBRElement) return "\n";
    if (!(node instanceof HTMLElement)) return "";
    return Array.from(node.childNodes, (child) => descendantText(child)).join("");
  }

  function blockLineText(block) {
    if (
      block.childNodes.length === 1 &&
      block.firstChild instanceof HTMLBRElement
    ) {
      return "";
    }
    return Array.from(block.childNodes, (child) => descendantText(child)).join("");
  }

  function blockStructuredComposerText(composer) {
    if (!(composer instanceof HTMLElement)) return null;
    const children = Array.from(composer.childNodes).filter((node) => !(
      node.nodeType === Node.TEXT_NODE && (node.nodeValue || "") === ""
    ));
    if (!children.length) return "";
    if (!children.some(node => node instanceof HTMLElement && COMPOSER_BLOCK_TAGS.has(node.tagName))) return null;
    // Chromium may keep the first line as a text node and render later lines as blocks.
    // Read that exact logical structure rather than innerText's doubled blank lines.
    const lines = [];
    let inline = "";
    for (const node of children) {
      if (node instanceof HTMLElement && COMPOSER_BLOCK_TAGS.has(node.tagName)) {
        if (inline) { lines.push(inline); inline = ""; }
        lines.push(blockLineText(node));
      } else {
        inline += descendantText(node);
      }
    }
    if (inline) lines.push(inline);
    return lines.join("\n");
  }

  function composerTextVariants(composer) {
    if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
      return [composer.value || ""];
    }
    if (!(composer instanceof HTMLElement)) return [""];
    const variants = [];
    const addVariant = (text) => {
      if (typeof text !== "string") return;
      variants.push(text);
      variants.push(normalizeContentEditableText(text));
    };
    addVariant(composer.innerText);
    addVariant(composer.textContent);
    const structured = blockStructuredComposerText(composer);
    if (typeof structured === "string") variants.push(structured);
    return Array.from(new Set(variants));
  }

  function composerText(composer) {
    return composerTextVariants(composer)[0] || "";
  }

  function composerMatchesText(composer, expected) {
    return composerTextVariants(composer).some((variant) => variant === expected);
  }

  function assistantIsGenerating() {
    const buttons = document.querySelectorAll(
      'button[data-testid="stop-button"], button[data-testid="composer-stop-button"]'
    );
    return Array.from(buttons).some((button) => button.checkVisibility({
      checkVisibilityCSS: true,
      checkOpacity: true
    }));
  }

  function selectContent(element) {
    const selection = window.getSelection();
    if (!selection) return;
    const range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function dispatchComposerBeforeInput(composer, text) {
    const inputType = text ? "insertText" : "deleteContentBackward";
    try {
      composer.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true,
        composed: true,
        inputType,
        data: text || null
      }));
    } catch (_error) {
      composer.dispatchEvent(new Event("beforeinput", { bubbles: true, composed: true }));
    }
  }

  function dispatchComposerInput(composer, text) {
    const inputType = text ? "insertText" : "deleteContentBackward";
    try {
      composer.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        inputType,
        data: text || null
      }));
    } catch (_error) {
      composer.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    }
  }

  function setComposerText(composer, text) {
    composer.focus();
    if (composer instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      if (!setter) throw new Error("textarea value setter unavailable");
      setter.call(composer, text);
      composer.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      return;
    }
    if (!(composer instanceof HTMLElement) || composer.contentEditable !== "true") {
      throw new Error("unsupported composer element");
    }
    selectContent(composer);
    dispatchComposerBeforeInput(composer, text);

    let inserted = false;
    try {
      inserted = document.execCommand("insertText", false, text);
    } catch (_error) {
      inserted = false;
    }
    if (!inserted) composer.textContent = text;

    // Always emit one explicit, fully-described input event after the DOM mutation.
    // Current ChatGPT/Lexical builds can emit a native execCommand input event while
    // still leaving application editor state stale; the explicit event is the state
    // synchronization boundary used by the Bridge.
    dispatchComposerInput(composer, text);
  }

  function clearComposer(composer, insertedText) {
    if (!composer.isConnected || composerText(composer) !== insertedText) return;
    try {
      setComposerText(composer, "");
    } catch (_error) {
      // Remove only our exact unchanged insertion; preserve every operator edit.
    }
  }

  function findSendButton(composer) {
    const selectors = [
      "#composer-submit-button",
      'button[data-testid="send-button"]',
      'button[data-testid="composer-submit-button"]',
      'button[aria-label="Send prompt"]',
      'button[aria-label="Send message"]',
      'button[aria-label="Send"]'
    ];
    const form = composer.closest("form");
    const scopes = form ? [form, document] : [document];
    for (const scope of scopes) {
      for (const selector of selectors) {
        const button = scope.querySelector(selector);
        if (button instanceof HTMLButtonElement && !button.disabled) return button;
      }
    }
    return null;
  }

  function submitComposer(composer, sendButton) {
    // Follow the same DOM path as a real operator click. ChatGPT's React handlers may
    // attach behavior to the live button that is not reproduced by requestSubmit().
    if (sendButton instanceof HTMLButtonElement && sendButton.isConnected && !sendButton.disabled) {
      sendButton.click();
      return;
    }
    const form = composer.closest("form");
    if (form instanceof HTMLFormElement && typeof form.requestSubmit === "function") {
      form.requestSubmit();
      return;
    }
    throw new Error("send control unavailable");
  }

  async function waitForSendButton(composer, timeoutMs = 4500) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (assistantIsGenerating()) return null;
      const button = findSendButton(composer);
      if (button) return button;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return null;
  }

  // Adapter for the GitHub-first protocol. Historical editor methods and
  // submitComposer() above are the original, not reimplementations.
  function readyButton(node) {
    return node ? findSendButton(node) : null;
  }

  async function submitOnce({ text, routeAllowed, beforeMutation, beforeClick, phase }) {
    if (!routeAllowed()) return { ok: false, reason: "route_changed" };
    let composer = findComposer();
    if (!composer) return { ok: false, reason: "composer_unavailable" };

    // Historical content.js allowed reuse of a byte-identical owned draft.
    // Never overwrite a different draft created by the operator.
    const alreadyWritten = composerMatchesText(composer, text);
    if (composerText(composer).trim() && !alreadyWritten) {
      return { ok: false, reason: "composer_not_empty" };
    }

    beforeMutation();
    if (!alreadyWritten) {
      try { setComposerText(composer, text); }
      catch (_error) {
        phase("composer_write_failed");
        return { ok: false, reason: "composer_write_failed" };
      }
    }
    const writtenComposer = findComposer() || composer;
    if (!composerMatchesText(writtenComposer, text)) {
      phase("composer_write_failed");
      return { ok: false, reason: "composer_write_failed" };
    }
    phase("composer_injected_no_click");

    // Original waitForSendButton, re-lookup after React reconciliation,
    // and original submitComposer (live button click, form fallback).
    let button = await waitForSendButton(writtenComposer, 15000);
    if (!button) {
      phase("send_not_attempted");
      return { ok: false, reason: "send_not_attempted" };
    }
    composer = findComposer();
    if (!routeAllowed() || !composer || !composerMatchesText(composer, text)) {
      phase("composer_changed_no_click");
      return { ok: false, reason: "composer_changed_no_click" };
    }
    if (assistantIsGenerating()) {
      phase("send_not_attempted");
      return { ok: false, reason: "send_not_attempted" };
    }
    button = findSendButton(composer) || await waitForSendButton(composer, 1200);
    if (!button) {
      phase("send_not_attempted");
      return { ok: false, reason: "send_not_attempted" };
    }

    beforeClick();
    try { submitComposer(composer, button); }
    catch (_error) {
      phase("send_click_unknown");
      return { ok: false, reason: "send_click_unknown" };
    }
    phase("send_click_attempted");
    // Only observe the composer and the route, never the user/assistant chat.
    // The worker independently binds the child URL to the original tab.
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const live = findComposer();
      if (live && !composerText(live).trim()) {
        phase("composer_cleared_after_click");
        return { ok: true, submitted: true, reason: "composer_cleared_after_click" };
      }
      if (!routeAllowed()) {
        phase("route_changed_after_click");
        return { ok: false, reason: "route_changed_after_click" };
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    phase("send_click_unknown");
    return { ok: false, reason: "send_click_unknown" };
  }

  globalThis.LocalAgentGithubFirstComposerV1 = Object.freeze({
    findComposer, readyButton, submitOnce
  });
})();
