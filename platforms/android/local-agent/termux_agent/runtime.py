"""Termux POSIX shell adapter for the frozen Linux command runtime.

Only the isolated Termux process installs this adapter; installed Debian
and macOS runtimes are never modified. Python's default /bin/sh is not
available in the Android app sandbox.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

from linux_agent.foundation.process import popen_registered
from linux_agent.runtime import executor


def shell_path(prefix: Path) -> Path:
    if not prefix.is_absolute() or prefix.name != 'usr':
        raise ValueError('invalid Termux PREFIX')
    candidate = prefix / 'bin' / 'sh'
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError('Termux POSIX shell is not executable')
    return candidate


def install_shell_adapter(prefix: Path) -> None:
    executable = str(shell_path(prefix))

    def spawn_termux_shell(command: str, *, cwd: Path, env: Mapping[str, str]):
        proc = popen_registered(
            command, shell=True, executable=executable, cwd=cwd,
            env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True,
        )
        setattr(proc, '_local_agent_process_group', proc.pid)
        return proc

    executor.spawn_shell = spawn_termux_shell
