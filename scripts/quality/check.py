#!/usr/bin/env python3
"""Fast offline quality ratchets: active docs, scoped guidance and Python ownership.

No network, installed-service inspection, mutation, subprocesses or third-party deps.
Run: python scripts/quality/check.py [--root DIR] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]

# Only authoritative/live-facing docs: dated evidence is immutable historical data.
ACTIVE_DOCS = (
    'AGENTS.md',
    'CONTRIBUTING.md',
    'INSTALL_WITH_CODEX.md',
    'README.md',
    'chat_bridge/README.md',
    'deploy/macos/README.md',
    'docs/AGENTS.md',
    'docs/ARCHITECTURE.md',
    'docs/CHANGELOG.md',
    'docs/DESKTOP_MENU_BUNDLE.md',
    'docs/FAQ.md',
    'docs/GOLDEN_STANDARD.md',
    'docs/HANDOFF_PROTOCOL.md',
    'docs/HOST_CONTROL_PLANE.md',
    'docs/OPERATIONS.md',
    'docs/QUALITY_TOOLING.md',
    'docs/README.md',
    'docs/RELEASE_NOTES_V4.20.13.md',
    'docs/REPOSITORY_ONBOARDING.md',
    'docs/SECURITY_MODEL.md',
    'docs/SUPPORT.md',
    'docs/USAGE.md',
    'docs/host_ops/TOOL_INVENTORY.md',
    'docs/host_ops/architecture/TARGET_MODEL.md',
    'docs/host_ops/operations/BROWSER.md',
    'docs/host_ops/security/SECURITY_MODEL.md',
    'platforms/README.md',
    'platforms/android/README.md',
    'platforms/android/local-agent/AGENTS.md',
    'platforms/android/local-agent/INSTALL_WITH_CODEX.md',
    'platforms/android/local-agent/README.md',
    'platforms/linux/README.md',
    'platforms/linux/local-agent/INSTALL_WITH_CODEX.md',
    'platforms/linux/local-agent/README.md',
    'platforms/macos/README.md',
    'platforms/macos/installer/INSTALL_WITH_CODEX.md',
    'platforms/macos/installer/README.md',
    'platforms/macos/local-agent/AGENTS.md',
    'platforms/macos/local-agent/README.md',
    'platforms/macos/local-agent/host_ops/AGENTS.md',
    'platforms/macos/local-agent/repository/AGENTS.md',
    'platforms/macos/local-agent/supervisor/AGENTS.md',
    'platforms/raspberry-pi/local-agent/INSTALL_WITH_CODEX.md',
    'platforms/raspberry-pi/local-agent/README.md',
    'platforms/ubuntu/local-agent/README.md',
    'platforms/windows/local-agent/README.md',
    'scripts/AGENTS.md',
    'tests/AGENTS.md',
)

SCOPED_GUIDANCE = (
    "AGENTS.md", "docs/AGENTS.md", "platforms/macos/local-agent/AGENTS.md",
    "platforms/macos/local-agent/repository/AGENTS.md",
    "platforms/macos/local-agent/supervisor/AGENTS.md",
    "platforms/macos/local-agent/host_ops/AGENTS.md",
    "scripts/AGENTS.md", "tests/AGENTS.md",
    "platforms/android/local-agent/AGENTS.md",
)
# Existing over-budget modules are grandfathered at their exact measured baseline.
# Their growth must be accompanied by refactoring or an intentional reviewed update.
MODULE_BUDGET_LOC = 750
# Runtime packages on every accepted host. Keep test fixtures outside this budget.
MODULE_ROOTS = (
    "platforms/macos/local-agent",
    "platforms/linux/core",
    "platforms/linux/local-agent",
    "platforms/raspberry-pi/local-agent",
    "platforms/android/local-agent",
)
OVERSIZE_BASELINE = {
    "platforms/macos/local-agent/supervisor/orchestrator.py": 1514,
    "platforms/macos/local-agent/daemon/service.py": 1438,
    "platforms/macos/local-agent/development/live_runner.py": 1312,
    "platforms/macos/local-agent/foundation/core.py": 1177,
    "platforms/macos/local-agent/development/mvp_flow.py": 879,
    "platforms/macos/local-agent/cli/diagnostics.py": 841,
    "platforms/macos/local-agent/repository/worker.py": 817,
}
# Shared observation and planning must not start depending on a daemon/UI/scheduler.
CORE_MODULES = (
    "platforms/macos/local-agent/runtime/health_snapshot.py",
    "platforms/macos/local-agent/runtime/backlog_snapshot.py",
    "platforms/macos/local-agent/repository/onboarding.py",
)
FORBIDDEN_IMPORTS = (
    "local_agent.daemon", "local_agent.supervisor", "local_agent.desktop",
    "local_agent.cli", "PySide6", "PyQt5", "PyQt6",
)

FENCES = re.compile(r"^\s{0,3}(`{3,}|~{3,}).*?^\s{0,3}\1\s*$", re.MULTILINE | re.DOTALL)
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


def markdown_destinations(text: str) -> list[str]:
    plain = FENCES.sub("", text)
    return [*INLINE_LINK.findall(plain), *REFERENCE_LINK.findall(plain)]


def local_link(raw: str) -> str | None:
    candidate = raw.strip()
    if candidate.startswith("<") and ">" in candidate:
        candidate = candidate[1:candidate.index(">")]
    else:
        candidate = candidate.split(maxsplit=1)[0] if candidate else ""
    if (
        not candidate or candidate.startswith(("#", "/"))
        or re.match(r"^[a-z][a-z0-9+.-]*:", candidate, re.IGNORECASE)
        or candidate.startswith("//")
    ):
        return None
    return unquote(candidate.split("#", 1)[0].split("?", 1)[0]) or None


def check_docs(root: Path, paths: tuple[str, ...] = ACTIVE_DOCS) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    anchor = root.resolve()
    for name in paths:
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: missing active documentation")
            continue
        try:
            links = markdown_destinations(path.read_text(encoding="utf-8"))
        except UnicodeError as exc:
            errors.append(f"{name}: invalid UTF-8 ({exc})")
            continue
        for raw in links:
            dest = local_link(raw)
            if dest is None:
                continue
            checked += 1
            target = (path.parent / dest).resolve(strict=False)
            try:
                target.relative_to(anchor)
            except ValueError:
                errors.append(f"{name}: link escapes repository ({raw})")
                continue
            if not target.exists():
                errors.append(f"{name}: broken relative link ({raw})")
    return errors, checked


def check_agents(root: Path) -> list[str]:
    errors: list[str] = []
    for name in SCOPED_GUIDANCE:
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: missing scoped AGENTS.md")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"{name}: empty scoped AGENTS.md")
    return errors


def _import_names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def check_core_boundaries(root: Path) -> list[str]:
    errors: list[str] = []
    for name in CORE_MODULES:
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: required shared core module missing")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"{name}: cannot parse Python ({exc})")
            continue
        for target in sorted(_import_names(tree)):
            if any(target == prefix or target.startswith(prefix + ".")
                   for prefix in FORBIDDEN_IMPORTS):
                errors.append(f"{name}: forbidden upward import {target}")
    return errors


def check_module_budget(
    root: Path, *, limit: int = MODULE_BUDGET_LOC,
    baseline: dict[str, int] | None = None,
    roots: tuple[str, ...] = MODULE_ROOTS,
) -> tuple[list[str], list[tuple[str, int]]]:
    allowed = OVERSIZE_BASELINE if baseline is None else baseline
    errors: list[str] = []
    oversized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for relative in roots:
        folder = root / relative
        if not folder.is_dir():
            errors.append(f"{relative}: package missing")
            continue
        for path in sorted(folder.rglob("*.py")):
            if "tests" in path.relative_to(folder).parts:
                continue
            if path.is_symlink() or any(
                parent.is_symlink() for parent in path.parents
                if parent != root and parent.is_relative_to(root)
            ):
                continue
            name = path.relative_to(root).as_posix()
            # Match ordinary human-visible LOC, not an AST count or byte count.
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines <= limit:
                continue
            oversized.append((name, lines))
            if name not in allowed:
                errors.append(f"{name}: new oversized module {lines} LOC > {limit}")
            elif lines > allowed[name]:
                errors.append(
                    f"{name}: complexity debt grew {allowed[name]} -> {lines} LOC"
                )
            seen.add(name)
    for name in sorted(set(allowed) - seen):
        # Retired or reduced debt should be removed from the exception map.
        errors.append(f"{name}: stale oversized baseline; remove its exception")
    return errors, oversized


def check(root: Path) -> dict[str, object]:
    docs, links = check_docs(root)
    agents = check_agents(root)
    imports = check_core_boundaries(root)
    budget, oversize = check_module_budget(root)
    return {
        "status": "pass" if not (docs or agents or imports or budget) else "fail",
        "active_docs": len(ACTIVE_DOCS),
        "relative_links": links,
        "agent_scopes": len(SCOPED_GUIDANCE),
        "grandfathered_oversize_modules": len(oversize),
        "errors": [*docs, *agents, *imports, *budget],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(args.root.resolve())
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        print(
            f"quality {result['status']}: {result['active_docs']} docs, "
            f"{result['relative_links']} relative links, "
            f"{result['agent_scopes']} scoped AGENTS, "
            f"{result['grandfathered_oversize_modules']} legacy large modules"
        )
        for error in result["errors"]:
            print(f"ERROR {error}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
