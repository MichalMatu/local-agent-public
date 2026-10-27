"""Exact-SHA external Mac release pins, without launchctl or service changes."""
from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from scripts import macos_cli_polling_rollout as controller


class PinnedMacReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        names = (
            "BASE_SHA", "CANDIDATE_SHA", "RELEASE_NAME", "CONTROLLER_LABEL",
            "_DYNAMIC_PIN",
        )
        values = {name: getattr(controller, name) for name in names}
        self.addCleanup(
            lambda: [setattr(controller, name, value) for name, value in values.items()]
        )

    def test_rejects_invalid_nonimmutable_release_pins(self) -> None:
        base, candidate = "ab" * 20, "cd" * 20
        for lhs, rhs in (
            (base, base), ("abc", candidate), (base, "../" + candidate),
            (base.upper(), candidate), ("", candidate),
        ):
            with self.subTest(lhs=lhs, rhs=rhs):
                with self.assertRaises(controller.CutoverBlocked):
                    controller._configure_pinned_release(lhs, rhs)

    def test_explicit_pins_survive_watch_argv(self) -> None:
        base, target = "ab" * 20, "cd" * 20
        controller._configure_pinned_release(base, target)
        self.assertEqual(controller.BASE_SHA, base)
        self.assertEqual(controller.CANDIDATE_SHA, target)
        self.assertTrue(controller._DYNAMIC_PIN)
        self.assertEqual(
            controller.RELEASE_NAME,
            "Library/Application Support/local-agent/releases/" + target,
        )
        self.assertEqual(
            controller._pin_arguments(),
            ["--base-sha", base, "--candidate-sha", target],
        )
        self.assertEqual(
            controller.CONTROLLER_LABEL, "dev.localagent.cli-rollout." + target[:12],
        )

    def test_watch_plist_must_include_same_exact_pins(self) -> None:
        base, target = "ab" * 20, "cd" * 20
        controller._configure_pinned_release(base, target)
        with tempfile.TemporaryDirectory() as dirname:
            home = Path(dirname)
            root = home / controller.RELEASE_NAME
            root.mkdir(parents=True)
            script = root / "mvp-controller.py"
            script.write_text("pinned independent controller\n", encoding="utf-8")
            from hashlib import sha256
            entry = {
                "Label": controller.CONTROLLER_LABEL,
                "ProgramArguments": [
                    controller.sys.executable, "-B", str(script),
                    "watch", "--home", str(home), *controller._pin_arguments(),
                ],
                "WorkingDirectory": str(root),
                "RunAtLoad": True,
                "KeepAlive": False,
                "StartInterval": 20,
            }
            job = root / "mvp-watch.plist"
            job.write_bytes(plistlib.dumps(entry))
            record = {
                "controller_sha256": sha256(script.read_bytes()).hexdigest(),
            }
            parsed = plistlib.loads(
                controller._watch_job_bytes(home, root, record)
            )
            self.assertEqual(parsed["ProgramArguments"][-4:], [
                "--base-sha", base, "--candidate-sha", target,
            ])
            entry["ProgramArguments"][-1] = "ef" * 20
            job.write_bytes(plistlib.dumps(entry))
            with self.assertRaises(controller.CutoverBlocked):
                controller._watch_job_bytes(home, root, record)

    def test_cli_passes_exact_pins_only_for_requested_action(self) -> None:
        base, target = "ab" * 20, "cd" * 20
        with mock.patch.object(controller, "prepare", return_value={"state": "prepared"}) as prepare:
            with mock.patch("builtins.print"):
                self.assertEqual(controller.main([
                    "prepare", "--home", "/private/test",
                    "--base-sha", base, "--candidate-sha", target,
                ]), 0)
        prepare.assert_called_once()
        self.assertEqual(controller._pin_arguments()[-1], target)
        with self.assertRaises(controller.CutoverBlocked):
            controller.main(["watch", "--home", "/private/test", "--candidate-sha", target])


if __name__ == "__main__":
    unittest.main()
