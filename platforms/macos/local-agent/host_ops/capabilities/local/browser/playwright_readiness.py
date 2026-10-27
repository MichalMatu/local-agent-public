"""Internal Playwright helper for DOM and extension content-script readiness evidence."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import suppress
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import BrowserExtensionScriptEvidence, BrowserReadinessInspection
from .playwright_selector_counts import _count_selector, _document_root
from .playwright_snapshot import (
    _bounded_text,
    _load_playwright_sync_api,
    _operation_timeout_ms,
    _target_info,
)
from .readiness import (
    _SCRIPT_FINGERPRINTS_ENV,
    BrowserReadinessError,
    ScriptFingerprint,
    _normalize_script_fingerprints,
)
from .selector_counts import _SELECTORS_ENV, _normalize_selectors
from .snapshot import _SNAPSHOT_TARGET_ENV

_MAX_SCRIPT_EVENTS = 10_000
_MAX_TARGETS = 4096
_DEBUGGER_DRAIN_MS = 250


def _extension_identity(raw_url: object) -> tuple[str, str] | None:
    if not isinstance(raw_url, str) or not raw_url:
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme != "chrome-extension" or not parsed.netloc or parsed.query or parsed.fragment:
        return None
    name = PurePosixPath(parsed.path).name
    if not name:
        return None
    return parsed.netloc, name


def _script_inventory(
    events: list[object], fingerprints: tuple[ScriptFingerprint, ...]
) -> tuple[dict[str, dict[str, set[str]]], bool]:
    expected_names = {name for name, _digest in fingerprints}
    inventory: dict[str, dict[str, set[str]]] = {}
    invalid_expected_hash = False
    for event in events:
        if not isinstance(event, dict):
            continue
        identity = _extension_identity(event.get("url"))
        if identity is None:
            continue
        origin, name = identity
        if name not in expected_names:
            continue
        digest = event.get("hash")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            invalid_expected_hash = True
            continue
        inventory.setdefault(origin, {}).setdefault(name, set()).add(digest.lower())
    return inventory, invalid_expected_hash


def _classify_content_scripts(
    inventory: dict[str, dict[str, set[str]]],
    fingerprints: tuple[ScriptFingerprint, ...],
) -> tuple[tuple[BrowserExtensionScriptEvidence, ...], str, str | None]:
    expected = dict(fingerprints)
    candidates = {
        origin: scripts
        for origin, scripts in inventory.items()
        if any(name in scripts for name in expected)
    }
    ready_origins = [
        origin
        for origin, scripts in candidates.items()
        if all(expected[name] in scripts.get(name, set()) for name in expected)
    ]
    if len(ready_origins) > 1:
        return _aggregate_script_evidence(candidates, fingerprints), "ambiguous", None
    if len(ready_origins) == 1:
        origin = ready_origins[0]
        evidence = tuple(
            BrowserExtensionScriptEvidence(name=name, status="matched")
            for name, _digest in fingerprints
        )
        return evidence, "ready", origin
    if len(candidates) > 1:
        return _aggregate_script_evidence(candidates, fingerprints), "ambiguous", None
    if not candidates:
        evidence = tuple(
            BrowserExtensionScriptEvidence(name=name, status="missing")
            for name, _digest in fingerprints
        )
        return evidence, "missing", None

    origin, scripts = next(iter(candidates.items()))
    evidence_list: list[BrowserExtensionScriptEvidence] = []
    has_stale = False
    for name, digest in fingerprints:
        observed = scripts.get(name, set())
        if digest in observed:
            status = "matched"
        elif observed:
            status = "stale"
            has_stale = True
        else:
            status = "missing"
        evidence_list.append(BrowserExtensionScriptEvidence(name=name, status=status))
    return tuple(evidence_list), "stale" if has_stale else "missing", origin


def _aggregate_script_evidence(
    candidates: dict[str, dict[str, set[str]]], fingerprints: tuple[ScriptFingerprint, ...]
) -> tuple[BrowserExtensionScriptEvidence, ...]:
    evidence: list[BrowserExtensionScriptEvidence] = []
    for name, digest in fingerprints:
        observed = [hashes for scripts in candidates.values() if (hashes := scripts.get(name))]
        if any(digest in hashes for hashes in observed):
            status = "matched"
        elif observed:
            status = "stale"
        else:
            status = "missing"
        evidence.append(BrowserExtensionScriptEvidence(name=name, status=status))
    return tuple(evidence)


def _worker_state(targets_response: object, extension_origin: str | None) -> str:
    if extension_origin is None:
        return "unknown"
    if not isinstance(targets_response, dict) or not isinstance(
        targets_response.get("targetInfos"), list
    ):
        raise BrowserReadinessError("CDP Target.getTargets returned invalid data")
    targets = targets_response["targetInfos"]
    if len(targets) > _MAX_TARGETS:
        raise BrowserReadinessError("CDP target inventory exceeded its bound")
    for item in targets:
        if not isinstance(item, dict) or item.get("type") != "service_worker":
            continue
        identity = _extension_identity(item.get("url"))
        if identity is not None and identity[0] == extension_origin:
            return "running"
    return "inactive"


def _diagnosis(dom_ready: bool, content_state: str, worker_state: str) -> str:
    if not dom_ready:
        return "dom_not_ready"
    if content_state == "missing":
        return "content_script_missing"
    if content_state == "stale":
        return "content_script_stale"
    if content_state == "ambiguous":
        return "extension_ambiguous"
    if worker_state != "running":
        return "worker_inactive"
    return "ready"


def _run_readiness(
    endpoint: str,
    target_id: str,
    selectors: tuple[str, ...],
    fingerprints: tuple[ScriptFingerprint, ...],
    timeout_seconds: float,
) -> BrowserReadinessInspection:
    sync_api = _load_playwright_sync_api()
    timeout_ms = _operation_timeout_ms(timeout_seconds)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint, timeout=timeout_ms, is_local=True, no_defaults=True
        )
        matched_session = None
        matched_page = None
        debugger_enabled = False
        try:
            for context in browser.contexts:
                for page in context.pages:
                    session = context.new_cdp_session(page)
                    keep = False
                    try:
                        info = _target_info(session.send("Target.getTargetInfo"))
                        if info["targetId"] == target_id:
                            matched_session = session
                            matched_page = page
                            keep = True
                            break
                    finally:
                        if not keep:
                            session.detach()
                if matched_session is not None:
                    break
            if matched_session is None or matched_page is None:
                raise BrowserReadinessError(
                    "requested browser target is not an attached page target"
                )

            before = _target_info(matched_session.send("Target.getTargetInfo"))
            if before["targetId"] != target_id or before["type"] != "page":
                raise BrowserReadinessError("requested browser target is not a page target")
            before_url = _sanitize_target_url(str(before.get("url") or ""))
            if not before_url:
                raise BrowserReadinessError("CDP page target has an invalid URL")

            root_id = _document_root(
                matched_session.send("DOM.getDocument", {"depth": 0, "pierce": False})
            )
            counts = tuple(
                _count_selector(matched_session, root_id, selector, index)
                for index, selector in enumerate(selectors)
            )
            dom_ready = all(item.match_count > 0 for item in counts)

            script_events: list[object] = []
            overflow = False

            def on_script(event: object) -> None:
                nonlocal overflow
                if len(script_events) >= _MAX_SCRIPT_EVENTS:
                    overflow = True
                    return
                script_events.append(event)

            matched_session.on("Debugger.scriptParsed", on_script)
            matched_session.send("Debugger.enable")
            debugger_enabled = True
            matched_page.wait_for_timeout(min(_DEBUGGER_DRAIN_MS, max(1, timeout_ms / 8)))
            matched_session.send("Debugger.disable")
            debugger_enabled = False
            if overflow:
                raise BrowserReadinessError("CDP script inventory exceeded its bound")

            inventory, invalid_hash = _script_inventory(script_events, fingerprints)
            if invalid_hash:
                raise BrowserReadinessError("CDP extension script hash evidence is invalid")
            script_evidence, content_state, extension_origin = _classify_content_scripts(
                inventory, fingerprints
            )
            worker_state = _worker_state(
                matched_session.send("Target.getTargets"), extension_origin
            )

            after = _target_info(matched_session.send("Target.getTargetInfo"))
            after_url = _sanitize_target_url(str(after.get("url") or ""))
            if after["targetId"] != target_id or after["type"] != "page":
                raise BrowserReadinessError("page target changed during readiness inspection")
            if not after_url or after_url != before_url:
                raise BrowserReadinessError("page URL changed during readiness inspection")
            diagnosis = _diagnosis(dom_ready, content_state, worker_state)
            return BrowserReadinessInspection(
                endpoint=endpoint,
                target_id=target_id,
                target_type="page",
                title=_bounded_text(after.get("title"), 512),
                url=after_url,
                selectors=counts,
                extension_scripts=script_evidence,
                dom_ready=dom_ready,
                content_script_state=content_state,
                worker_state=worker_state,
                diagnosis=diagnosis,
            )
        finally:
            try:
                if debugger_enabled and matched_session is not None:
                    with suppress(Exception):
                        matched_session.send("Debugger.disable")
                if matched_session is not None:
                    matched_session.detach()
            finally:
                browser.close(reason="host-ops read-only readiness inspection complete")


def _load_fingerprints(raw: str) -> tuple[ScriptFingerprint, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserReadinessError("browser script fingerprint list is invalid JSON") from exc
    if not isinstance(value, list):
        raise BrowserReadinessError("browser script fingerprint list must be a JSON array")
    encoded: list[str] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise BrowserReadinessError("browser script fingerprint entry is invalid")
        encoded.append(f"{item[0]}={item[1]}")
    return _normalize_script_fingerprints(encoded)


def _load_selectors(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserReadinessError("browser selector list is invalid JSON") from exc
    if not isinstance(value, list):
        raise BrowserReadinessError("browser selector list must be a JSON array")
    return _normalize_selectors(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    target_id = os.environ.get(_SNAPSHOT_TARGET_ENV, "").strip()
    selectors_raw = os.environ.get(_SELECTORS_ENV, "")
    fingerprints_raw = os.environ.get(_SCRIPT_FINGERPRINTS_ENV, "")
    if not endpoint or not target_id or not selectors_raw or not fingerprints_raw:
        print(json.dumps({"error": "browser readiness input is missing"}, sort_keys=True))
        return 2
    if not 1 <= args.timeout <= 120:
        print(json.dumps({"error": "browser readiness timeout is invalid"}, sort_keys=True))
        return 2
    try:
        selectors = _load_selectors(selectors_raw)
        fingerprints = _load_fingerprints(fingerprints_raw)
        result = _run_readiness(endpoint, target_id, selectors, fingerprints, args.timeout)
    except (BrowserReadinessError, BrowserAttachError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"error": "browser readiness inspection failed"}, sort_keys=True))
        return 1
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
