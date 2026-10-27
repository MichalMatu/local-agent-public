"""Read-only pinned operator host registry transport and diagnostic contracts."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.operator import remote


HOST_UUID = "550e8400-e29b-41d4-a716-446655440000"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
OPERATOR_SHA = "a" * 40


def _manifest() -> str:
    return json.dumps({"version": 1, "hosts": [
        {"version": 1, "host_id": "worker-mac", "installation_id": HOST_UUID,
         "enabled": True, "repositories": [
             {"repository_id": "local-agent", "agent_binding": BINDING},
         ]},
    ]})


class OperatorHostRegistryTransportTests(unittest.TestCase):
    def _mock_git(self, *, payload: str, fetched: str = OPERATOR_SHA, truncated: bool = False):
        calls = []
        results = iter([
            {"exit_code": 0, "output": "https://github.com/example-owner/local-agent.git"},
            {"exit_code": 0, "output": "https://github.com/example-owner/local-agent.git"},
            {"exit_code": 0, "output": OPERATOR_SHA + "\trefs/heads/operator-control"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": fetched},
            {"exit_code": 0, "output": payload, "output_truncated": truncated},
        ])

        def fake(args, repo, *, timeout=30):
            calls.append((args, repo, timeout))
            return next(results)

        return fake, calls

    def test_success_from_pinned_branch_and_no_mutating_commands(self):
        fake, calls = self._mock_git(payload=_manifest())
        with mock.patch.object(remote, "_git", side_effect=fake):
            ref, rows = remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
        self.assertEqual(ref, OPERATOR_SHA)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].identity.host_id, "worker-mac")
        self.assertEqual(rows[0].repositories, (("local-agent", BINDING),))
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0][0], ["config", "--get", "remote.origin.url"])
        self.assertEqual(calls[1][0], ["remote", "get-url", "origin"])
        self.assertEqual(calls[2][0][:2], ["ls-remote", "--heads"])
        self.assertEqual(calls[4][0], ["rev-parse", "refs/remotes/origin/operator-control"])
        self.assertEqual(calls[5][0], ["show", f"{OPERATOR_SHA}:.agent/operator/hosts.json"])
        self.assertEqual(calls[3][0][0], "fetch")
        self.assertFalse(any("push" in argv for argv, *_ in calls))
        self.assertFalse(any("state.json" in " ".join(argv) for argv, *_ in calls))

    def test_forged_or_rewritten_origin_is_never_an_enrollment_authority(self):
        for raw, resolved in (
            ("https://github.com/attacker/local-agent.git", "https://github.com/attacker/local-agent.git"),
            ("https://github.com/example-owner/local-agent.git", "https://github.com/attacker/local-agent.git"),
            ("file:///tmp/local-agent.git", "file:///tmp/local-agent.git"),
            ("https://github.com/example-owner/local-agent.git", "https://github.com/example-owner/local-agent.git\nINJECT"),
        ):
            calls = []
            responses = iter([raw, resolved])

            def fake(args, repo, *, timeout=30):
                calls.append(args)
                return {"exit_code": 0, "output": next(responses)}

            with self.subTest(raw=raw, resolved=resolved), mock.patch.object(remote, "_git", side_effect=fake):
                with self.assertRaisesRegex(ValueError, "untrusted or rewritten"):
                    remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
            self.assertTrue(all(argv[0] in {"config", "remote"} for argv in calls))

    def test_missing_or_truncated_remote_origin_is_untrusted(self):
        for response in ({"exit_code": 1, "output": ""},
                         {"exit_code": 0, "output": "https://github.com/example-owner/local-agent.git",
                          "output_truncated": True}):
            with mock.patch.object(remote, "_git", return_value=response) as git:
                with self.assertRaisesRegex(ValueError, "untrusted or rewritten"):
                    remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
                git.assert_called_once()

    def test_changed_ref_refuses_stale_registry(self):
        fake, calls = self._mock_git(payload=_manifest(), fetched="b" * 40)
        with mock.patch.object(remote, "_git", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "changed during fetch"):
                remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
        self.assertEqual(len(calls), 5)

    def test_missing_invalid_and_truncated_registry_fail_closed(self):
        for content in ("", "not-json", '{"version":1,"hosts":[],"hosts":[]}',
                        '{"version":1,"hosts":[],"unexpected":true}'):
            with self.subTest(content=content):
                fake, _ = self._mock_git(payload=content)
                with mock.patch.object(remote, "_git", side_effect=fake):
                    with self.assertRaises(ValueError):
                        remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
        fake, _ = self._mock_git(payload=_manifest(), truncated=True)
        with mock.patch.object(remote, "_git", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "truncated"):
                remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
        fake, _ = self._mock_git(payload=" " * (64 * 1024 + 1))
        with mock.patch.object(remote, "_git", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "size"):
                remote.fetch_operator_host_registry(Path("/trusted/local-agent"))

    def test_missing_host_registry_read_is_not_an_emergency_stop(self):
        results = iter([
            {"exit_code": 0, "output": "https://github.com/example-owner/local-agent.git"},
            {"exit_code": 0, "output": "https://github.com/example-owner/local-agent.git"},
            {"exit_code": 0, "output": OPERATOR_SHA + "\trefs/heads/operator-control"},
            {"exit_code": 0, "output": ""},
            {"exit_code": 0, "output": OPERATOR_SHA},
            {"exit_code": 1, "output": "not found"},
        ])
        with mock.patch.object(remote, "_git", side_effect=lambda *_a, **_k: next(results)), \
             mock.patch.object(remote.agent_operator, "disable_agent") as disabled:
            with self.assertRaises(ValueError):
                remote.fetch_operator_host_registry(Path("/trusted/local-agent"))
        disabled.assert_not_called()

    def test_cli_reports_read_only_snapshot_and_generic_unverified(self):
        args = diagnostics.build_parser().parse_args(["host-registry-preview"])
        fake, _ = self._mock_git(payload=_manifest())
        output = io.StringIO()
        with mock.patch.object(remote, "_git", side_effect=fake), \
             redirect_stdout(output):
            self.assertEqual(args.func(args), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["status"], "parsed_preview")
        self.assertEqual(data["execution_authority"], "not_asserted")
        self.assertEqual(data["source_sha"], OPERATOR_SHA)
        self.assertEqual(data["host_count"], 1)

        output = io.StringIO()
        with mock.patch.object(diagnostics, "fetch_operator_host_registry", side_effect=RuntimeError("private token")), \
             redirect_stdout(output):
            self.assertEqual(args.func(args), 2)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["status"], "unverified")
        self.assertNotIn("private token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
