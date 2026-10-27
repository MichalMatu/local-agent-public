"""Mac independent operator must not arm or mutate a live service via an Agent."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scripts import operator_mac_fastpath as operator
from scripts import macos_host_self_update as updater


class FastpathExternalOperatorTests(unittest.TestCase):
    def test_lease_environment_blocks_before_git_or_service(self):
        with (
            mock.patch.object(operator.sys, "platform", "darwin"),
            mock.patch.object(operator.os, "getuid", return_value=501),
            mock.patch.object(operator.os, "geteuid", return_value=501),
            mock.patch.object(operator.sys.stdin, "isatty", return_value=True),
            mock.patch.dict("os.environ", {"LOCAL_AGENT_LEASE_FDS": "7"}),
            mock.patch.object(operator, "_git") as git,
            mock.patch.object(operator.service, "observation") as obs,
        ):
            with self.assertRaisesRegex(updater.HostReleaseBlocked, "independent"):
                operator.deploy(Path("/private/user"), "a"*40, "fresh-001")
        git.assert_not_called()
        obs.assert_not_called()

    def test_invalid_target_or_request_refused_before_service(self):
        with (
            mock.patch.object(operator.sys, "platform", "darwin"),
            mock.patch.object(operator.os, "getuid", return_value=501),
            mock.patch.object(operator.os, "geteuid", return_value=501),
            mock.patch.object(operator.sys.stdin, "isatty", return_value=True),
            mock.patch.dict("os.environ", {"LOCAL_AGENT_LEASE_FDS": "",
                                           "LOCAL_AGENT_LEASE_KEYS_DIGEST": ""}),
            mock.patch.object(operator.service, "observation") as obs,
        ):
            for sha, request in [("main", "fresh-001"), ("a"*40, "../escape")]:
                with self.assertRaisesRegex(updater.HostReleaseBlocked, "identity"):
                    operator.deploy(Path("/private/user"), sha, request)
        obs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
