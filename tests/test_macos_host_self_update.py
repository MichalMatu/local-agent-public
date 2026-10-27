"""Hermetic standalone pinned Mac updater tests (never invoke launchctl)."""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from scripts import macos_host_self_update as updater


class MacHostUpdateStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mac-update-stage-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.state = self.home / "Library/Application Support/local-agent"
        (self.state / "releases").mkdir(parents=True)
        (self.state / "releases").chmod(0o700)
        self.installed = self.home / "installed"
        self.original = plistlib.dumps({
            "Label": "com.michal.local-agent",
            "ProgramArguments": ["/usr/bin/python3"],
            "WorkingDirectory": str(self.installed),
        })
        self.base = "ab" * 20
        self.candidate = "cd" * 20
        self.intent = {
            "version": 1,
            "request_id": "deploy-01",
            "candidate_sha": self.candidate,
            "target_host_id": "macbook-one",
            "target_installation_id": "b0e94f93-478f-4fc6-abed-159d3702362f",
            "status": "pending_external_controller",
        }
        self.context = (self.state, self.intent, {
            "self_revision": self.base,
        }, self.installed, self.original)

    def test_stage_pins_both_sources_and_preserves_original_plist(self) -> None:
        def checkout(root: Path, name: str, _revision: str) -> None:
            (root / name).mkdir()

        def git(_path: Path, *arguments: str) -> str:
            return self.candidate if arguments == ("rev-parse", "HEAD") else ""

        def verified(root: Path, _candidate: Path, _python: str) -> None:
            updater._atomic(root / "verified-checks.json", {
                "version": 1, "candidate_sha": self.candidate,
                "checks": [],
            })

        with (
            mock.patch.object(updater, "_context", return_value=self.context),
            mock.patch.object(updater, "_clone", side_effect=checkout) as clone,
            mock.patch.object(updater, "_git", side_effect=git),
            mock.patch.object(updater, "_private_file", return_value=b"config"),
            mock.patch.object(updater, "_verify_candidate", side_effect=verified),
            mock.patch.object(updater, "rollout") as rollout,
        ):
            result = updater.stage(self.home, "deploy-01")
        root = self.state / "releases" / self.candidate
        self.assertEqual(result["status"], "verified_staged")
        self.assertEqual(clone.call_count, 2)
        self.assertEqual(json.loads(
            (root / "staging-manifest.json").read_text()
        )["baseline_sha"], self.base)
        self.assertEqual(
            (root / "configuration-backup/com.michal.local-agent.plist").read_bytes(),
            self.original,
        )
        rollout.prepare.assert_not_called()
        rollout.arm.assert_not_called()

    def test_sidecar_is_used_for_native_acceptance_and_manifest(self) -> None:
        def checkout(root: Path, name: str, _revision: str) -> None:
            (root / name).mkdir()

        def verified(root: Path, _candidate: Path, python: str) -> None:
            self.assertEqual(python, str(root / "candidate-runtime/bin/python3"))
            updater._atomic(root / "verified-checks.json", {
                "version": 1, "candidate_sha": self.candidate, "checks": [],
            })

        with (
            mock.patch.object(updater, "_context", return_value=self.context),
            mock.patch.object(updater, "_clone", side_effect=checkout),
            mock.patch.object(updater, "_private_file", return_value=b"config"),
            mock.patch.object(updater, "_git",
                              side_effect=lambda _path, *args: (
                                  self.candidate if args == ("rev-parse", "HEAD") else "")),
            mock.patch.object(updater, "_stage_sidecar_python",
                              side_effect=lambda root, _: str(root / "candidate-runtime/bin/python3")) as sidecar,
            mock.patch.object(updater, "_verify_candidate", side_effect=verified),
        ):
            self.assertEqual(
                updater.stage(self.home, "deploy-01", sidecar_python=True)["status"],
                "verified_staged",
            )
        self.assertEqual(sidecar.call_count, 1)
        root = self.state / "releases" / self.candidate
        self.assertEqual(
            json.loads((root / "staging-manifest.json").read_text())["candidate_python"],
            str(root / "candidate-runtime/bin/python3"),
        )

    def test_sidecar_installs_hash_locked_dependencies_without_inherited_packages(self) -> None:
        root = self.state / "releases" / self.candidate
        (root / "candidate-cli").mkdir(parents=True)
        lock = root / "candidate-cli/requirements-runtime.lock"
        lock.write_text("package==1.0 --hash=sha256:fixture\n")

        def run(command, **kwargs):
            if "venv" in command:
                self.assertNotIn("--system-site-packages", command)
                runtime = Path(command[-1])
                runtime.mkdir()
                (runtime / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
            elif "pip" in command:
                self.assertIn("--isolated", command)
                self.assertIn("--require-hashes", command)
                self.assertEqual(command[-2:], ["-r", str(lock)])
            return mock.Mock(returncode=0)

        with mock.patch.object(updater.subprocess, "run", side_effect=run) as spawn:
            updater._stage_sidecar_python(root, "/reviewed/python")
        self.assertEqual(spawn.call_count, 3)
        receipt = json.loads((root / "candidate-python.json").read_text())
        import hashlib
        self.assertEqual(receipt["requirements_lock_sha256"],
                         hashlib.sha256(lock.read_bytes()).hexdigest())
        self.assertNotIn("requirements", receipt)

    def test_repeat_stage_never_overwrites_existing_release(self) -> None:
        root = self.state / "releases" / self.candidate
        root.mkdir()
        sentinel = root / "valuable.txt"
        sentinel.write_text("keep this")
        with mock.patch.object(updater, "_context", return_value=self.context):
            with self.assertRaises(FileExistsError):
                updater.stage(self.home, "deploy-01")
        self.assertEqual(sentinel.read_text(), "keep this")

    def test_arm_requires_full_native_receipts(self) -> None:
        root = self.state / "releases" / self.candidate
        root.mkdir()
        def verified_command(_args: list[str], **kwargs) -> mock.Mock:
            kwargs["stdout"].write(b"native acceptance output\n")
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(updater, "_context", return_value=self.context),
            mock.patch.object(updater, "rollout") as rollout,
        ):
            with self.assertRaises(updater.HostReleaseBlocked):
                updater.arm(self.home, "deploy-01")
            with (
                mock.patch.object(updater.subprocess, "run", side_effect=verified_command),
                mock.patch.object(updater, "_git", return_value=self.candidate),
            ):
                updater._verify_candidate(root, root / "candidate-cli", "/private/python")
            rollout.prepare.return_value = {"state": "prepared"}
            rollout.arm.return_value = {"armed": True}
            result = updater.arm(self.home, "deploy-01")
        self.assertEqual(result["status"], "armed_external_watchdog")
        rollout._configure_pinned_release.assert_called_once_with(
            self.base, self.candidate,
        )
        rollout.prepare.assert_called_once_with(self.home)
        rollout.arm.assert_called_once_with(self.home)

    def test_readable_state_root_requires_owner_and_rejects_peer_writes(self) -> None:
        self.state.chmod(0o755)
        updater._check_directory(self.state, private=False)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(updater.HostReleaseBlocked):
            updater._check_directory(self.state)
        self.state.chmod(0o775)
        with self.assertRaises(updater.HostReleaseBlocked):
            updater._check_directory(self.state, private=False)

    def test_initial_external_watcher_ignores_absent_intent_directory(self) -> None:
        self.state.chmod(0o700)
        with mock.patch.object(updater, "_data") as data, \
             mock.patch.object(updater, "stage") as stage, \
             mock.patch.object(updater, "arm") as arm:
            result = updater._watch_intents(self.home)
        self.assertEqual(result, {"status": "no_pending_intents"})
        data.assert_not_called()
        stage.assert_not_called()
        arm.assert_not_called()

    def test_first_local_request_is_pinned_once_without_service_effect(self) -> None:
        self.state.chmod(0o700)

        def read(path: Path) -> dict:
            if path.name == "host-identity.json":
                return {
                    "host_id": self.intent["target_host_id"],
                    "installation_id": self.intent["target_installation_id"],
                }
            if path.name == "status.json":
                return {"self_revision": self.base}
            raise AssertionError(path)

        with (
            mock.patch.object(updater.sys, "platform", "darwin"),
            mock.patch.object(updater.os, "getuid", return_value=501),
            mock.patch.object(updater.os, "geteuid", return_value=501),
            mock.patch.object(updater, "_check_directory"),
            mock.patch.object(updater, "_data", side_effect=read),
            mock.patch.object(updater, "stage") as stage,
            mock.patch.object(updater, "arm") as arm,
        ):
            result = updater.enqueue_local(self.home, "deploy-01", self.candidate)
            with self.assertRaises(FileExistsError):
                updater.enqueue_local(self.home, "deploy-01", "ef" * 20)
        self.assertEqual(result["status"], "queued_external_controller")
        payload = json.loads(
            (self.state / updater.INTENTS / "deploy-01.json").read_text()
        )
        self.assertEqual(payload["candidate_sha"], self.candidate)
        self.assertEqual(payload["status"], "pending_external_controller")
        stage.assert_not_called()
        arm.assert_not_called()

    def test_two_incompatible_update_intents_fail_without_install(self) -> None:
        self.state.chmod(0o700)
        tickets = self.state / updater.INTENTS
        tickets.mkdir(mode=0o700)
        for ident, sha in (("first", self.candidate), ("second", "ef" * 20)):
            updater._atomic(tickets / f"{ident}.json", {
                **self.intent, "request_id": ident, "candidate_sha": sha,
            })
        with (
            mock.patch.object(
                updater, "_data",
                side_effect=lambda p: (
                    {"self_revision": self.base} if p.name == "status.json"
                    else json.loads(p.read_text())
                ),
            ),
            mock.patch.object(updater, "stage") as stage,
            mock.patch.object(updater, "arm") as arm,
        ):
            with self.assertRaises(updater.HostReleaseBlocked):
                updater._watch_intents(self.home)
        stage.assert_not_called()
        arm.assert_not_called()

    def test_subsequent_update_skips_verified_prior_install_without_replay(self) -> None:
        """Two sequential releases must not resurrect the first ticket."""
        self.state.chmod(0o700)
        tickets = self.state / updater.INTENTS
        tickets.mkdir(mode=0o700)
        old = {**self.intent, "request_id": "first", "candidate_sha": self.base}
        next_release = {**self.intent, "request_id": "second"}
        updater._atomic(tickets / "first.json", old)
        updater._atomic(tickets / "second.json", next_release)
        current = [self.base]
        intents = {"first": old, "second": next_release}

        def data(path: Path) -> dict:
            if path.name == "status.json":
                return {"self_revision": current[0]}
            return json.loads(path.read_text())

        def context(_home: Path, request_id: str) -> tuple:
            return (self.state, intents[request_id],
                    {"self_revision": current[0]}, self.installed, self.original)

        with (
            mock.patch.object(updater, "_data", side_effect=data),
            mock.patch.object(updater, "_context", side_effect=context),
            mock.patch("scripts.macos_host_service.remember_accepted"),
            mock.patch.object(updater, "stage") as stage,
            mock.patch.object(updater, "arm", return_value={"status": "armed_external_watchdog"}) as arm,
        ):
            self.assertEqual(
                updater._watch_intents(self.home)["status"],
                "armed_external_watchdog",
            )
            first_receipt = self.state / updater.COMPLETED / "first.json"
            self.assertEqual(json.loads(first_receipt.read_text())["candidate_sha"],
                             self.base)
            self.assertEqual(stage.call_count, 1)
            self.assertEqual(arm.call_count, 1)

            current[0] = self.candidate
            self.assertEqual(
                updater._watch_intents(self.home),
                {"status": "verified_update_history", "count": 2,
                 "running_revision": self.candidate},
            )
            self.assertEqual(stage.call_count, 1)
            self.assertEqual(arm.call_count, 1)
            self.assertEqual(
                json.loads((self.state / updater.COMPLETED / "second.json").read_text())["candidate_sha"],
                self.candidate,
            )

            # A corrupt prior receipt must fail closed, not stage a rollback.
            first_receipt.write_text('{"candidate_sha":"wrong"}')
            with self.assertRaises(updater.HostReleaseBlocked):
                updater._watch_intents(self.home)
            self.assertEqual(arm.call_count, 1)

    def test_failed_test_receipt_cannot_arm(self) -> None:
        root = self.state / "releases" / self.candidate
        root.mkdir()
        updater._atomic(root / "verified-checks.json", {
            "version": 1, "candidate_sha": self.candidate,
            "checks": [
                {"command": ["something else"], "exit_code": 0,
                 "log_sha256": "ef" * 20},
                {"command": ["-m", "unittest"], "exit_code": 1,
                 "log_sha256": "ef" * 20},
            ],
        })
        with (
            mock.patch.object(updater, "_context", return_value=self.context),
            mock.patch.object(updater, "rollout") as rollout,
        ):
            with self.assertRaises(updater.HostReleaseBlocked):
                updater.arm(self.home, "deploy-01")
        rollout.prepare.assert_not_called()
        rollout.arm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
