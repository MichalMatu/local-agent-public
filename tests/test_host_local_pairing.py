"""Target-local owner GitHub pairing state: offline real-Git negative E2E.

Fixture owner confirmations are synthetic. This does not attest physical
presence, activate remote intake, update operator-control or run any tasks.
"""
from __future__ import annotations

import json
import stat
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from local_agent.cli import host_remote as cli
from local_agent.supervisor import host_local_pairing as local


class LocalPairingTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_host_pairing_plan import PrivateGithubPairingPlanTests
        fixture = PrivateGithubPairingPlanTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture
        self.state = fixture.root / "pairing-state"
        self.state.mkdir(mode=0o700)
        self.args = {**fixture.args, "state_dir": self.state}
        self.pair_confirm = local.pairing_confirmation(
            fixture.args["repository"], fixture.identity.host_id,
        )
        self.unpair_confirm = local.unpair_confirmation(
            fixture.args["repository"], fixture.identity.host_id,
        )

    def pair(self, **overrides):
        arguments = {**self.args, "owner_confirmation": self.pair_confirm, **overrides}
        with self.f.remote_fixture():
            return local.pair_local_github_host(**arguments)

    def unpair(self, **overrides):
        arguments = {
            "state_dir": self.state, "identity_file": self.f.identity_file,
            "expected_repository": self.f.args["repository"],
            "owner_confirmation": self.unpair_confirm, **overrides,
        }
        return local.unpair_local_github_host(**arguments)

    def record(self):
        return json.loads((self.state / local.PAIRING_RECORD).read_text())

    def test_one_local_pair_is_private_and_not_authority(self):
        first = self.pair()
        self.assertEqual(first["status"], "locally_paired_only")
        self.assertTrue(first["paired"])
        self.assertFalse(first["standing_execution_enabled"])
        self.assertFalse(first["github_writers_verified"])
        self.assertEqual(first["execution_authority"], "not_asserted")
        data = self.record()
        self.assertEqual(data["state"], "paired")
        self.assertFalse(data["standing_execution_enabled"])
        self.assertEqual(data["epoch"], 1)
        self.assertEqual(data["repository"], "exampleowner/paired-host")
        self.assertEqual(stat.S_IMODE((self.state / local.PAIRING_RECORD).stat().st_mode), 0o600)
        self.assertEqual(local.read_local_github_pairing(
            state_dir=self.state, identity_file=self.f.identity_file,
        )["execution_authority"], "not_asserted")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.pair()
        self.assertEqual(data, self.record())

    def test_local_unpair_tombstone_offline_and_idempotent(self):
        self.pair()
        with mock.patch.object(local, "plan_private_github_pairing",
                               side_effect=AssertionError("unpair must be offline")):
            result = self.unpair()
        self.assertEqual(result["status"], "locally_unpaired")
        self.assertFalse(result["paired"])
        self.assertEqual(result["epoch"], 2)
        self.assertEqual(self.record()["state"], "unpaired")
        self.assertEqual(self.unpair()["status"], "already_unpaired")
        self.assertEqual(self.record()["epoch"], 2)

    def test_explicit_repair_after_unpair_checks_new_git_and_monotonic_epoch(self):
        self.pair()
        self.unpair()
        result = self.pair()
        self.assertEqual(result["epoch"], 3)
        self.assertEqual(self.record()["state"], "paired")
        self.assertFalse(self.record()["standing_execution_enabled"])

    def test_wrong_confirmation_or_host_or_repo_leaves_state_absent(self):
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.pair(owner_confirmation="yes")
        with self.assertRaisesRegex(ValueError, "installation"):
            self.pair(
                expected_host_id="different-mac",
                owner_confirmation=local.pairing_confirmation(
                    self.f.args["repository"], "different-mac",
                ),
            )
        with self.assertRaises(ValueError):
            self.pair(
                repository="another/private",
                owner_confirmation=local.pairing_confirmation(
                    "another/private", self.f.identity.host_id,
                ),
            )
        self.assertFalse((self.state / local.PAIRING_RECORD).exists())

    def test_unpair_requires_same_host_and_repo_with_confirmation(self):
        self.pair()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.unpair(owner_confirmation="UNPAIR something else")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            local.unpair_local_github_host(
                state_dir=self.state, identity_file=self.f.identity_file,
                expected_repository="different/repository",
                owner_confirmation=local.unpair_confirmation(
                    "different/repository", self.f.identity.host_id,
                ),
            )
        self.assertEqual(self.record()["state"], "paired")
        with self.assertRaisesRegex(ValueError, "another installation|different|mismatch"):
            wrong = self.f.root / "different-id.json"
            wrong.write_text('{"version":1,"host_id":"other-host","installation_id":"b0e94f93-478f-4fc6-abed-159d3702362f"}')
            wrong.chmod(0o600)
            local.read_local_github_pairing(state_dir=self.state, identity_file=wrong)

    def test_symlink_untrusted_permissions_and_git_stored_state_denied(self):
        link = self.f.root / "link-state"
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory"):
            self.pair(state_dir=link)
        self.state.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                self.pair()
        finally:
            self.state.chmod(0o700)
        in_git = self.f.checkout / "private-state"
        in_git.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "outside Git"):
            self.pair(state_dir=in_git)
        self.assertFalse((self.state / local.PAIRING_RECORD).exists())

    def test_tampered_permissions_and_symlink_record_fail_closed(self):
        self.pair()
        path = self.state / local.PAIRING_RECORD
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            self.unpair()
        path.chmod(0o600)
        path.unlink()
        victim = self.f.root / "victim"
        victim.write_text("leave me\n")
        path.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.pair()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.unpair()
        self.assertEqual(victim.read_text(), "leave me\n")

    def test_modified_record_cannot_enable_standing_execution(self):
        self.pair()
        path = self.state / local.PAIRING_RECORD
        record = self.record()
        record["standing_execution_enabled"] = True
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "cannot enable standing"):
            self.unpair()
        with self.assertRaisesRegex(ValueError, "cannot enable standing"):
            local.read_local_github_pairing(
                state_dir=self.state, identity_file=self.f.identity_file,
            )

    def test_stale_remote_refuses_pair_and_does_not_write_state(self):
        with self.f.remote_fixture(sha="f" * 40), self.assertRaisesRegex(
            ValueError, "exact pinned",
        ):
            local.pair_local_github_host(
                owner_confirmation=self.pair_confirm, **self.args,
            )
        self.assertFalse((self.state / local.PAIRING_RECORD).exists())

    def test_pair_cli_rejects_non_interactive_confirmation(self):
        with redirect_stdout(StringIO()):
            code = cli.command_pair_local(Namespace(**self.args))
        self.assertEqual(code, 2)
        self.assertFalse((self.state / local.PAIRING_RECORD).exists())
        fake = SimpleNamespace(
            stdin=SimpleNamespace(isatty=lambda: False),
            stdout=SimpleNamespace(isatty=lambda: True),
        )
        with mock.patch.object(cli, "sys", fake):
            with self.assertRaisesRegex(ValueError, "interactive"):
                cli._interactive_local_confirmation(
                    self.pair_confirm,
                    repo=self.f.args["repository"], host=self.f.identity.host_id,
                )

    def test_pair_unpair_cli_and_status_fail_closed(self):
        with self.f.remote_fixture(), mock.patch.object(
            cli, "_interactive_local_confirmation", return_value=self.pair_confirm,
        ), redirect_stdout(StringIO()) as out:
            code = cli.command_pair_local(Namespace(**self.args))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["paired"])
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(cli.command_pair_status(Namespace(
                state_dir=self.state, identity_file=self.f.identity_file,
            )), 0)
        self.assertEqual(json.loads(out.getvalue())["execution_authority"], "not_asserted")
        with mock.patch.object(
            cli, "_interactive_local_confirmation", return_value=self.unpair_confirm,
        ), redirect_stdout(StringIO()) as out:
            code = cli.command_unpair_local(Namespace(
                state_dir=self.state, identity_file=self.f.identity_file,
                repository=self.f.args["repository"],
            ))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out.getvalue())["paired"])
        self.assertEqual(self.record()["state"], "unpaired")


if __name__ == "__main__":
    unittest.main()
