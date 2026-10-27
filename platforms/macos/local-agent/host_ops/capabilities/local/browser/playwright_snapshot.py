"""Internal Playwright helper for one read-only Chromium page snapshot."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from typing import Any, cast

from .attach import _ATTACH_ENDPOINT_ENV, BrowserAttachError
from .inspection import _sanitize_target_url
from .models import BrowserPageSnapshot
from .snapshot import (
    _MAX_FRAMES,
    _MAX_MIME_TYPE_CHARS,
    _MAX_TARGET_ID_CHARS,
    _MAX_TARGET_TYPE_CHARS,
    _MAX_TITLE_CHARS,
    _SNAPSHOT_TARGET_ENV,
    BrowserSnapshotError,
)


def _load_playwright_sync_api() -> Any:
    try:
        return importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as exc:
        if exc.name == "playwright" or str(exc.name).startswith("playwright."):
            raise BrowserSnapshotError(
                "Playwright is not installed; install the optional 'host-ops[browser]' extra"
            ) from exc
        raise


def _operation_timeout_ms(timeout_seconds: float) -> float:
    cleanup_reserve = min(1.0, timeout_seconds / 4.0)
    return max(250.0, (timeout_seconds - cleanup_reserve) * 1000.0)


def _run_snapshot(endpoint: str, target_id: str, timeout_seconds: float) -> BrowserPageSnapshot:
    sync_api = _load_playwright_sync_api()
    timeout_ms = _operation_timeout_ms(timeout_seconds)

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint, timeout=timeout_ms, is_local=True, no_defaults=True
        )
        matched_session = None
        try:
            for context in browser.contexts:
                for page in context.pages:
                    session = context.new_cdp_session(page)
                    keep = False
                    try:
                        info = _target_info(session.send("Target.getTargetInfo"))
                        if info["targetId"] == target_id:
                            matched_session = session
                            keep = True
                            break
                    finally:
                        if not keep:
                            session.detach()
                if matched_session is not None:
                    break

            if matched_session is None:
                raise BrowserSnapshotError(
                    "requested browser target is not an attached page target"
                )

            target_info = _target_info(matched_session.send("Target.getTargetInfo"))
            if target_info["targetId"] != target_id or target_info["type"] != "page":
                raise BrowserSnapshotError("requested browser target is not a page target")

            frame_tree = matched_session.send("Page.getFrameTree")
            document = matched_session.send("DOM.getDocument", {"depth": 0, "pierce": False})
            return _snapshot_from_protocol(endpoint, target_info, frame_tree, document)
        finally:
            try:
                if matched_session is not None:
                    matched_session.detach()
            finally:
                browser.close(reason="host-ops read-only page snapshot complete")


def _target_info(response: object) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("targetInfo"), dict):
        raise BrowserSnapshotError("CDP Target.getTargetInfo returned invalid target info")
    info = cast(dict[str, Any], response["targetInfo"])
    target_id = info.get("targetId")
    target_type = info.get("type")
    if not isinstance(target_id, str) or not 1 <= len(target_id) <= _MAX_TARGET_ID_CHARS:
        raise BrowserSnapshotError("CDP target has an invalid id")
    if not isinstance(target_type, str) or not 1 <= len(target_type) <= _MAX_TARGET_TYPE_CHARS:
        raise BrowserSnapshotError("CDP target has an invalid type")
    return info


def _count_frames(frame_tree: object) -> tuple[int, dict[str, Any]]:
    if not isinstance(frame_tree, dict) or not isinstance(frame_tree.get("frameTree"), dict):
        raise BrowserSnapshotError("CDP Page.getFrameTree returned invalid frame data")
    root = frame_tree["frameTree"]
    stack = [root]
    count = 0
    while stack:
        item = stack.pop()
        if not isinstance(item, dict) or not isinstance(item.get("frame"), dict):
            raise BrowserSnapshotError("CDP frame tree contains an invalid frame")
        count += 1
        if count > _MAX_FRAMES:
            raise BrowserSnapshotError("CDP frame tree exceeded its bound")
        children = item.get("childFrames", [])
        if not isinstance(children, list):
            raise BrowserSnapshotError("CDP frame tree contains invalid children")
        stack.extend(children)
    return count, root["frame"]


def _document_root(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("root"), dict):
        raise BrowserSnapshotError("CDP DOM.getDocument returned invalid root data")
    return cast(dict[str, Any], document["root"])


def _bounded_text(value: object, maximum: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text[:maximum]


def _snapshot_from_protocol(
    endpoint: str,
    target_info: dict[str, Any],
    frame_tree: object,
    document: object,
) -> BrowserPageSnapshot:
    frame_count, main_frame = _count_frames(frame_tree)
    root = _document_root(document)

    main_frame_url = _sanitize_target_url(str(main_frame.get("url") or ""))
    if not main_frame_url:
        raise BrowserSnapshotError("CDP main frame has an invalid URL")
    document_node_name = root.get("nodeName")
    document_child_count = root.get("childNodeCount", 0)
    if not isinstance(document_node_name, str) or not document_node_name:
        raise BrowserSnapshotError("CDP document root has an invalid node name")
    if (
        not isinstance(document_child_count, int)
        or isinstance(document_child_count, bool)
        or document_child_count < 0
        or document_child_count > 100000
    ):
        raise BrowserSnapshotError("CDP document root has an invalid child count")

    target_url = _sanitize_target_url(str(target_info.get("url") or ""))
    if not target_url:
        raise BrowserSnapshotError("CDP page target has an invalid URL")

    return BrowserPageSnapshot(
        endpoint=endpoint,
        target_id=str(target_info["targetId"]),
        target_type=str(target_info["type"]),
        title=_bounded_text(target_info.get("title"), _MAX_TITLE_CHARS),
        url=target_url,
        frame_count=frame_count,
        main_frame_url=main_frame_url,
        main_frame_mime_type=_bounded_text(main_frame.get("mimeType"), _MAX_MIME_TYPE_CHARS),
        document_node_name=document_node_name[:64],
        document_child_count=document_child_count,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    endpoint = os.environ.get(_ATTACH_ENDPOINT_ENV, "").strip()
    target_id = os.environ.get(_SNAPSHOT_TARGET_ENV, "").strip()
    if not endpoint or not target_id:
        print(
            json.dumps(
                {"error": "browser CDP snapshot endpoint or target is missing"}, sort_keys=True
            )
        )
        return 2
    if not 1 <= args.timeout <= 120:
        print(json.dumps({"error": "browser CDP snapshot timeout is invalid"}, sort_keys=True))
        return 2

    try:
        result = _run_snapshot(endpoint, target_id, args.timeout)
    except (BrowserSnapshotError, BrowserAttachError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"error": "browser CDP snapshot failed"}, sort_keys=True))
        return 1

    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
