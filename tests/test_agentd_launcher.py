from __future__ import annotations

import unittest
from unittest import mock

from local_agent.daemon import launcher


class AgentDaemonLauncherTests(unittest.TestCase):
    def test_no_registry_fails_closed_without_entering_legacy_main(self) -> None:
        with mock.patch.object(
            launcher.service, "dispatch_multirepo_if_configured", return_value=False
        ), mock.patch.object(
            launcher.service, "multirepo_registry_path", return_value="/tmp/repositories.json"
        ), mock.patch.object(launcher.service, "main") as legacy_main, mock.patch.object(
            launcher.service, "log"
        ):
            with self.assertRaisesRegex(RuntimeError, "repository registry is required"):
                launcher.run()
        legacy_main.assert_not_called()

    def test_registry_dispatch_is_terminal(self) -> None:
        with mock.patch.object(
            launcher.service, "dispatch_multirepo_if_configured", return_value=True
        ), mock.patch.object(launcher.service, "log") as log:
            self.assertIsNone(launcher.run())
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
