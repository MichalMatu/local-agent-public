"""CLI rendering for local browser inspection, probing and CDP attachment."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from local_agent.host_ops.capabilities.local.browser import (
    BrowserAttachError,
    BrowserCdpAttacher,
    BrowserCdpContentScriptRecoverer,
    BrowserCdpReadinessInspector,
    BrowserCdpReloader,
    BrowserCdpSelectorCounter,
    BrowserCdpSnapshotter,
    BrowserCdpWorkerDiagnoser,
    BrowserContentScriptRecoveryError,
    BrowserInspectionError,
    BrowserInspector,
    BrowserReadinessError,
    BrowserReloadError,
    BrowserSelectorCountError,
    BrowserSnapshotError,
    BrowserWorkerDiagnosticsError,
    ManagedBrowserError,
    ManagedBrowserProber,
)
from local_agent.host_ops.core.execution import ExecutionLimits

from .browser_session import run_browser_session


def run_inspect(
    *,
    endpoints: Sequence[str],
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        inspection = BrowserInspector().inspect(
            endpoints=endpoints,
            timeout_seconds=timeout_seconds,
            limits=ExecutionLimits(
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=1_048_576,
                max_stderr_bytes=16_384,
            ),
        )
    except (BrowserInspectionError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser inspect failed: {exc}", file=sys.stderr)
        return 1

    payload = inspection.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    for process in inspection.processes:
        parts = [f"pid={process.pid}", f"family={process.family}"]
        if process.remote_debugging_port is not None:
            parts.append(f"remote_debugging_port={process.remote_debugging_port}")
        if process.remote_debugging_pipe:
            parts.append("remote_debugging_pipe=true")
        print(" ".join(parts))
    for endpoint in inspection.endpoints:
        status = "reachable" if endpoint.reachable else "unreachable"
        print(f"endpoint={endpoint.endpoint} status={status} targets={len(endpoint.targets)}")
    for warning in inspection.warnings:
        print(f"warning={warning}")
    return 0


def run_probe(
    url: str,
    *,
    engine: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = ManagedBrowserProber().probe(
            url,
            engine=engine,
            timeout_seconds=timeout_seconds,
        )
    except (ManagedBrowserError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser probe failed: {exc}", file=sys.stderr)
        return 1

    payload = result.as_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    print(
        " ".join(
            [
                f"engine={result.engine}",
                f"status={result.status_code}",
                f"url={result.final_url}",
                f"title={result.title!r}",
            ]
        )
    )
    print(
        " ".join(
            [
                f"console_errors={result.console_error_count}",
                f"page_errors={result.page_error_count}",
                f"request_failures={result.request_failure_count}",
            ]
        )
    )
    return 0


def run_attach_inspect(
    endpoint: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpAttacher().inspect(endpoint, timeout_seconds=timeout_seconds)
    except (BrowserAttachError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach inspect failed: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0

    print(
        " ".join(
            [
                f"endpoint={result.endpoint}",
                f"browser_version={result.browser_version}",
                f"contexts={result.context_count}",
                f"targets={len(result.targets)}",
            ]
        )
    )
    for target in result.targets:
        print(
            " ".join(
                [
                    f"id={target.target_id}",
                    f"type={target.target_type}",
                    f"attached={str(target.attached).lower()}",
                    f"url={target.url}",
                    f"title={target.title!r}",
                ]
            )
        )
    return 0


def run_attach_snapshot(
    endpoint: str,
    target_id: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpSnapshotter().snapshot(
            endpoint, target_id, timeout_seconds=timeout_seconds
        )
    except (BrowserSnapshotError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach snapshot failed: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0

    print(
        " ".join(
            [
                f"target_id={result.target_id}",
                f"type={result.target_type}",
                f"url={result.url}",
                f"title={result.title!r}",
                f"frames={result.frame_count}",
                f"document={result.document_node_name}",
                f"document_children={result.document_child_count}",
            ]
        )
    )
    return 0


def run_attach_selectors(
    endpoint: str,
    target_id: str,
    selectors: Sequence[str],
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpSelectorCounter().count(
            endpoint, target_id, selectors, timeout_seconds=timeout_seconds
        )
    except (BrowserSelectorCountError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach selectors failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    print(
        " ".join(
            [
                f"target_id={result.target_id}",
                f"type={result.target_type}",
                f"url={result.url}",
                f"title={result.title!r}",
            ]
        )
    )
    for item in result.selectors:
        print(f"selector={item.selector!r} matches={item.match_count}")
    return 0


def run_attach_readiness(
    endpoint: str,
    target_id: str,
    selectors: Sequence[str],
    script_fingerprints: Sequence[str],
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpReadinessInspector().inspect(
            endpoint,
            target_id,
            selectors,
            script_fingerprints,
            timeout_seconds=timeout_seconds,
        )
    except (BrowserReadinessError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach readiness failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    print(
        " ".join(
            [
                f"target_id={result.target_id}",
                f"type={result.target_type}",
                f"url={result.url}",
                f"title={result.title!r}",
                f"dom_ready={str(result.dom_ready).lower()}",
                f"content_script={result.content_script_state}",
                f"worker={result.worker_state}",
                f"diagnosis={result.diagnosis}",
            ]
        )
    )
    for selector_item in result.selectors:
        print(f"selector={selector_item.selector!r} matches={selector_item.match_count}")
    for script_item in result.extension_scripts:
        print(f"extension_script={script_item.name!r} status={script_item.status}")
    return 0


def run_attach_content_script_recovery(
    endpoint: str,
    target_id: str,
    expected_url: str,
    selectors: Sequence[str],
    script_fingerprints: Sequence[str],
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpContentScriptRecoverer().recover(
            endpoint,
            target_id,
            expected_url,
            selectors,
            script_fingerprints,
            timeout_seconds=timeout_seconds,
        )
    except (BrowserContentScriptRecoveryError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach recover-content-script failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0

    parts = [
        f"target_id={result.target_id}",
        f"expected_url={result.expected_url}",
        f"action={result.action}",
        f"outcome={result.outcome}",
        f"before={result.before.diagnosis}",
    ]
    if result.after is not None:
        parts.append(f"after={result.after.diagnosis}")
    print(" ".join(parts))
    return 0


def run_attach_workers(
    endpoint: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpWorkerDiagnoser().inspect(endpoint, timeout_seconds=timeout_seconds)
    except (BrowserWorkerDiagnosticsError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach workers failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0

    print(
        " ".join(
            [
                f"endpoint={result.endpoint}",
                f"worker_targets={len(result.worker_targets)}",
                f"registrations={len(result.registrations)}",
                f"versions={len(result.versions)}",
                f"errors={result.error_count}",
            ]
        )
    )
    for target in result.worker_targets:
        print(
            " ".join(
                [
                    f"target_id={target.target_id}",
                    f"type={target.target_type}",
                    f"attached={str(target.attached).lower()}",
                    f"url={target.url}",
                ]
            )
        )
    for registration in result.registrations:
        print(
            f"registration_scope={registration.scope_url} "
            f"deleted={str(registration.is_deleted).lower()}"
        )
    for version in result.versions:
        print(
            " ".join(
                [
                    f"service_worker_target={version.target_id or '-'}",
                    f"scope={version.scope_url or '-'}",
                    f"script={version.script_url}",
                    f"running={version.running_status}",
                    f"lifecycle={version.lifecycle_status}",
                    f"controlled_clients={version.controlled_client_count}",
                    f"target_attached={version.target_attached}",
                    f"registration_deleted={version.registration_deleted}",
                ]
            )
        )
    return 0


def run_attach_reload(
    endpoint: str,
    target_id: str,
    expected_url: str,
    *,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = BrowserCdpReloader().reload(
            endpoint,
            target_id,
            expected_url,
            timeout_seconds=timeout_seconds,
        )
    except (BrowserReloadError, ValueError) as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"browser attach reload failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 0
    print(
        " ".join(
            [
                f"target_id={result.target_id}",
                f"type={result.target_type}",
                f"before_url={result.before_url}",
                f"before_title={result.before_title!r}",
                f"after_url={result.after_url}",
                f"after_title={result.after_title!r}",
            ]
        )
    )
    return 0


def run_command(args: argparse.Namespace) -> int:
    if args.browser_command == "inspect":
        return run_inspect(
            endpoints=args.endpoints,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "probe":
        return run_probe(
            args.url,
            engine=args.engine,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "session":
        return run_browser_session(args)
    if args.browser_command == "attach" and args.browser_attach_command == "inspect":
        return run_attach_inspect(
            args.endpoint,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "snapshot":
        return run_attach_snapshot(
            args.endpoint,
            args.target_id,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "selectors":
        return run_attach_selectors(
            args.endpoint,
            args.target_id,
            args.selectors,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "readiness":
        return run_attach_readiness(
            args.endpoint,
            args.target_id,
            args.selectors,
            args.script_fingerprints,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "recover-content-script":
        return run_attach_content_script_recovery(
            args.endpoint,
            args.target_id,
            args.expected_url,
            args.selectors,
            args.script_fingerprints,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "workers":
        return run_attach_workers(
            args.endpoint,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    if args.browser_command == "attach" and args.browser_attach_command == "reload":
        return run_attach_reload(
            args.endpoint,
            args.target_id,
            args.expected_url,
            timeout_seconds=args.timeout_seconds,
            as_json=args.as_json,
        )
    raise AssertionError(f"unhandled browser command: {args.browser_command}")
