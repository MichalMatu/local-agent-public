#!/usr/bin/env python3
"""Read-only, deterministic-structure session handoff generator.

No network, no access to live production, no Git writes or dependency install.
Source facts come from the local Git checkout; operational facts must be
rechecked through the current owner-approved workflow at resume time.

Usage:
    python scripts/handoff.py --check
    python scripts/handoff.py --format markdown
    python scripts/handoff.py --format json
    python scripts/handoff.py --output /private/tmp/local-agent-handoff.md

By design it does not overwrite canonical docs or claim tests passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "MichalMatu/local-agent-public"
DEVELOPMENT_BRANCH = "main"
CANONICAL_DOCS = ('AGENTS.md', 'README.md', 'INSTALL_WITH_CODEX.md', 'CONTRIBUTING.md', 'docs/ARCHITECTURE.md', 'docs/HOST_CONTROL_PLANE.md', 'docs/REPOSITORY_ONBOARDING.md', 'docs/QUALITY_TOOLING.md', 'docs/HANDOFF_PROTOCOL.md', 'docs/SUPPORT.md')
CONTRACT_TEXT = {'AGENTS.md': ('github_writers', 'immutable', 'UNKNOWN'), 'INSTALL_WITH_CODEX.md': ('private', 'unique', 'PASS/PENDING/FAIL')}

def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True, capture_output=True, timeout=15, check=False,
    )
    if proc.returncode:
        # Never include stderr: Git remote URLs and credentials are private.
        raise ValueError(f"Git metadata unavailable ({args[0]})")
    return proc.stdout.strip()


def source_state(root: Path) -> dict[str, object]:
    """Read-only snapshot; no remote fetch and no PR/production status claims."""
    repo_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if repo_root != root:
        raise ValueError("root must be a Git repository toplevel")
    sha = _git(root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("invalid Git HEAD")
    branch = _git(root, "branch", "--show-current") or None
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal"))
    return {
        "sha": sha,
        "branch": branch,
        "detached": branch is None,
        "clean": not dirty,
        "repository": REPOSITORY,
        "expected_branch": DEVELOPMENT_BRANCH,
        # No remote query is needed; a checked-out Git repository is not
        # automatically verified to belong to the named GitHub repository.
        "remote_identity_verified": False,
    }


def audit_docs(
    root: Path, state: dict[str, object], *, require_approved_branch: bool = True
) -> list[str]:
    errors: list[str] = []
    for name in CANONICAL_DOCS:
        path = root / name
        if not path.is_file() or path.is_symlink():
            errors.append(f"{name}: required canonical document missing or symlinked")
            continue
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            errors.append(f"{name}: empty canonical document")
        for required in CONTRACT_TEXT.get(name, ()):
            if required.casefold() not in content.casefold():
                errors.append(f"{name}: required handoff contract missing ({required})")
    if require_approved_branch and state["branch"] not in (None, "main", DEVELOPMENT_BRANCH):
        errors.append("checked-out branch differs from approved source branch")
    if not state["clean"]:
        errors.append("checkout has uncommitted changes; commit or separately explain them")
    return errors


def evidence_for_sha(evidence_path: Path | None, sha: str) -> dict[str, object]:
    """User-supplied evidence, clearly labelled as unverified by the generator."""
    if evidence_path is None:
        return {"status": "NOT_PROVIDED", "checks": []}
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict) or evidence.get("version") != 1:
        raise ValueError("handoff evidence must have version 1")
    receipt_sha = evidence.get("source_sha")
    if not isinstance(receipt_sha, str) or re.fullmatch(r"[0-9a-f]{40}", receipt_sha) is None:
        raise ValueError("evidence must identify a 40-character source_sha")
    checks = evidence.get("checks")
    if not isinstance(checks, list) or len(checks) > 25:
        raise ValueError("evidence checks must be a bounded list")
    summary: list[dict[str, str]] = []
    for item in checks:
        if not isinstance(item, dict):
            raise ValueError("invalid handoff evidence check")
        label, status = item.get("name"), item.get("status")
        if (not isinstance(label, str) or not 1 <= len(label) <= 90
                or re.fullmatch(r"[a-zA-Z0-9 _./:-]+", label) is None
                or status not in ("PASS", "FAIL", "TIMEOUT", "SKIP", "UNKNOWN", "UNVERIFIED")):
            raise ValueError("invalid handoff evidence name or status")
        summary.append({"name": label, "status": status})
    # A receipt from an earlier source SHA must not be attached as current PASS.
    return {
        "status": "SUPPLIED_MATCHING_SHA" if receipt_sha == sha else "STALE_SHA_UNVERIFIED",
        "source_sha": receipt_sha,
        "checks": summary if receipt_sha == sha else [],
        "provenance": "provided by operator; not independently checked by this generator",
    }


def snapshot(
    root: Path, evidence_path: Path | None = None, *,
    require_approved_branch: bool = True,
) -> dict[str, object]:
    root = root.resolve()
    state = source_state(root)
    errors = audit_docs(root, state, require_approved_branch=require_approved_branch)
    return {
        "schema_version": 1,
        "generation_status": "pass" if not errors else "fail",
        "source": state,
        "documents": list(CANONICAL_DOCS),
        "contract_errors": errors,
        "test_evidence": evidence_for_sha(evidence_path, str(state["sha"])),
        "pr_state": "NOT_CHECKED_REVERIFY_REMOTE",
        "live_installation_state": "NOT_CHECKED_REVERIFY_SELECTED_INSTALLATION",
        "release_acceptance": "NOT_ASSESSED_OFFLINE",
    }


def markdown(root: Path, report: dict[str, object]) -> str:
    source = report["source"]
    evidence = report["test_evidence"]
    lines = [
        "# Local Agent — source handoff",
        "",
        "**Generator status:** " + str(report["generation_status"]).upper(),
        "**Source SHA:** `" + str(source["sha"]) + "`",
        "**Checked-out branch:** `" + str(source["branch"] or "DETACHED") + "`",
        "**Working tree clean:** " + ("YES" if source["clean"] else "NO"),
        "",
        "## Offline scope",
        "",
        "Live installation, GitHub state and remote identity: **NOT CHECKED**.",
        "This artifact cannot approve a release or assert service health.",
        "",
        "## Evidence scoped to this exact source",
        "",
        "- Evidence: **" + str(evidence["status"]) + "**.",
    ]
    for item in evidence["checks"]:
        lines.append(f"- Operator-provided: {item['name']}: **{item['status']}** "
                     "(not independently verified by generator).")
    if not evidence["checks"]:
        lines.append("- No trustworthy exact-SHA test PASS supplied.")
    lines.extend(["", "## Product contracts", ""])
    lines.extend("- `" + name + "`" for name in report["documents"])
    if report["contract_errors"]:
        lines.extend(["", "## Blocking consistency errors", ""])
        lines.extend("- " + error for error in report["contract_errors"])
    lines.extend([
        "", "## Do next", "",
        "1. Read AGENTS.md and the relevant installation/product contracts.",
        "2. Follow the owner's assigned scope; refresh live evidence only where needed.",
        "3. Scope receipts to their actual SHA, inputs and installation.",
        "4. Preserve immutable tasks, UNKNOWN, private ledgers and independent recovery.",
        "5. No standing audit, deployment or command replay follows from this snapshot.",
        "",
    ])
    return "\n".join(lines)


def _write_private_output(path: Path, repo_root: Path, content: str) -> None:
    # Require output outside source: no generated changes can silently enter Git.
    resolved_parent = path.parent.resolve()
    if resolved_parent == repo_root or repo_root in resolved_parent.parents:
        raise ValueError("handoff output must be outside source checkout")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--check", action="store_true", help="strict canonical handoff validation")
    p.add_argument(
        "--check-source", action="store_true",
        help="check clean source/docs without restricting a temporary review branch",
    )
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--evidence", type=Path, help="optional operator-supplied SHA-scoped JSON receipt")
    p.add_argument("--output", type=Path, help="create new private artifact OUTSIDE Git checkout")
    args = p.parse_args()
    if (args.check or args.check_source) and args.output:
        p.error("checks cannot write an output artifact")
    if args.check and args.check_source:
        p.error("choose either --check or --check-source")
    try:
        root = args.root.resolve()
        result = snapshot(
            root, args.evidence, require_approved_branch=not args.check_source,
        )
        if args.check or args.check_source:
            print(json.dumps({
                "status": result["generation_status"],
                "source_sha": result["source"]["sha"],
                "clean": result["source"]["clean"],
                "issues": result["contract_errors"],
                "live_state_verified": False,
            }, sort_keys=True))
        else:
            rendered = (json.dumps(result, indent=2, sort_keys=True) + "\n"
                        if args.format == "json" else markdown(root, result))
            if args.output:
                _write_private_output(args.output, root, rendered)
                print(f"handoff artifact created: {args.output}")
            else:
                print(rendered)
        return 0 if result["generation_status"] == "pass" else 1
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"handoff UNVERIFIED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
