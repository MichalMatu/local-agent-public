"""Build a *staged*, self-contained Termux source bundle; never install it.

Release bytes include the fixed compatible Linux wire/command subset but
never import an installed Debian package at runtime. SHA and files are
recorded for later independent device-side verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent
PRODUCT_LICENSE = ROOT.parents[2] / 'LICENSE'
# Bundled source comes from the single Linux engine, not Debian adapters.
LINUX = ROOT.parents[1] / 'linux' / 'core' / 'linux_agent'
FILES = (
    '__init__.py', 'agent.py', 'config.py', 'execution_recovery.py',
    'git_transport.py', 'resources.py',
    'foundation/__init__.py', 'foundation/core.py', 'foundation/process.py',
    'runtime/__init__.py', 'runtime/core.py', 'runtime/executor.py',
    'runtime/output.py', 'runtime/progress.py', 'runtime/task_contract.py',
    'runtime/telemetry.py', 'repository/__init__.py', 'repository/binding.py',
)


def package(destination: Path, source_sha: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{40}', source_sha):
        raise ValueError('source SHA must be a full immutable 40-hex commit')
    if destination.is_symlink() or destination.exists():
        raise ValueError('destination must not exist (no overwrite or upgrade)')
    destination.mkdir(mode=0o700, parents=True)
    try:
        if not PRODUCT_LICENSE.is_file() or PRODUCT_LICENSE.is_symlink():
            raise ValueError('missing/unsafe product license')
        shutil.copyfile(PRODUCT_LICENSE, destination / 'LICENSE')
        for name in FILES:
            src, dst = LINUX / name, destination / 'linux_agent' / name
            if not src.is_file() or src.is_symlink():
                raise ValueError('missing/unsafe frozen Linux dependency: ' + name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        source = ROOT / 'termux_agent'
        for src in sorted(source.rglob('*.py')):
            if src.is_symlink():
                raise ValueError('unsafe Termux source')
            dest = destination / src.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        digests = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(destination.rglob('*.py'))}
        digests['LICENSE'] = hashlib.sha256((destination / 'LICENSE').read_bytes()).hexdigest()
        manifest = {'version': 1, 'platform': 'termux', 'source_sha': source_sha,
                    'files_sha256': digests}
        (destination / 'manifest.json').write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    args = parser.parse_args()
    result = package(args.output, args.source_sha)
    print(json.dumps({'platform': result['platform'], 'files': len(result['files_sha256']),
                      'source_sha': result['source_sha']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
