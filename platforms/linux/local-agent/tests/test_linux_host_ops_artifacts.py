"""Offline file Host Ops tests: no external devices, services or user repositories."""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent.host_ops_artifacts import ArtifactError, inspect_artifact, copy_artifact


class ArtifactTests(unittest.TestCase):
    def test_inspect_and_atomic_copy_with_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            src, dest = base / "source", base / "dest"
            payload = b"Linux Host Ops \x00" * 10000
            src.write_bytes(payload)
            info = inspect_artifact(str(src))
            self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(info["size_bytes"], len(payload))
            result = copy_artifact(str(src), str(dest))
            self.assertTrue(result["verified"])
            self.assertTrue(result["directory_fsynced"])
            self.assertEqual(dest.read_bytes(), payload)
            self.assertFalse(list(base.glob(".la-hostops-*.part")))
            with self.assertRaisesRegex(ArtifactError, "already exists"):
                copy_artifact(str(src), str(dest))
            self.assertEqual(dest.read_bytes(), payload)

    def test_limit_missing_nonregular_and_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src = root / "source"
            src.write_bytes(b"1234")
            with self.assertRaisesRegex(ArtifactError, "max_bytes"):
                inspect_artifact(str(src), max_bytes=3)
            with self.assertRaisesRegex(ArtifactError, "max_bytes"):
                copy_artifact(str(src), str(root / "dest"), max_bytes=3)
            self.assertFalse((root / "dest").exists())
            with self.assertRaises(ArtifactError):
                inspect_artifact("relative")
            with self.assertRaises(ArtifactError):
                inspect_artifact(str(root / ".." / root.name / "source"))
            with self.assertRaises(ArtifactError):
                inspect_artifact(str(root))
            with self.assertRaises(ArtifactError):
                inspect_artifact(str(root / "missing"))
            with self.assertRaises(ArtifactError):
                inspect_artifact(str(src), max_bytes=True)
            with self.assertRaises(ArtifactError):
                inspect_artifact(str(src), timeout=0)

    def test_reject_parent_and_source_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            actual = root / "real"
            actual.mkdir()
            (actual / "data").write_bytes(b"data")
            (root / "link").symlink_to(actual, target_is_directory=True)
            (root / "source_link").symlink_to(actual / "data")
            for source in (root / "source_link", root / "link" / "data"):
                with self.subTest(path=str(source)), self.assertRaises(ArtifactError):
                    inspect_artifact(str(source))
            with self.assertRaises(ArtifactError):
                copy_artifact(str(actual / "data"), str(root / "link" / "out"))
            self.assertFalse((actual / "out").exists())

    def test_copy_destination_is_no_clobber_even_for_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src, protected, dest = root / "src", root / "protected", root / "dest"
            src.write_bytes(b"new")
            protected.write_bytes(b"old")
            dest.symlink_to(protected)
            with self.assertRaisesRegex(ArtifactError, "already exists"):
                copy_artifact(str(src), str(dest))
            self.assertTrue(dest.is_symlink())
            self.assertEqual(protected.read_bytes(), b"old")
            self.assertFalse(list(root.glob(".la-hostops-*.part")))

    def test_fails_before_commit_when_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src, dest = root / "src", root / "dest"
            src.write_bytes(b"safe")
            import linux_agent.host_ops_artifacts as artifacts
            original = artifacts._source_matches

            def changed(parent, name, initial):
                if name == "src":
                    raise ArtifactError("artifact changed during operation")
                return original(parent, name, initial)

            with mock.patch.object(artifacts, "_source_matches", side_effect=changed):
                with self.assertRaises(ArtifactError):
                    copy_artifact(str(src), str(dest))
            self.assertFalse(dest.exists())
            self.assertFalse(list(root.glob(".la-hostops-*.part")))


    def test_postcommit_failure_is_explicit_and_no_clobber_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            src, dest = root / "src", root / "dest"
            src.write_bytes(b"stable")
            import linux_agent.host_ops_artifacts as artifacts
            with mock.patch.object(artifacts, "_hash", side_effect=ArtifactError("injected verify")):
                with self.assertRaisesRegex(ArtifactError, "already committed"):
                    copy_artifact(str(src), str(dest))
            self.assertEqual(dest.read_bytes(), b"stable")
            with self.assertRaisesRegex(ArtifactError, "already exists"):
                copy_artifact(str(src), str(dest))


if __name__ == "__main__":
    unittest.main()
