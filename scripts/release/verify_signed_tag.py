#!/usr/bin/env python3
"""Read-only gate for an exact-SHA, cryptographically signed release tag.

Git signature validity does not establish signer identity by itself. Operators
must separately compare the verified signer with a trusted fingerprint/key.
No private key is ever read, created or uploaded by this verifier.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(root: Path, *args: str) -> tuple[int, str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Do not echo Git stderr, which could contain local configuration or paths.
    return result.returncode, result.stdout.strip()


def check_signed_tag(root: Path, tag: str, expected_sha: str) -> dict[str, str]:
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        return {"status": "FAIL", "reason": "invalid_release_tag"}
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        return {"status": "FAIL", "reason": "invalid_expected_sha"}
    root = Path(root).resolve()
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or top[0] != 0 or Path(top[1]) != root:
        return {"status": "UNVERIFIED", "reason": "repository_root_unavailable"}
    head = _git(root, "rev-parse", "HEAD^{commit}")
    if head is None or head != (0, expected_sha):
        return {"status": "FAIL", "reason": "source_head_mismatch"}
    ref = "refs/tags/" + tag
    object_type = _git(root, "cat-file", "-t", ref)
    if object_type is None or object_type != (0, "tag"):
        return {"status": "FAIL", "reason": "signed_annotated_tag_required"}
    target = _git(root, "rev-parse", ref + "^{commit}")
    if target is None or target != (0, expected_sha):
        return {"status": "FAIL", "reason": "tag_target_mismatch"}
    verified = _git(root, "verify-tag", ref)
    if verified is None:
        return {"status": "UNVERIFIED", "reason": "signature_verifier_unavailable"}
    if verified[0] != 0:
        return {"status": "FAIL", "reason": "tag_signature_invalid_or_missing"}
    return {"status": "PASS", "reason": "signature_valid_signer_identity_check_separate"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="The expected immutable release tag")
    parser.add_argument("--expected-sha", required=True, help="40-character release commit SHA")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    options = parser.parse_args()
    result = check_signed_tag(options.root, options.tag, options.expected_sha)
    print(json.dumps(result, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
