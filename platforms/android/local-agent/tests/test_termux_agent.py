"""Offline, synthetic filesystem/Git conformance; no Android hardware used."""
from __future__ import annotations

# ruff: noqa: E402 -- local fixture import setup

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / 'linux' / 'local-agent'))
from package import package, FILES
from termux_agent.agent import TermuxAgent, suspend_gap, validate_private_path, validate_termux_environment
from termux_agent.runtime import shell_path
from termux_agent.diagnostics import diagnose, _bundle_state, _runit_state
from linux_agent.git_transport import GitError


def git(*args, cwd=None):
    return subprocess.run(['git', *(['-C', str(cwd)] if cwd else []), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class TermuxSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.files = self.root / 'com.termux' / 'files'
        self.prefix, self.home = self.files / 'usr', self.files / 'home'
        (self.prefix / 'bin').mkdir(parents=True)
        self.home.mkdir()
        (self.prefix / 'bin' / 'sh').symlink_to('/bin/sh')
        self.env = {'PREFIX': str(self.prefix), 'HOME': str(self.home)}

    def test_path_preflight_rejects_external_storage_and_other_uid(self):
        prefix, home = validate_termux_environment(self.env, require_device=False)
        self.assertEqual((prefix, home), (self.prefix, self.home))
        validate_private_path(self.home / 'private' / 'state', home)
        with self.assertRaises(ValueError):
            validate_private_path(self.root / 'sdcard' / 'state', home)
        with self.assertRaises(ValueError):
            validate_private_path(home, home)
        with self.assertRaises(ValueError):
            validate_termux_environment(self.env, require_device=True)
        with self.assertRaises(ValueError):
            validate_termux_environment({'HOME': str(home), 'PREFIX': '/tmp/usr'}, require_device=False)

    def test_shell_and_suspend_detection(self):
        self.assertEqual(shell_path(self.prefix), self.prefix / 'bin' / 'sh')
        self.assertFalse(suspend_gap(1, 1, 2, 2))
        self.assertTrue(suspend_gap(1, 1, 40, 2))
        self.assertTrue(suspend_gap(1, 1, 2, 40))
        self.assertTrue(suspend_gap(40, 1, 2, 2))

    def test_bundle_only_command_runtime_with_digests(self):
        output = self.root / 'stage'
        manifest = package(output, 'a' * 40)
        self.assertTrue(all((output / 'linux_agent' / p).is_file() for p in FILES))
        self.assertFalse((output / 'linux_agent' / 'service_upgrade.py').exists())
        self.assertEqual(json.loads((output / 'manifest.json').read_text()), manifest)
        with self.assertRaises(ValueError):
            package(output, 'a' * 40)


class TermuxWireTests(TermuxSourceTests):
    def setUp(self):
        super().setUp()
        self.bare = self.root / 'remote.git'
        self.writer = self.root / 'writer'
        git('init', '--bare', str(self.bare))
        git('clone', str(self.bare), str(self.writer))
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@local.invalid',
            'commit', '--allow-empty', '-m', 'init', cwd=self.writer)
        git('branch', '-M', 'agent-control', cwd=self.writer)
        git('push', '-u', 'origin', 'agent-control', cwd=self.writer)
        self.cfg = {'version': 1, 'host_id': 'android-test-phone',
                    'installation_id': 'bbbbbbbb-2222-4333-8444-555555555555',
                    'repository': str(self.bare), 'branch': 'agent-control',
                    'enabled': True, 'host_intake': {
                        'enabled': True, 'authorization': 'github_writers'}}
        self.state = self.home / 'state'
        self.agent = TermuxAgent(self.cfg, self.state, environ=self.env, require_device=False)

    def test_dangling_stop_symlink_blocks_termux_git_task_without_claim(self):
        marker = self.home / 'forbidden.txt'
        self.send(commands=[f'touch {marker}'])
        self.state.mkdir(mode=0o700)
        (self.state / 'EMERGENCY_STOP').symlink_to(self.home / 'missing-stop-target')
        self.assertEqual(self.agent.once()['status'], 'disabled')
        self.assertFalse(marker.exists())
        self.assertFalse(self.agent._claim_path('termux-canary').exists())

    def send(self, task_id='termux-canary', commands=None):
        relative = (f".agent/hosts/{self.cfg['host_id']}/supervisor/"
                    f"{self.cfg['installation_id']}/tasks/{task_id}.json")
        task = {'version': 1, 'id': task_id,
                'target_host_id': self.cfg['host_id'],
                'target_installation_id': self.cfg['installation_id'],
                'cwd': str(self.home), 'commands': commands or ['printf yes'],
                'resources': [], 'command_timeout': 3, 'idle_timeout': 3,
                'task_timeout': 65, 'memory_limit_mb': 256,
                'output_policy': 'summary'}
        path = self.writer / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task))
        git('add', relative, cwd=self.writer)
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@local.invalid',
            'commit', '-m', 'enqueue', cwd=self.writer)
        git('push', cwd=self.writer)

    def test_isolated_git_roundtrip_and_no_replay(self):
        marker = self.home / 'marker'
        self.send(commands=[f"printf x >> '{marker}'"])
        self.assertEqual(self.agent.once()['status'], 'done')
        self.assertEqual(self.agent.once()['status'], 'idle')
        self.assertEqual(marker.read_text(), 'x')
        self.assertTrue((self.state / 'transport' / 'OWNED_BY_TERMUX_AGENT.json').exists())
        git('pull', '--ff-only', cwd=self.writer)
        result_path = (self.writer / '.agent' / 'hosts' / self.cfg['host_id'] /
                       'supervisor' / self.cfg['installation_id'] / 'task-results' /
                       'termux-canary.json')
        self.assertEqual(json.loads(result_path.read_text())['status'], 'done')

    def test_stop_between_stages_fences_termux_command_before_spawn(self):
        """A STOP watcher observation prevents the next Termux shell effect."""
        import threading
        from unittest import mock
        from linux_agent.runtime.executor import RuntimeExecutor

        marker = self.home / 'termux-stop-must-not-execute'
        self.send(commands=[f"touch '{marker}'"])
        path = self.writer / (
            f".agent/hosts/{self.cfg['host_id']}/supervisor/"
            f"{self.cfg['installation_id']}/tasks/termux-canary.json"
        )
        task = json.loads(path.read_text())
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        observed = threading.Event()
        real_process_task = RuntimeExecutor.process_task

        def after_stop(executor, pending):
            (self.state / 'EMERGENCY_STOP').write_text('disabled\\n')
            self.assertTrue(observed.wait(3), 'Termux STOP watcher did not run')
            return real_process_task(executor, pending)

        with (
            mock.patch.object(
                RuntimeExecutor, 'terminate_active_command', autospec=True,
                side_effect=lambda _executor: observed.set(),
            ),
            mock.patch.object(
                RuntimeExecutor, 'process_task', autospec=True,
                side_effect=after_stop,
            ),
        ):
            result = self.agent._execute(task)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(marker.exists(), 'STOP must fence Termux shell spawn')
        self.assertTrue(result['commands'][0].get('not_started'))

    def test_overlong_git_task_filename_does_not_poison_termux_inbox(self):
        """Shared Git intake must ignore a name unsafe for durable claims."""
        invalid_id = 'a' * 210
        self.send(task_id=invalid_id, commands=['printf ignored'])
        marker = self.home / 'valid-after-malformed-termux'
        git('pull', '--ff-only', cwd=self.writer)
        self.send(task_id='z-valid-after-malformed', commands=[
            f"printf x >> '{marker}'"
        ])
        self.assertEqual(self.agent.once()['status'], 'done')
        self.assertEqual(marker.read_text(), 'x')
        self.assertFalse(self.agent._claim_path(invalid_id).exists())
        self.assertEqual(TermuxAgent(self.cfg, self.state, environ=self.env,
                                    require_device=False).once()['status'], 'idle')
        self.assertEqual(marker.read_text(), 'x')

    def test_pre_effect_claim_is_unknown_and_never_replayed(self):
        marker = self.home / 'marker'
        self.send(commands=[f"printf x >> '{marker}'"])
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.agent.claim('termux-canary', 'a' * 64)
        self.assertEqual(self.agent.once()['status'], 'idle')
        self.assertFalse(marker.exists())
        git('pull', '--ff-only', cwd=self.writer)
        result = self.writer / '.agent' / 'hosts' / self.cfg['host_id'] / 'supervisor' / self.cfg['installation_id'] / 'task-results' / 'termux-canary.json'
        self.assertEqual(json.loads(result.read_text())['status'], 'unknown')

    def test_git_publication_outage_preserves_spool_and_no_replay(self):
        marker = self.home / 'effect'
        self.send(commands=[f"printf x >> '{marker}'"])
        with patch.object(self.agent.transport, 'publish', side_effect=GitError('offline')):
            with self.assertRaises(GitError):
                self.agent.once()
        self.assertEqual(marker.read_text(), 'x')
        original = (self.state / 'results' / 'termux-canary.json').read_bytes()
        self.assertEqual(self.agent.once()['status'], 'idle')
        self.assertEqual(marker.read_text(), 'x')
        self.assertEqual((self.state / 'results' / 'termux-canary.json').read_bytes(), original)
        git('pull', '--ff-only', cwd=self.writer)
        remote = (self.writer / '.agent' / 'hosts' / self.cfg['host_id'] /
                  'supervisor' / self.cfg['installation_id'] / 'task-results' /
                  'termux-canary.json')
        self.assertEqual(remote.read_bytes(), original)

    def test_corrupt_pre_effect_claim_fails_closed(self):
        marker = self.home / 'forbidden'
        self.send(commands=[f"touch '{marker}'"])
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.agent.claim('termux-canary', 'a' * 64)
        (self.state / 'claims' / 'termux-canary.json').write_text('{"status":"done"}')
        with self.assertRaises(ValueError):
            self.agent.once()
        self.assertFalse(marker.exists())

    def test_unsupported_mac_mutation_rejected_without_side_effect(self):
        marker = self.home / 'forbidden'
        self.send(commands=[f"touch '{marker}'"])
        rel = (f".agent/hosts/{self.cfg['host_id']}/supervisor/"
               f"{self.cfg['installation_id']}/tasks/termux-canary.json")
        path = self.writer / rel
        task = json.loads(path.read_text())
        task['writes'] = [{'path': 'unapproved', 'content': 'x'}]
        path.write_text(json.dumps(task))
        git('add', rel, cwd=self.writer)
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@local.invalid',
            'commit', '-m', 'unsupported mutation', cwd=self.writer)
        git('push', cwd=self.writer)
        self.assertEqual(self.agent.once()['reason'], 'invalid_task_file')
        self.assertFalse(marker.exists())

    def test_stop_fences_new_command(self):
        self.send(commands=[f"touch '{self.home / 'forbidden'}'"])
        self.state.mkdir(mode=0o700, exist_ok=True)
        (self.state / 'EMERGENCY_STOP').write_text('disabled\n')
        self.assertEqual(self.agent.once()['status'], 'disabled')
        self.assertFalse((self.home / 'forbidden').exists())


    def test_diagnose_enabled_does_not_claim_unverified_service_is_running(self):
        # Source fixtures have no installed private runit or immutable manifest.
        # The existing 'enabled' intake setting is never a liveness receipt.
        with patch.object(self.agent.transport, 'refresh', side_effect=AssertionError('network call')):
            with patch.object(self.agent.transport, 'publish', side_effect=AssertionError('Git write')):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['intake'], 'enabled')
        self.assertEqual(snapshot['service'], 'not_configured')
        self.assertEqual(snapshot['status'], 'unverified')
        self.assertEqual(snapshot['remote_result_publication'], 'not_checked')
        self.assertEqual((self.state / 'claims').exists(), False)

    def test_diagnose_cli_is_on_demand_json_and_does_not_contact_git(self):
        import io
        from contextlib import redirect_stdout
        from termux_agent import __main__ as cli

        buffer = io.StringIO()
        with patch.object(cli, 'validate_termux_environment',
                          return_value=(self.prefix, self.home)):
            with patch.object(cli, 'config_from_file', return_value=self.cfg):
                with patch.object(cli, 'TermuxAgent', return_value=self.agent):
                    with patch.object(self.agent.transport, 'refresh',
                                      side_effect=AssertionError('no Git fetch')):
                        with patch.object(self.agent.transport, 'publish',
                                          side_effect=AssertionError('no Git publish')):
                            with redirect_stdout(buffer):
                                self.assertEqual(cli.main([
                                    '--config', str(self.home / 'config.json'),
                                    '--state', str(self.state), 'diagnose',
                                ]), 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report['intake'], 'enabled')
        self.assertEqual(report['service'], 'not_configured')
        self.assertEqual(report['remote_result_publication'], 'not_checked')
        self.assertFalse((self.state / 'claims').exists())

    def test_diagnose_healthy_complete_result_and_remote_not_checked(self):
        self.send()
        self.assertEqual(self.agent.once()['status'], 'done')
        with patch('termux_agent.diagnostics._bundle_state', return_value=('verified', 'a' * 40)):
            with patch('termux_agent.diagnostics._runit_state', return_value='running'):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['status'], 'healthy')
        self.assertEqual((snapshot['claims'], snapshot['spooled']), (1, 1))
        self.assertEqual(snapshot['unspooled_claim_ids'], [])
        self.assertEqual(snapshot['invalid_spool_ids'], [])
        self.assertEqual(snapshot['remote_result_publication'], 'not_checked')

    def test_diagnose_unspooled_claim_is_attention_not_replay_or_pass(self):
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.agent.claim('inflight', 'a' * 64)
        with patch('termux_agent.diagnostics._bundle_state', return_value=('verified', 'a' * 40)):
            with patch('termux_agent.diagnostics._runit_state', return_value='running'):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['status'], 'attention')
        self.assertEqual(snapshot['unspooled_claim_ids'], ['inflight'])
        self.assertFalse((self.state / 'results' / 'inflight.json').exists())
        self.assertTrue(snapshot['no_replay'])

    def test_diagnose_invalid_spool_fails_closed_read_only(self):
        self.send()
        self.assertEqual(self.agent.once()['status'], 'done')
        spool = self.state / 'results' / 'termux-canary.json'
        spool.write_text('{"status":"done","status":"failed"}')
        old_bytes = spool.read_bytes()
        with patch('termux_agent.diagnostics._bundle_state', return_value=('verified', 'a' * 40)):
            with patch('termux_agent.diagnostics._runit_state', return_value='running'):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['status'], 'degraded')
        self.assertEqual(snapshot['invalid_spool_ids'], ['termux-canary'])
        self.assertEqual(spool.read_bytes(), old_bytes)

    def test_diagnose_broken_claim_and_unclaimed_spool(self):
        self.state.mkdir(mode=0o700, exist_ok=True)
        claims = self.state / 'claims'
        results = self.state / 'results'
        claims.mkdir(mode=0o700)
        results.mkdir(mode=0o700)
        (claims / 'bad.json').write_text('{"version":1,"version":2}')
        (results / 'orphan.json').write_text('{}')
        with patch('termux_agent.diagnostics._bundle_state', return_value=('verified', 'a' * 40)):
            with patch('termux_agent.diagnostics._runit_state', return_value='running'):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['status'], 'degraded')
        self.assertEqual(snapshot['invalid_claim_ids'], ['bad'])
        self.assertEqual(snapshot['orphan_spool_ids'], ['orphan'])
        self.assertEqual(snapshot['invalid_spool_ids'], ['orphan'])

    def test_bundle_retains_license_and_diagnose_detects_license_tampering(self):
        bundle = self.root / 'bundle'
        manifest = package(bundle, 'a' * 40)
        self.assertIn('LICENSE', manifest['files_sha256'])
        self.assertEqual((bundle / 'LICENSE').read_bytes(),
                         (ROOT.parents[2] / 'LICENSE').read_bytes())
        self.assertEqual(_bundle_state(bundle), ('verified', 'a' * 40))
        (bundle / 'LICENSE').write_bytes(b'altered')
        self.assertEqual(_bundle_state(bundle), ('invalid', 'a' * 40))

    def test_diagnose_runit_state_realized_without_starting_service(self):
        svc = self.state / 'runit/services/termux-agent'
        svc.mkdir(mode=0o700, parents=True)
        class Observation:
            def __init__(self, output: str, code: int):
                self.stdout, self.returncode = output, code
        with patch('termux_agent.diagnostics.shutil.which', return_value='/bin/sv'):
            with patch('termux_agent.diagnostics.subprocess.run',
                       return_value=Observation('run: isolated worker', 0)) as called:
                self.assertEqual(_runit_state(self.state), 'running')
                self.assertEqual(called.call_args.args[0][1], 'status')
            with patch('termux_agent.diagnostics.subprocess.run',
                       return_value=Observation('down: isolated worker', 0)):
                self.assertEqual(_runit_state(self.state), 'down')

    def test_diagnose_intake_disabled_does_not_hide_corrupt_spool(self):
        self.send()
        self.assertEqual(self.agent.once()['status'], 'done')
        (self.state / 'EMERGENCY_STOP').write_text('disabled\\n')
        path = self.state / 'results' / 'termux-canary.json'
        path.write_text('{}')
        with patch('termux_agent.diagnostics._bundle_state', return_value=('verified', 'a' * 40)):
            with patch('termux_agent.diagnostics._runit_state', return_value='running'):
                snapshot = diagnose(self.agent, bundle_root=ROOT)
        self.assertEqual(snapshot['intake'], 'disabled')
        self.assertEqual(snapshot['status'], 'degraded')
        self.assertEqual(snapshot['invalid_spool_ids'], ['termux-canary'])


if __name__ == '__main__':
    unittest.main()
