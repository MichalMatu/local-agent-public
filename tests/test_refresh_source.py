"""Offline Git fixture tests for exact-HEAD Library source refresh."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sandbox/refresh-source.sh"
PACK = ROOT / 'scripts/sandbox/pack-source.sh'
BRANCH = 'work/fabric-github-first-live-mvp'


def run(*args: str | Path, cwd: Path | None = None, check: bool = True):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_AUTHOR_NAME': 'Sandbox', 'GIT_AUTHOR_EMAIL': 'sandbox@example.invalid',
           'GIT_COMMITTER_NAME': 'Sandbox', 'GIT_COMMITTER_EMAIL': 'sandbox@example.invalid'}
    cmd = list(map(str, args))
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=20)
    if check and result.returncode:
        raise AssertionError(f'{cmd}: {result.returncode}: {result.stderr}')
    return result


class RefreshSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        run('git', 'init', '-q', '-b', BRANCH, self.repo)
        pack = self.repo / 'scripts/sandbox/pack-source.sh'
        pack.parent.mkdir(parents=True)
        shutil.copyfile(PACK, pack)
        (self.repo / 'fixture.txt').write_text('first\n')
        run('git', 'add', '.', cwd=self.repo)
        run('git', 'commit', '-qm', 'first', cwd=self.repo)
        self.old = run('git', 'rev-parse', 'HEAD', cwd=self.repo).stdout.strip()
        self.bundle = self.root / 'cached.bundle'
        run('git', 'bundle', 'create', self.bundle, 'HEAD', cwd=self.repo)
        self.manifest = self.root / 'cached.bundle.sha256'
        self.manifest.write_text(f'{hashlib.sha256(self.bundle.read_bytes()).hexdigest()}  {self.bundle.name}\n')
        (self.repo / 'fixture.txt').write_text('second\n')
        run('git', 'add', 'fixture.txt', cwd=self.repo)
        run('git', 'commit', '-qm', 'second', cwd=self.repo)
        self.new = run('git', 'rev-parse', 'HEAD', cwd=self.repo).stdout.strip()

    def refresh(self, sha: str, out: str = 'new', remote: Path | str | None = None):
        return run('bash', SCRIPT, sha, BRANCH, self.bundle, self.manifest,
                   self.root / out, remote if remote is not None else self.repo,
                   check=False)

    def test_updates_cached_source_to_exact_remote_sha(self):
        result = self.refresh(self.new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SOURCE_SYNC_READY', result.stdout)
        bundle = self.root / 'new' / f'local-agent-source-{self.new}.bundle'
        self.assertTrue(bundle.is_file())
        check = self.root / 'fresh-checkout'
        run('git', 'clone', '-q', bundle, check)
        self.assertEqual(run('git', 'rev-parse', 'HEAD', cwd=check).stdout.strip(), self.new)
        self.assertEqual((check / 'fixture.txt').read_text(), 'second\n')

    def test_reuses_exact_cached_sha_without_network(self):
        result = self.refresh(self.old, remote='/does/not/exist')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pinned_sha_does_not_match_live_branch(self):
        result = self.refresh('f' * 40)
        self.assertEqual(result.returncode, 2)
        self.assertIn('differs', result.stderr)
        self.assertFalse((self.root / 'new').exists())

    def test_checksum_corruption_fails_closed(self):
        self.bundle.write_bytes(self.bundle.read_bytes() + b'corrupt')
        result = self.refresh(self.new)
        self.assertEqual(result.returncode, 2)
        self.assertIn('checksum', result.stderr)
        self.assertFalse((self.root / 'new').exists())

    def test_existing_destination_untouched(self):
        target = self.root / 'new'
        target.mkdir()
        (target / 'keep').write_text('safe')
        result = self.refresh(self.new)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((target / 'keep').read_text(), 'safe')

    def test_unavailable_remote_fails_without_stale_fallback(self):
        result = self.refresh(self.new, remote='/does/not/exist')
        self.assertEqual(result.returncode, 2)
        self.assertIn('remote source unavailable', result.stderr)
        self.assertFalse((self.root / 'new').exists())


if __name__ == '__main__':
    unittest.main()