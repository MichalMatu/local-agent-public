from __future__ import annotations

import unittest
from unittest import mock

import local_agent.daemon.service as agentd


class SelfUpdateEnvironmentTests(unittest.TestCase):
    def test_validation_strips_inherited_execution_lease_environment(self) -> None:
        environments: list[dict[str, str]] = []

        def fake_process(_command, _cwd, **kwargs):
            environments.append(dict(kwargs["environment"]))
            return {"exit_code": 0, "output": "", "elapsed_seconds": 0.1}

        lease_env = {
            agentd.LEASE_FDS_ENV: "4,5",
            agentd.LEASE_KEYS_DIGEST_ENV: "digest",
            agentd.RESOURCE_LEASE_FDS_ENV: "6,7",
        }
        with mock.patch.dict(agentd.core.ENV, lease_env, clear=False), mock.patch.object(
            agentd.core, "process", side_effect=fake_process
        ):
            valid, error = agentd._validate_installed_update()

        self.assertTrue(valid)
        self.assertEqual(error, "")
        self.assertEqual(len(environments), 2)
        for environment in environments:
            self.assertNotIn(agentd.LEASE_FDS_ENV, environment)
            self.assertNotIn(agentd.LEASE_KEYS_DIGEST_ENV, environment)
            self.assertNotIn(agentd.RESOURCE_LEASE_FDS_ENV, environment)


if __name__ == "__main__":
    unittest.main()
