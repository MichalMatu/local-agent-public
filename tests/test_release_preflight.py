"""Negative receipt tests; never run host services."""
from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
import warnings
import os
import tempfile
from pathlib import Path
import unittest
from scripts.release.preflight import (
    GLOBAL_GATES, HOST_GATES, REQUIRED_ARTIFACTS, artifact_gaps, missing_receipts,
    run_source_gates, SOURCE_BUNDLE_REQUIRED, source_bundle_matches_checkout,
)

SHA = "a" * 40


def make_bundle(root: Path, *, omit: str | None = None,
                prefix: str = "local-agent/") -> Path:
    """Build a tiny complete source archive from actual temporary source files."""
    for name in SOURCE_BUNDLE_REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(("fixture:" + name).encode("utf-8"))
    target = root / "source_bundle.tar.gz"
    with tarfile.open(target, mode="w:gz") as archive:
        for name in SOURCE_BUNDLE_REQUIRED:
            if name == omit:
                continue
            payload = (root / name).read_bytes()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return target



class ReleasePreflightTests(unittest.TestCase):
    def test_missing_or_stale_receipts_are_rejected(self) -> None:
        self.assertEqual(missing_receipts({}, SHA, ("macos",)), ["invalid_receipt_schema"])
        self.assertEqual(missing_receipts({
            "schema_version": 1, "source_sha": "b" * 40, "gates": {},
        }, SHA, ("macos",)), ["receipt_source_sha_mismatch"])

    def test_unchecked_platform_remains_unverified(self) -> None:
        data = {"schema_version": 1, "source_sha": SHA, "gates": {
            key: {"status": "PASS", "source_sha": SHA} for key in GLOBAL_GATES
        }}
        self.assertEqual(missing_receipts(data, SHA, ("macos", "debian")), [
            *(f"macos.{key}" for key in HOST_GATES),
            *(f"debian.{key}" for key in HOST_GATES),
        ])

    def test_full_matrix_requires_exact_source(self) -> None:
        keys = (*GLOBAL_GATES, *(f"macos.{key}" for key in HOST_GATES))
        gates = {key: {"status": "PASS", "source_sha": SHA} for key in keys}
        data = {"schema_version": 1, "source_sha": SHA, "gates": gates}
        self.assertEqual(missing_receipts(data, SHA, ("macos",)), [])
        gates["g2_stop"]["source_sha"] = "b" * 40
        self.assertEqual(missing_receipts(data, SHA, ("macos",)), ["g2_stop"])


    def test_local_release_artifact_hashes_and_missing_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-preflight-") as tmp:
            root = Path(tmp)
            source_bundle = make_bundle(root)
            paths = {}
            for name in REQUIRED_ARTIFACTS:
                filename = (
                    "requirements-runtime.lock" if name == "runtime_lock"
                    else source_bundle.name if name == "source_bundle"
                    else name + ".json"
                )
                path = root / filename
                if name in ("runtime_sbom", "third_party_notices"):
                    path.write_bytes(("test-" + name).encode("utf-8"))
                paths[name] = {
                    "path": filename,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            receipt = {"artifacts": paths}
            self.assertEqual(artifact_gaps(receipt, root), [])
            paths["runtime_sbom"]["sha256"] = "a" * 64
            self.assertEqual(artifact_gaps(receipt, root), ["runtime_sbom"])
            (root / "third_party_notices.json").unlink()
            self.assertEqual(artifact_gaps(receipt, root),
                             ["runtime_sbom", "third_party_notices"])

    def test_archive_completeness_and_source_bytes_are_required(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-archive-") as tmp:
            root = Path(tmp)
            archive = make_bundle(root)
            self.assertTrue(source_bundle_matches_checkout(archive, root))
            (root / "AGENTS.md").write_text("modified", encoding="utf-8")
            self.assertFalse(source_bundle_matches_checkout(archive, root))
            incomplete = make_bundle(root, omit="scripts/verify.py")
            self.assertFalse(source_bundle_matches_checkout(incomplete, root))

    def test_archive_rejects_path_traversal_and_accepts_zip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-archive-") as tmp:
            root = Path(tmp)
            make_bundle(root)
            packed = root / "source_bundle.zip"
            with zipfile.ZipFile(packed, mode="w") as archive:
                for name in SOURCE_BUNDLE_REQUIRED:
                    archive.write(root / name, arcname=name)
            self.assertTrue(source_bundle_matches_checkout(packed, root))
            with zipfile.ZipFile(packed, mode="a") as archive:
                archive.writestr("../escape.txt", "bad")
            self.assertFalse(source_bundle_matches_checkout(packed, root))


    def test_archive_must_include_other_runtime_modules_too(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-archive-") as tmp:
            root = Path(tmp)
            archive = make_bundle(root)
            added = root / "platforms/macos/local-agent/supervisor/orchestrator.py"
            added.parent.mkdir(parents=True)
            added.write_text("def source_present(): return True\n", encoding="utf-8")
            self.assertFalse(source_bundle_matches_checkout(archive, root))

    def test_archive_requires_root_launchers_and_package_bridge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-archive-") as tmp:
            root = Path(tmp)
            for name in ("agent_entrypoint.py", "local_agent/__init__.py"):
                with self.subTest(name=name):
                    archive = make_bundle(root, omit=name)
                    self.assertFalse(source_bundle_matches_checkout(archive, root))

    def test_archive_duplicate_and_symlink_members_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-archive-") as tmp:
            root = Path(tmp)
            make_bundle(root)
            zipped = root / "source_bundle.zip"
            with zipfile.ZipFile(zipped, mode="w") as archive:
                for name in SOURCE_BUNDLE_REQUIRED:
                    archive.write(root / name, arcname=name)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(zipped, mode="a") as archive:
                    archive.write(root / "LICENSE", arcname="LICENSE")
            self.assertFalse(source_bundle_matches_checkout(zipped, root))

            linked = root / "source_bundle.tar"
            with tarfile.open(linked, mode="w") as archive:
                for name in SOURCE_BUNDLE_REQUIRED:
                    payload = (root / name).read_bytes()
                    entry = tarfile.TarInfo(name)
                    entry.size = len(payload)
                    archive.addfile(entry, io.BytesIO(payload))
                link = tarfile.TarInfo("unsafe_link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                archive.addfile(link)
            self.assertFalse(source_bundle_matches_checkout(linked, root))

    def test_source_gate_execution_uses_only_canonical_verifier(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-verifier-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "verify.py").write_text(
                "import sys\n"
                "assert sys.argv[1:] in ([\"--profile\", \"platform-contract\"],"
                " [\"--profile\", \"fault-matrix\"])\n"
                "sys.exit(0 if sys.argv[-1] == 'platform-contract' else 7)\n",
                encoding="utf-8",
            )
            self.assertEqual(run_source_gates(root), {
                "platform-contract": "PASS", "fault-matrix": "FAIL",
            })

    def test_source_gate_preserves_unverified_exit_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-verifier-") as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts/verify.py").write_text("raise SystemExit(2)\n")
            self.assertEqual(run_source_gates(root), {
                "platform-contract": "UNVERIFIED", "fault-matrix": "UNVERIFIED",
            })

    @unittest.skipUnless(os.name == "posix", "timeout group cleanup requires POSIX")
    def test_source_gate_timeout_does_not_report_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-release-verifier-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "verify.py").write_text(
                "import time\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            self.assertEqual(run_source_gates(root, timeout_seconds=1), {
                "platform-contract": "UNVERIFIED", "fault-matrix": "UNVERIFIED",
            })


if __name__ == "__main__":
    unittest.main()
