"""Explicit Termux process lifecycle; never installs services or boot hooks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

from linux_agent.agent import config_from_file, daemon_lock
from linux_agent.foundation.process import atomic_write_text
from .agent import TermuxAgent, validate_termux_environment, validate_private_path
from .diagnostics import diagnose


def power_command(name: str) -> None:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(name + ' unavailable; install/enable Termux:API first')
    outcome = subprocess.run([executable], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             timeout=8, check=False)
    if outcome.returncode:
        raise RuntimeError(name + ' failed')


@contextmanager
def optional_wakelock(enabled: bool):
    if enabled:
        power_command('termux-wake-lock')
    try:
        yield
    finally:
        if enabled:
            # Never hide an in-progress service exception on unlock failure.
            try:
                power_command('termux-wake-unlock')
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='termux-agent')
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--wake-lock', action='store_true',
                   help='hold a Termux:API wake lock only during serve')
    p.add_argument('--poll-seconds', type=float, default=10.0)
    p.add_argument('action', choices=('preflight', 'status', 'diagnose', 'once', 'serve',
                                      'publish', 'recover', 'stop'))
    args = p.parse_args(argv)
    _, home = validate_termux_environment(os.environ)
    validate_private_path(args.config, home)
    validate_private_path(args.state, home)
    if args.wake_lock and args.action != 'serve':
        p.error('--wake-lock is only supported with serve')
    if not 2 <= args.poll_seconds <= 3600:
        p.error('poll interval must be between 2 and 3600 seconds')
    config = config_from_file(args.config)
    agent = TermuxAgent(config, args.state)
    if args.action == 'preflight':
        for binary in ('git', 'sh'):
            if shutil.which(binary) is None:
                raise RuntimeError(binary + ' is not installed')
        out = {'status': 'ready_source_only', 'platform': 'termux',
               'host_id': config['host_id'], 'installation_id': config['installation_id']}
    elif args.action == 'stop':
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(args.state / 'EMERGENCY_STOP', 'disabled\n')
        out = {'status': 'disabled'}
    elif args.action == 'status':
        out = {'status': 'disabled' if agent.disabled() else 'enabled',
               'claimed': len(list((args.state / 'claims').glob('*.json'))),
               'spooled': len(list((args.state / 'results').glob('*.json')))}
    elif args.action == 'diagnose':
        out = diagnose(agent, bundle_root=Path(__file__).resolve().parents[1])
    elif args.action == 'serve':
        signal.signal(signal.SIGTERM, lambda _s, _f: agent.request_stop())
        signal.signal(signal.SIGINT, lambda _s, _f: agent.request_stop())
        with optional_wakelock(args.wake_lock):
            agent.serve(poll_seconds=args.poll_seconds)
        out = {'status': 'stopped'}
    elif args.action == 'once':
        out = agent.once()
    else:
        with daemon_lock(args.state):
            out = ({'recovered_unknown': agent.recover()} if args.action == 'recover'
                   else {'published': agent.publish_pending()})
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
