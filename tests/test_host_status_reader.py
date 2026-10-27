"""Pinned Git host status reader acceptance; all transports are local fixtures."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.repository.binding import resolve_execution_target
from local_agent.repository.host_routing import HostIdentity, build_host_status, validate_host_enrollments
from local_agent.repository import host_status_reader as reader

BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
INSTALLATION = "12345678-1234-4234-8234-123456789abc"
SOURCE = "a" * 40
OPERATOR = "b" * 40


class HostStatusReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / "work"
        self.remote = self.root / "remote.git"
        self.work.mkdir()
        self.git("init", "-q", "-b", "agent-control")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps({"version": 1, "agents": [{
            "id": "local-agent", "repository": "owner/local-agent", "agent_binding": BINDING,
        }]}))
        self.target = resolve_execution_target("local-agent", path=self.catalog)
        self.identity = HostIdentity("worker-linux", INSTALLATION)
        self.enrolled = validate_host_enrollments([{
            "version": 1, "host_id": "worker-linux", "installation_id": INSTALLATION,
            "enabled": True, "repositories": [{"repository_id": "local-agent", "agent_binding": BINDING}],
        }])[0]
        self.now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        self.write(".agent/binding.json", {
            "version": 1, "repository_id": "local-agent", "repository": "owner/local-agent",
            "agent_binding": BINDING,
        })
        self.write(".agent/hosts/worker-linux/status.json", build_host_status(
            self.identity, source_revision=SOURCE, state="idle", observed_at=self.now,
        ))
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def write(self, name, record):
        f = self.work / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(record) + "\n")

    def commit(self):
        self.git("add", "--", ".agent")
        self.git("commit", "-q", "-m", "fixture")
        self.git("push", "-q", "-u", "origin", "HEAD:refs/heads/agent-control")
        self.sha = self.git("rev-parse", "HEAD")

    def preview(self, **changes):
        options = {"host_id": "worker-linux", "expected_control_sha": self.sha,
                   "expected_source_sha": SOURCE, "expected_operator_sha": OPERATOR,
                   "enrollment": self.enrolled, "target": self.target, "now": self.now}
        options.update(changes)
        with mock.patch.object(reader, "_validate_control_origin"):
            return reader.preview_host_status_snapshot(self.work, **options)

    def test_pinned_record_is_observation_not_execution_permission(self):
        result = self.preview()
        self.assertEqual(result["status"], "validated_observation")
        self.assertEqual(result["source_sha"], self.sha)
        self.assertEqual(result["observed_state"], "idle")
        self.assertTrue(result["fresh"])
        self.assertFalse(result["publisher_authenticated"])
        self.assertEqual(result["execution_authority"], "not_asserted")
        self.assertFalse(result["claimed"] or result["executed"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_stale_future_and_disabled_observation_is_unknown(self):
        self.assertEqual(self.preview(now=self.now + timedelta(hours=2))["observed_state"], "unknown")
        self.assertEqual(self.preview(now=self.now - timedelta(minutes=2))["observed_state"], "unknown")
        self.write(".agent/hosts/worker-linux/status.json", build_host_status(
            self.identity, source_revision=SOURCE, state="disabled", observed_at=self.now,
        ))
        self.commit()
        self.assertEqual(self.preview()["observed_state"], "unknown")

    def test_wrong_enrollment_or_revision_is_rejected(self):
        revoked = self.enrolled.__class__(identity=self.enrolled.identity, enabled=False,
                                         repositories=self.enrolled.repositories)
        for change in ({"expected_control_sha": "0" * 40},
                       {"expected_source_sha": "0" * 40},
                       {"expected_operator_sha": "short"},
                       {"enrollment": revoked},
                       {"host_id": "other-host"},
                       {"now": datetime.now()}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                self.preview(**change)

    def test_changed_remote_head_fails_closed(self):
        previous = self.sha
        self.git("commit", "-q", "--allow-empty", "-m", "advance")
        self.git("push", "-q", "origin", "HEAD:refs/heads/agent-control")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.preview(expected_control_sha=previous)

    def test_symlink_or_missing_status_is_not_a_regular_blob(self):
        f = self.work / ".agent/hosts/worker-linux/status.json"
        f.unlink()
        f.symlink_to("/etc/passwd")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular Git blob"):
            self.preview()
        f.unlink()
        self.git("add", "-u", "--", ".agent")
        self.git("commit", "-q", "-m", "remove")
        self.git("push", "-q", "origin", "HEAD:refs/heads/agent-control")
        self.sha = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "missing"):
            self.preview()

    def test_invalid_and_duplicate_json_is_rejected(self):
        f = self.work / ".agent/hosts/worker-linux/status.json"
        data = json.loads(f.read_text())
        f.write_text(json.dumps(data)[:-1] + ', "state": "busy"}\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.preview()
        data.pop("installation_id")
        f.write_text(json.dumps(data))
        self.commit()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.preview()

    def test_binding_from_pinned_git_object_is_checked(self):
        p = self.work / ".agent/binding.json"
        obj = json.loads(p.read_text())
        obj["agent_binding"] = INSTALLATION
        self.write(".agent/binding.json", obj)
        self.commit()
        with self.assertRaisesRegex(ValueError, "control binding"):
            self.preview()

    def test_remote_moves_during_inspection(self):
        original = reader._git
        seen = 0

        def move(cwd, *args, **kw):
            nonlocal seen
            if args[:2] == ("ls-remote", "--heads"):
                seen += 1
                if seen == 2:
                    self.git("commit", "-q", "--allow-empty", "-m", "race")
                    self.git("push", "-q", "origin", "HEAD:refs/heads/agent-control")
            return original(cwd, *args, **kw)

        with mock.patch.object(reader, "_git", side_effect=move):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.preview()
        self.assertEqual(seen, 2)

    def test_remote_head_not_cached_fails_without_fetch(self):
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", "-b", "agent-control", str(self.remote), str(other)], check=True)
        for key, value in (("user.name", "Other"), ("user.email", "other@example.invalid")):
            subprocess.run(["git", "-C", str(other), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "external"], check=True)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "HEAD:refs/heads/agent-control"], check=True)
        external = subprocess.check_output(["git", "-C", str(other), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaisesRegex(ValueError, "not cached"):
            self.preview(expected_control_sha=external)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rewritten_origin_precedes_network_probe(self):
        calls = []
        def hostile_git(cwd, *args, **kw):
            calls.append(args)
            if args[:3] == ("remote", "get-url", "origin"):
                return "https://attacker.invalid/fork.git\n"
            raise AssertionError("network or local Git inspection attempted after bad origin")

        with (mock.patch.object(reader, "_trusted_origin"),
              mock.patch.object(reader, "_git", side_effect=hostile_git)):
            with self.assertRaisesRegex(ValueError, "rewritten origin"):
                reader.preview_host_status_snapshot(self.work, host_id="worker-linux",
                    expected_control_sha=self.sha, expected_source_sha=SOURCE,
                    expected_operator_sha=OPERATOR, enrollment=self.enrolled,
                    target=self.target, now=self.now)
        self.assertEqual(calls, [("remote", "get-url", "origin")])

    def test_rewritten_origin_is_rejected_without_network(self):
        with self.assertRaisesRegex(ValueError, "origin"):
            reader.preview_host_status_snapshot(self.work, host_id="worker-linux",
                expected_control_sha=self.sha, expected_source_sha=SOURCE,
                expected_operator_sha=OPERATOR, enrollment=self.enrolled,
                target=self.target, now=self.now)

    def test_cli_success_and_failure_contain_no_private_data(self):
        args = diagnostics.build_parser().parse_args([
            "preview-host-status", "--host-id", "worker-linux", "--repository", "local-agent",
            "--expected-control-sha", self.sha, "--expected-operator-sha", OPERATOR,
            "--expected-source-sha", SOURCE, "--catalog", str(self.catalog),
        ])
        row = SimpleNamespace(repository_id="local-agent", repository="owner/local-agent",
                              agent_binding=BINDING, control=self.work)
        output = io.StringIO()
        with (mock.patch.object(diagnostics, "fetch_operator_host_registry", return_value=(OPERATOR, (self.enrolled,))),
              mock.patch.object(diagnostics, "load_repository_registry", return_value=[row]),
              mock.patch.object(diagnostics, "validate_repository_control_binding"),
              mock.patch.object(reader, "_validate_control_origin"),
              redirect_stdout(output)):
            # The record timestamp is intentionally static; a valid stale
            # status remains unknown, not an execution token.
            self.assertEqual(args.func(args), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["observed_state"], "unknown")
        self.assertEqual(report["execution_authority"], "not_asserted")
        with (mock.patch.object(diagnostics, "fetch_operator_host_registry",
                                side_effect=RuntimeError("private credential")),
              redirect_stdout(output := io.StringIO())):
            self.assertEqual(args.func(args), 2)
        self.assertNotIn("private credential", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
