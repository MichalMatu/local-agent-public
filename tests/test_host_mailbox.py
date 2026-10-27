from __future__ import annotations

import copy
import io
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock
import json
import tempfile
import unittest
from pathlib import Path

from local_agent.daemon.service import load_task_file
from local_agent.cli import diagnostics
from local_agent.repository.host_mailbox import prepare_host_mailbox_task, write_host_mailbox_bundle
from local_agent.repository.host_routing import HostEnrollment, validate_host_enrollments

BINDING = 'b0e94f93-478f-4fc6-abed-159d3702362f'
INSTALLATION = '12345678-1234-4234-8234-123456789abc'
OTHER_INSTALLATION = '12345678-1234-4234-8234-123456789abd'


class HostMailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.catalog = self.root / 'catalog.json'
        self.catalog.write_text(json.dumps({
            'version': 1,
            'agents': [{'id': 'local-agent', 'repository': 'owner/local-agent',
                        'agent_binding': BINDING, 'execution_enabled': True}],
        }))
        self.enrollments = validate_host_enrollments([{
            'version': 1, 'host_id': 'worker-linux', 'installation_id': INSTALLATION,
            'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        }])
        self.draft = {'id': 'test-targeted-001', 'mode': 'commands',
                      'commands': ['printf hello'], 'command_timeout': 30,
                      'task_timeout': 120}

    def prepare(self, **kwargs):
        return prepare_host_mailbox_task(
            kwargs.pop('draft', self.draft),
            host_id=kwargs.pop('host_id', 'worker-linux'),
            repository=kwargs.pop('repository', 'local-agent'),
            enrollments=kwargs.pop('enrollments', self.enrollments),
            catalog_path=self.catalog, **kwargs,
        )

    def test_prepare_is_non_mutating_and_bound_to_one_host_and_repository(self) -> None:
        original = copy.deepcopy(self.draft)
        task, enrolled, target = self.prepare()
        self.assertEqual(self.draft, original)
        self.assertEqual(task['agent_binding'], BINDING)
        self.assertEqual(task['host_mailbox_version'], 1)
        self.assertEqual(task['target_host_id'], 'worker-linux')
        self.assertEqual(task['target_installation_id'], INSTALLATION)
        self.assertEqual(task['target_repository_id'], 'local-agent')
        self.assertEqual(enrolled.identity.host_id, 'worker-linux')
        self.assertEqual(target.repository_id, 'local-agent')

    def test_writer_preserves_exact_payload_and_refuses_duplicates(self) -> None:
        draft = self.draft | {'commands': ['printf hello\n' * 20]}
        task, enrollment, target = self.prepare(draft=draft)
        root = self.root / 'publication'
        manifest = write_host_mailbox_bundle(root, task, enrollment=enrollment, target=target)
        self.assertEqual(manifest, root / '.agent/hosts/worker-linux/tasks/test-targeted-001.json')
        self.assertEqual(load_task_file(manifest), task)
        self.assertTrue((manifest.parent / 'test-targeted-001.payload/commands/001.sh').exists())
        with self.assertRaises(FileExistsError):
            write_host_mailbox_bundle(root, task, enrollment=enrollment, target=target)

    def test_disabled_wrong_host_or_binding_never_creates_mailbox(self) -> None:
        disabled = (HostEnrollment.from_operator_record({
            'version': 1, 'host_id': 'worker-linux', 'installation_id': INSTALLATION,
            'enabled': False, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        }),)
        for changes in ({'host_id': 'worker-mac'}, {'enrollments': ()}, {'enrollments': disabled}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(**changes)
        for field, value in (
            ('target_host_id', 'worker-mac'),
            ('target_installation_id', OTHER_INSTALLATION),
            ('target_repository_id', 'another'),
            ('host_mailbox_version', 9),
            ('agent_binding', OTHER_INSTALLATION),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare(draft=self.draft | {field: value})
        self.assertFalse((self.root / 'publication').exists())

    def test_staging_protects_active_dirs_and_symlink_paths(self) -> None:
        task, enrollment, target = self.prepare()
        active = self.root / 'active/control'
        active.mkdir(parents=True)
        for output in (active, active / 'nested', self.root / 'active'):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'overlap'):
                write_host_mailbox_bundle(output, task, enrollment=enrollment,
                                          target=target, protected_paths=(active,))
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            write_host_mailbox_bundle(alias, task, enrollment=enrollment, target=target)
        git_checkout = self.root / 'git-checkout'
        (git_checkout / '.git').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Git working tree'):
            write_host_mailbox_bundle(git_checkout / 'out', task, enrollment=enrollment, target=target)
        mailbox = self.root / 'pub/.agent/hosts'
        mailbox.mkdir(parents=True)
        (mailbox / 'worker-linux').symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            write_host_mailbox_bundle(self.root / 'pub', task, enrollment=enrollment, target=target)

    def test_cli_is_offline_and_requires_exact_operator_snapshot(self) -> None:
        draft = self.root / 'task.json'
        draft.write_text(json.dumps(self.draft))
        registry = self.root / 'registry.json'
        registry.write_text(json.dumps({'version': 1, 'repositories': [{'id': 'demo', 'repository': 'owner/demo', 'agent_binding': OTHER_INSTALLATION, 'control_dir': str(self.root / 'active/control'), 'work_dir': str(self.root / 'active/work'), 'checkpoints_dir': str(self.root / 'active/checkpoints') }]}))
        args = Namespace(
            path=str(draft), repository='local-agent', host_id='worker-linux',
            expected_operator_sha='a' * 40, output_root=self.root / 'pub',
            profile='repository', registry=registry, catalog=self.catalog,
        )
        result = io.StringIO()
        with (mock.patch.object(diagnostics, 'fetch_operator_host_registry', return_value=('a' * 40, self.enrollments)),
              mock.patch.object(diagnostics, 'is_disabled', return_value=False),
              redirect_stdout(result)):
            self.assertEqual(diagnostics.command_prepare_host_mailbox(args), 0)
        output = json.loads(result.getvalue())
        self.assertEqual(output['status'], 'staged_only')
        self.assertFalse(output['published'])
        self.assertEqual(load_task_file(Path(output['manifest']))['target_host_id'], 'worker-linux')

        args.output_root = self.root / 'reject'
        args.expected_operator_sha = 'b' * 40
        with (mock.patch.object(diagnostics, 'fetch_operator_host_registry', return_value=('a' * 40, self.enrollments)),
              mock.patch.object(diagnostics, 'is_disabled', return_value=False),
              redirect_stdout(io.StringIO())):
            self.assertEqual(diagnostics.command_prepare_host_mailbox(args), 2)
        self.assertFalse(args.output_root.exists())

        args.expected_operator_sha = 'a' * 40
        with (mock.patch.object(diagnostics, 'fetch_operator_host_registry', return_value=('a' * 40, self.enrollments)),
              mock.patch.object(diagnostics, 'is_disabled', return_value=True),
              redirect_stdout(io.StringIO())):
            self.assertEqual(diagnostics.command_prepare_host_mailbox(args), 2)
        self.assertFalse(args.output_root.exists())

    def test_writer_rechecks_enrollment_and_never_accepts_legacy_task(self) -> None:
        task, enrollment, target = self.prepare()
        for bad in (task | {'target_host_id': 'wrong'}, task | {'host_mailbox_version': 2},
                    {key: value for key, value in task.items() if key != 'host_mailbox_version'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                write_host_mailbox_bundle(self.root / 'pub', bad, enrollment=enrollment, target=target)
        self.assertFalse((self.root / 'pub').exists())


if __name__ == '__main__':
    unittest.main()
