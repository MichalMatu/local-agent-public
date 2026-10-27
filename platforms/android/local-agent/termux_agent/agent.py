"""Android lifecycle and suspend fencing; Git effects stay in proven Linux Core."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from linux_agent.agent import Agent as LinuxAgent
from linux_agent.runtime.core import Core
from linux_agent.runtime.executor import RuntimeExecutor
from linux_agent.runtime.task_contract import task_digest
from .runtime import install_shell_adapter
from .transport import TermuxGitTransport


SUSPEND_GAP_SECONDS = 30.0


def suspend_gap(previous_wall: float, previous_mono: float,
                wall: float, mono: float) -> bool:
    return (wall - previous_wall > SUSPEND_GAP_SECONDS
            or mono - previous_mono > SUSPEND_GAP_SECONDS
            or wall - previous_wall < -SUSPEND_GAP_SECONDS)


def validate_termux_environment(env: Mapping[str, str], *, require_device: bool = True) -> tuple[Path, Path]:
    """Validate private app files, not shared /sdcard or an external working tree.

    `require_device=False` is for pure offline contract tests only; the CLI
    always validates the physical Termux layout.
    """
    prefix_raw, home_raw = env.get('PREFIX', ''), env.get('HOME', '')
    if not prefix_raw or not home_raw:
        raise ValueError('Termux PREFIX and HOME are required')
    prefix, home = Path(prefix_raw), Path(home_raw)
    if not prefix.is_absolute() or not home.is_absolute() or prefix.is_symlink() or home.is_symlink():
        raise ValueError('Termux paths must be absolute non-symlink paths')
    if prefix.name != 'usr' or home.name != 'home' or prefix.parent != home.parent:
        raise ValueError('PREFIX/HOME do not belong to one Termux installation')
    if require_device and prefix.as_posix() not in (
        '/data/data/com.termux/files/usr', '/data/user/0/com.termux/files/usr'
    ):
        raise ValueError('not the supported Android Termux package path')
    if not prefix.is_dir() or not home.is_dir():
        raise ValueError('Termux private directories do not exist')
    if prefix.resolve().parent != home.resolve().parent:
        raise ValueError('Termux paths resolve into different installations')
    if home.stat().st_uid != os.getuid():
        raise ValueError('Termux HOME belongs to another UID')
    return prefix, home


def validate_private_path(path: Path, home: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or path == home:
        raise ValueError('state/config must be private absolute paths below Termux HOME')
    try:
        path.relative_to(home)
    except ValueError:
        raise ValueError('state/config must be below Termux HOME') from None
    # Existing parents may not redirect into Android shared storage.
    nearest = path
    while not nearest.exists():
        nearest = nearest.parent
    if not nearest.resolve().is_relative_to(home.resolve()):
        raise ValueError('state/config parent escapes Termux HOME')


class TermuxAgent(LinuxAgent):
    def __init__(self, config: dict[str, Any], state: Path, *,
                 environ: Mapping[str, str] | None = None, require_device: bool = True):
        env = os.environ if environ is None else environ
        prefix, home = validate_termux_environment(env, require_device=require_device)
        validate_private_path(state, home)
        install_shell_adapter(prefix)
        super().__init__(config, state)
        self.transport = TermuxGitTransport(config, state)

    def _execute(self, task: dict[str, Any]) -> dict[str, Any]:
        """UNKNOWN when Android suspends the watcher during a live command.

        A clock gap cannot prove that a child did *not* cause effects. Kill
        the observed process group, preserve the claim, never retry it.
        """
        finished = threading.Event()
        uncertain = threading.Event()
        # The watcher can observe STOP/suspend between stages; propagate its
        # sticky fence into the shared executor before another shell starts.
        executor = RuntimeExecutor(Core(Path(task['cwd'])), interruption=uncertain)
        previous_wall, previous_mono = time.time(), time.monotonic()

        def watcher() -> None:
            nonlocal previous_wall, previous_mono
            while not finished.wait(0.10):
                wall, mono = time.time(), time.monotonic()
                if (suspend_gap(previous_wall, previous_mono, wall, mono)
                        or self.disabled() or self.stop_requested.is_set()):
                    uncertain.set()
                    executor.terminate_active_command()
                    return
                previous_wall, previous_mono = wall, mono

        watcher_thread = threading.Thread(target=watcher, daemon=True)
        watcher_thread.start()
        try:
            if self.disabled() or self.stop_requested.is_set():
                uncertain.set()
                result = {'version': 1, 'id': task['id'], 'status': 'unknown',
                          'reason': 'stop_after_claim', 'task_digest': task_digest(task)}
            else:
                result = executor.process_task(task)
        finally:
            finished.set()
            watcher_thread.join(timeout=1.0)
        if uncertain.is_set() or self.disabled() or self.stop_requested.is_set():
            result['status'] = 'unknown'
            result['reason'] = 'android_suspend_or_stop_effects_uncertain'
        return result

    def serve(self, poll_seconds: float = 10.0) -> None:
        # Short owner-selected response window; Android Doze may delay wakeups.
        # No telemetry or heartbeat; only the existing Git task/result poll.
        super().serve(poll_seconds=poll_seconds, adaptive=True)
