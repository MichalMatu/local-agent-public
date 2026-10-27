"""Owner-configured host GitHub repository, without real network or host effects."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.repository.host_routing import HostIdentity
from local_agent.supervisor.host_control import (
    CONTROL_REPOSITORY, HostControlSource, canonical_control_repository,
    load_host_control_source,
)
from local_agent.supervisor import host_private_producer as producer
from local_agent.supervisor import host_remote_worker
from local_agent.supervisor.host_private_producer import _validate_origin


class OwnerRepositoryTests(unittest.TestCase):
    def test_explicit_owner_repository_contract(self) -> None:
        self.assertEqual(canonical_control_repository("Owner/Host_Agent"), "owner/host_agent")
        for value in ("", "plain", "a/..", "a/.test", "-bad/host",
                      "a/b.git", "https://github.com/a/b", "a/b/extra"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_control_repository(value)

    def test_origin_must_match_owner_selected_repository(self) -> None:
        for value in ("https://github.com/Owner/Agent.git",
                      "git@github.com:Owner/Agent",
                      "ssh://git@github.com/Owner/Agent.git"):
            _validate_origin(value, "owner/agent")
        for value in ("https://github.com/other/agent", "file:///tmp/owner/agent",
                      "https://github.com/owner/agent/extra"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validate_origin(value, "owner/agent")

    def test_host_config_binds_private_repository_and_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-owner-repository-") as root:
            directory = Path(root)
            identity = HostIdentity("example-mac", "12345678-1234-4234-8234-123456789abc")
            identity_file = directory / "identity.json"
            identity_file.write_text(json.dumps(identity.as_record()))
            identity_file.chmod(0o600)
            config = directory / "supervisor-control.json"
            record = {
                "version": 1, "enabled": True,
                "host_id": identity.host_id,
                "identity_file": str(identity_file),
                "control_checkout": str(directory / "control"),
                "repository": "Owner/Private-Tasks",
            }
            config.write_text(json.dumps(record))
            config.chmod(0o600)
            host = load_host_control_source(config)
            self.assertEqual(host.repository, "owner/private-tasks")
            self.assertEqual(host.installation_id, identity.installation_id)
            record["repository"] = "../other"
            config.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "repository"):
                load_host_control_source(config)

    def test_integrated_host_intake_requires_one_local_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-integrated-host-") as root:
            directory = Path(root)
            identity = HostIdentity("example-mac", "12345678-1234-4234-8234-123456789abc")
            identity_file = directory / "identity.json"
            identity_file.write_text(json.dumps(identity.as_record()))
            identity_file.chmod(0o600)
            config = directory / "supervisor-control.json"
            record = {
                "version": 1, "enabled": True, "host_id": identity.host_id,
                "identity_file": str(identity_file),
                "control_checkout": str(directory / "control"),
                "repository": "owner/private-agent",
                "host_intake": {"enabled": True, "authorization": "github_writers"},
            }
            config.write_text(json.dumps(record))
            config.chmod(0o600)
            selected = load_host_control_source(config)
            self.assertIsNotNone(selected)
            intake = host_remote_worker.load_remote_intake_config(directory, selected)
            self.assertEqual(intake.authorization, "github_writers")
            self.assertEqual(intake.identity_file, identity_file)
            record["host_intake"]["enabled"] = False
            config.write_text(json.dumps(record))
            selected = load_host_control_source(config)
            self.assertIsNone(host_remote_worker.load_remote_intake_config(directory, selected))

    def test_legacy_intake_disabled_has_precedence_over_integrated_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-intake-precedence-") as root:
            directory = Path(root)
            identity = HostIdentity("example-mac", "12345678-1234-4234-8234-123456789abc")
            identity_file = directory / "identity.json"
            identity_file.write_text(json.dumps(identity.as_record()))
            identity_file.chmod(0o600)
            config = directory / "supervisor-control.json"
            config.write_text(json.dumps({
                "version": 1, "enabled": True, "host_id": identity.host_id,
                "identity_file": str(identity_file),
                "control_checkout": str(directory / "control"),
                "host_intake": {"enabled": True, "authorization": "github_writers"},
            }))
            config.chmod(0o600)
            legacy = directory / "host-task-intake.json"
            legacy.write_text(json.dumps({
                "version": 1, "enabled": False, "host_id": identity.host_id,
                "installation_id": identity.installation_id,
                "identity_file": str(identity_file),
            }))
            legacy.chmod(0o600)
            source = load_host_control_source(config)
            self.assertIsNone(host_remote_worker.load_remote_intake_config(directory, source))

    def test_github_writer_profile_needs_no_separate_operator_pin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="la-owner-producer-") as root:
            directory = Path(root)
            identity = HostIdentity("example-mac", "12345678-1234-4234-8234-123456789abc")
            identity_file = directory / "identity.json"
            identity_file.write_text(json.dumps(identity.as_record()))
            identity_file.chmod(0o600)
            checkout = directory / "control"
            checkout.mkdir()
            profile = directory / "producer-profile.json"
            record = {
                "version": 1, "host_id": identity.host_id,
                "installation_id": identity.installation_id,
                "identity_file": str(identity_file), "operator_checkout": "",
                "operator_sha": "github_writers",
                "control_checkout": str(checkout),
                "origin": "https://github.com/owner/private-agent",
                "repository": "owner/private-agent",
                "authorization": "github_writers",
            }
            profile.write_text(json.dumps(record))
            profile.chmod(0o600)
            with (
                mock.patch.object(producer, "_require_control_origin"),
                mock.patch.object(producer, "_git", return_value=record["origin"]),
                mock.patch.object(producer, "_read_authority",
                                  side_effect=AssertionError("legacy pin queried")) as legacy,
            ):
                selected = producer.load_private_host_producer(profile)
                self.assertEqual(selected.repository, "owner/private-agent")
                self.assertEqual(selected.host_id, identity.host_id)
                legacy.assert_not_called()
                record["operator_checkout"] = "/some/other/operator"
                profile.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "must not carry"):
                    producer.load_private_host_producer(profile)

    def test_old_config_and_source_default_remain_compatible(self) -> None:
        host = HostControlSource("example-mac", "12345678-1234-4234-8234-123456789abc",
                                 Path("/tmp/host"))
        self.assertEqual(host.repository, CONTROL_REPOSITORY)


if __name__ == "__main__":
    unittest.main()
