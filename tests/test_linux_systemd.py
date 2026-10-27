from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.platform.linux_systemd import (
    check_installation, render_journal_config, render_service,
)
from local_agent.repository.context import load_repository_registry


class LinuxSystemdTests(unittest.TestCase):
    def setUp(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch("local_agent.daemon.service.STATE_DIR", state))

    def options(self) -> dict:
        return {
            "repo_root": Path('/private/source with spaces/%n'),
            "python": Path('/private/venv/bin/python'),
            "registry": Path('/private/operator/$registry.json'),
            "user": "worker", "host_id": "pi-zero2w",
        }

    def test_service_preserves_worker_drain_and_bounds_its_process_tree(self) -> None:
        text = render_service(**self.options())
        self.assertIn('KillMode=mixed\n', text)
        self.assertIn('TimeoutStopSec=360\n', text)
        self.assertIn('Restart=on-failure\n', text)
        self.assertIn('SuccessExitStatus=130\n', text)
        self.assertIn('MemoryMax=128M\n', text)
        self.assertIn('LOCAL_AGENT_MEMORY_LIMIT_MB_MIN=1\n', text)
        self.assertIn('LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT=32\n', text)
        self.assertIn('LOCAL_AGENT_MEMORY_LIMIT_MB_MAX=64\n', text)
        self.assertIn('LogNamespace=local-agent\n', text)
        self.assertIn('-m local_agent.supervisor.serial --registry', text)
        self.assertNotIn('--once', text)

    def test_unit_values_cannot_expand_specifiers_environment_or_inject_directives(self) -> None:
        text = render_service(**self.options())
        self.assertIn('WorkingDirectory=/private/source with spaces/%%n\n', text)
        self.assertIn('--registry "/private/operator/$$registry.json"', text)
        options = self.options()
        for value in ('worker\nExecStart=/bin/false', 'root'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_service(**{**options, 'user': value})
        with self.assertRaises(ValueError):
            render_service(**{**options, 'registry': Path('/private/name\n[Service]')})
        for value in ('/private/trailing ', '/private/back\\slash'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                render_service(**{**options, 'repo_root': Path(value)})

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('systemd-analyze'), 'requires Linux systemd tools')
    def test_generated_service_passes_native_unit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            unit = Path(folder) / 'local-agent.service'
            unit.write_text(render_service(
                **{**self.options(), 'python': Path(sys.executable)},
            ))
            result = subprocess.run(
                ['systemd-analyze', 'verify', str(unit)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_stop_or_memory_configuration_is_rejected(self) -> None:
        for key, values in [('stop_grace_seconds', (0, 3601, True)), ('memory_max_mb', (1, 16385, True))]:
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    render_service(**{**self.options(), key: value})

    def test_missing_explicit_registry_never_selects_a_legacy_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'explicit repository registry'):
                load_repository_registry(path=Path(folder) / 'missing.json')

    def test_service_preflight_rejects_wrong_host_and_missing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            for name in ['agent_multirepo.py', 'local_agent/supervisor/serial.py', 'local_agent/supervisor/worker.py']:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
            registry = root / 'registry.json'
            row = {
                'id': 'testrepo', 'repository': 'example/project',
                'agent_binding': 'b0e94f93-478f-4fc6-abed-159d3702362f',
                'control_dir': str(root / 'control'), 'work_dir': str(root / 'work'),
                'checkpoints_dir': str(root / 'checkpoints'), 'host_id': 'pi-zero2w',
                'host_mailbox_enabled': True, 'host_execution_enabled': True,
                'host_identity_file': str(root / 'identity.json'),
            }
            registry.write_text(json.dumps({'version': 1, 'repositories': [row]}))
            report = check_installation(repo_root=source, registry=registry, host_id='pi-zero2w')
            self.assertEqual(report['scope'], 'service_configuration')
            self.assertEqual(report['execution_authority'], 'not_asserted')
            with self.assertRaises(ValueError):
                check_installation(repo_root=source, registry=registry, host_id='another-host')
            registry.unlink()
            with self.assertRaises(ValueError):
                check_installation(repo_root=source, registry=registry, host_id='pi-zero2w')

    def test_zero_project_preflight_requires_exact_private_host_control(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            for name in ("agent_multirepo.py", "local_agent/supervisor/serial.py",
                         "local_agent/supervisor/worker.py"):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("")
            registry = root / "empty-registry.json"
            registry.write_text(json.dumps({"version": 1, "repositories": []}))
            state = root / "state"
            state.mkdir(mode=0o700)
            config = state / "supervisor-control.json"
            identity = state / "identity.json"
            identity.write_text(json.dumps({
                "version": 1, "host_id": "pi-zero2w",
                "installation_id": "7d114aff-9d1a-4df1-849b-ae4a99b89db0",
            }))
            identity.chmod(0o600)
            control = state / "control-checkout"
            payload = {
                "version": 1, "enabled": True, "host_id": "pi-zero2w",
                "identity_file": str(identity), "control_checkout": str(control),
            }
            with mock.patch("local_agent.daemon.service.STATE_DIR", state):
                with self.assertRaises(ValueError):
                    check_installation(repo_root=source, registry=registry, host_id="pi-zero2w")
                config.write_text(json.dumps(payload))
                config.chmod(0o600)
                report = check_installation(
                    repo_root=source, registry=registry, host_id="pi-zero2w",
                )
                self.assertEqual(report["repository_count"], 0)
                self.assertEqual(report["execution_authority"], "not_asserted")
                with self.assertRaisesRegex(ValueError, "different host"):
                    check_installation(
                        repo_root=source, registry=registry, host_id="another-host",
                    )
                payload["enabled"] = False
                config.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    check_installation(repo_root=source, registry=registry, host_id="pi-zero2w")

    def test_journal_has_separate_disk_and_memory_limits(self) -> None:
        text = render_journal_config()
        self.assertIn('SystemMaxUse=16M', text)
        self.assertIn('RuntimeMaxUse=8M', text)
        self.assertIn('MaxRetentionSec=7day', text)


if __name__ == '__main__':
    unittest.main()
