#!/usr/bin/env python3
from __future__ import annotations

import codecs
import contextlib
import fcntl
import io
import hashlib
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

OUTPUT_QUEUE_CAPACITY = 256
INHERITED_STOP_ENV = "LOCAL_AGENT_INHERITED_STOP_REQUEST"
OUTPUT_READ_SIZE = 8192
LEASE_FDS_ENV = "LOCAL_AGENT_LEASE_FDS"
LEASE_KEYS_DIGEST_ENV = "LOCAL_AGENT_LEASE_KEYS_DIGEST"
RESOURCE_LEASE_FDS_ENV = "LOCAL_AGENT_RESOURCE_LEASE_FDS"

_process_lock = threading.RLock()
_active_processes: dict[int, subprocess.Popen[Any]] = {}
_process_shutdown_requested = False
_process_spawn_in_progress = False
_deferred_termination_signal: int | None = None
_termination_deferral_owner: int | None = None
_termination_deferral_depth = 0


class ExecutionLeaseBusy(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(f"execution lease is busy: {key}")
        self.key = key


def lease_keys_digest(keys: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(keys),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class ExecutionLeaseSet:
    keys: tuple[str, ...]
    paths: tuple[Path, ...]
    handles: tuple[TextIO, ...]

    @property
    def fds(self) -> tuple[int, ...]:
        return tuple(handle.fileno() for handle in self.handles)

    def environment(self) -> dict[str, str]:
        return {
            LEASE_FDS_ENV: ",".join(str(fd) for fd in self.fds),
            LEASE_KEYS_DIGEST_ENV: lease_keys_digest(self.keys),
        }

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()


def execution_lease_path(lock_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return lock_dir / f"{digest}.lock"


def acquire_execution_leases(
    lock_dir: Path,
    keys: tuple[str, ...],
) -> ExecutionLeaseSet:
    ordered_keys = tuple(sorted(set(keys)))
    if not ordered_keys:
        raise ValueError("execution lease keys must not be empty")
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles: list[TextIO] = []
    paths: list[Path] = []
    try:
        for key in ordered_keys:
            path = execution_lease_path(lock_dir, key)
            handle = path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise ExecutionLeaseBusy(key) from None
            handles.append(handle)
            paths.append(path)
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {"pid": os.getpid(), "key": key},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(lock_dir)
        return ExecutionLeaseSet(
            keys=ordered_keys,
            paths=tuple(paths),
            handles=tuple(handles),
        )
    except Exception:
        for handle in reversed(handles):
            handle.close()
        raise


def _inherited_fds(env: Mapping[str, str], name: str) -> tuple[int, ...]:
    raw = env.get(name, "").strip()
    if not raw:
        return ()
    try:
        fds = tuple(int(item) for item in raw.split(","))
    except ValueError:
        raise RuntimeError(f"invalid {name}: {raw!r}") from None
    if not fds or any(fd < 3 for fd in fds) or len(set(fds)) != len(fds):
        raise RuntimeError(f"invalid {name}: {raw!r}")
    for fd in fds:
        try:
            os.fstat(fd)
        except OSError as exc:
            raise RuntimeError(f"closed execution lease descriptor in {name}: {fd}") from exc
    return fds


def inherited_lease_fds(env: Mapping[str, str]) -> tuple[int, ...]:
    return _inherited_fds(env, LEASE_FDS_ENV)


def inherited_resource_lease_fds(env: Mapping[str, str]) -> tuple[int, ...]:
    return _inherited_fds(env, RESOURCE_LEASE_FDS_ENV)


def popen_registered(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    """Spawn and register a process atomically with respect to shutdown."""
    global _process_spawn_in_progress
    env = kwargs.get("env")
    inherited_env = os.environ if env is None else env
    requested_fds = (
        *inherited_lease_fds(inherited_env),
        *inherited_resource_lease_fds(inherited_env),
    )
    explicit_fds = tuple(kwargs.pop("pass_fds", ()))
    kwargs["pass_fds"] = tuple(sorted(set((*explicit_fds, *requested_fds))))
    deferred_signal: int | None = None
    try:
        with _process_lock:
            caller_owns_deferral = (
                _termination_deferral_owner == threading.get_ident()
            )
            if _process_shutdown_requested and not caller_owns_deferral:
                raise RuntimeError("process spawn rejected during shutdown")
            _process_spawn_in_progress = True
            try:
                proc = subprocess.Popen(*args, **kwargs)
                if kwargs.get("start_new_session"):
                    setattr(proc, "_local_agent_process_group", proc.pid)
                _active_processes[proc.pid] = proc
            finally:
                _process_spawn_in_progress = False
                deferred_signal = _take_deferred_termination_signal()
    except BaseException:
        if deferred_signal is not None:
            _redeliver_termination_signal(deferred_signal)
        raise
    if deferred_signal is not None:
        _redeliver_termination_signal(deferred_signal)
    return proc


def _take_deferred_termination_signal() -> int | None:
    global _deferred_termination_signal
    if _process_spawn_in_progress or _termination_deferral_depth:
        return None
    signum = _deferred_termination_signal
    _deferred_termination_signal = None
    return signum


def _redeliver_termination_signal(signum: int) -> None:
    if threading.current_thread() is threading.main_thread():
        signal.raise_signal(signum)
        raise RuntimeError("termination signal handler returned unexpectedly")
    os.kill(os.getpid(), signum)


def process_shutdown_requested() -> bool:
    """Return whether a termination signal has committed this process to shutdown."""
    with _process_lock:
        return _process_shutdown_requested


def defer_termination(signum: int) -> bool:
    """Record shutdown and defer signal delivery until subprocess state is safe."""
    global _deferred_termination_signal, _process_shutdown_requested
    _process_shutdown_requested = True
    if not _process_spawn_in_progress and not _termination_deferral_depth:
        return False
    if _deferred_termination_signal is None:
        _deferred_termination_signal = signum
    return True


@contextlib.contextmanager
def termination_critical_section() -> Iterator[None]:
    """Allow one thread to finish a bounded subprocess transaction on shutdown."""
    global _termination_deferral_depth, _termination_deferral_owner
    deferred_signal: int | None = None
    owner = threading.get_ident()
    try:
        with _process_lock:
            if _process_shutdown_requested:
                raise RuntimeError("critical section rejected during shutdown")
            if _termination_deferral_owner not in (None, owner):
                raise RuntimeError("critical section ownership conflict")
            _termination_deferral_owner = owner
            _termination_deferral_depth += 1
            try:
                yield
            finally:
                _termination_deferral_depth -= 1
                if _termination_deferral_depth == 0:
                    _termination_deferral_owner = None
                deferred_signal = _take_deferred_termination_signal()
    finally:
        if deferred_signal is not None:
            _redeliver_termination_signal(deferred_signal)


def unregister_process(proc: subprocess.Popen[Any]) -> None:
    with _process_lock:
        _active_processes.pop(proc.pid, None)


def reset_process_lifecycle_for_tests() -> None:
    global _deferred_termination_signal, _process_shutdown_requested
    global _process_spawn_in_progress, _termination_deferral_depth
    global _termination_deferral_owner
    with _process_lock:
        live = [proc for proc in _active_processes.values() if proc.poll() is None]
        if live:
            raise RuntimeError("cannot reset process lifecycle with live processes")
        _active_processes.clear()
        _process_shutdown_requested = False
        _process_spawn_in_progress = False
        _deferred_termination_signal = None
        _termination_deferral_owner = None
        _termination_deferral_depth = 0


def fsync_directory(path: Path) -> None:
    """Persist directory entry changes when the platform supports directory fsync."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically and durably replace one UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


class BoundedTextBuffer:
    """Retain only the newest text up to a strict character limit."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("buffer limit must be positive")
        self.limit = limit
        self._chunks: deque[str] = deque()
        self._chars = 0
        self._total_chars = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._total_chars += len(text)
        if len(text) >= self.limit:
            self._chunks.clear()
            self._chunks.append(text[-self.limit :])
            self._chars = self.limit
            return

        self._chunks.append(text)
        self._chars += len(text)
        overflow = self._chars - self.limit
        while overflow > 0 and self._chunks:
            first = self._chunks[0]
            if len(first) <= overflow:
                self._chunks.popleft()
                self._chars -= len(first)
                overflow = self._chars - self.limit
                continue
            self._chunks[0] = first[overflow:]
            self._chars -= overflow
            overflow = 0

    def text(self) -> str:
        return "".join(self._chunks)

    def __len__(self) -> int:
        return self._chars

    @property
    def truncated(self) -> bool:
        return self._total_chars > self.limit


@dataclass
class OutputPump:
    process: subprocess.Popen[str]
    queue: queue.Queue[str | None]
    stop_event: threading.Event
    thread: threading.Thread
    activity_lock: threading.Lock
    last_activity_at: float

    def note_activity(self) -> None:
        with self.activity_lock:
            self.last_activity_at = time.monotonic()

    def activity_at(self) -> float:
        with self.activity_lock:
            return self.last_activity_at

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)
        if self.process.stdout is not None:
            try:
                self.process.stdout.close()
            except (OSError, ValueError):
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)


def spawn_shell(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.Popen[str]:
    """Start one shell command in its own process group."""
    proc = popen_registered(
        command,
        shell=True,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    setattr(proc, "_local_agent_process_group", proc.pid)
    return proc


def spawn_argv(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_text: str | None = None,
) -> subprocess.Popen[str]:
    """Start one argument-vector command in its own process group."""
    proc = popen_registered(
        args,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    setattr(proc, "_local_agent_process_group", proc.pid)
    return proc


def start_input_writer(
    proc: subprocess.Popen[str],
    input_text: str | None,
) -> threading.Thread | None:
    """Feed bounded command input without blocking the watchdog thread."""
    if input_text is None:
        return None

    def writer() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(input_text)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    thread = threading.Thread(target=writer, daemon=True, name="local-agent-input")
    thread.start()
    return thread


def start_output_pump(
    proc: subprocess.Popen[str],
    *,
    capacity: int = OUTPUT_QUEUE_CAPACITY,
    read_size: int = OUTPUT_READ_SIZE,
) -> OutputPump:
    """Read stdout in raw bounded chunks while preserving text line semantics."""
    if capacity < 1 or read_size < 1:
        raise ValueError("output pump bounds must be positive")

    lines: queue.Queue[str | None] = queue.Queue(maxsize=capacity)
    stop_event = threading.Event()
    activity_lock = threading.Lock()
    pump: OutputPump

    def put(item: str | None) -> bool:
        while not stop_event.is_set():
            try:
                lines.put(item, timeout=0.25)
                return True
            except queue.Full:
                continue
        return False

    def reader() -> None:
        assert proc.stdout is not None
        encoding = proc.stdout.encoding or "utf-8"
        errors = proc.stdout.errors or "replace"
        decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        newline_decoder = io.IncrementalNewlineDecoder(decoder, translate=True)
        pending = ""
        try:
            descriptor = proc.stdout.fileno()
            while not stop_event.is_set():
                raw = os.read(descriptor, read_size)
                if not raw:
                    break
                pump.note_activity()
                pending += newline_decoder.decode(raw)
                while pending:
                    newline = pending.find("\n")
                    if newline >= 0:
                        boundary = newline + 1
                    elif len(pending) >= read_size:
                        boundary = read_size
                    else:
                        break
                    chunk = pending[:boundary]
                    pending = pending[boundary:]
                    if not put(chunk):
                        return
            pending += newline_decoder.decode(b"", final=True)
            if pending:
                put(pending)
        except (OSError, ValueError, UnicodeError):
            return
        finally:
            put(None)

    thread = threading.Thread(target=reader, daemon=True, name="local-agent-output")
    pump = OutputPump(
        proc,
        lines,
        stop_event,
        thread,
        activity_lock,
        time.monotonic(),
    )
    thread.start()
    return pump


def process_group_for(proc: subprocess.Popen[str]) -> int | None:
    stored = getattr(proc, "_local_agent_process_group", None)
    if isinstance(stored, int) and stored > 1:
        return stored
    if proc.poll() is not None:
        return None
    try:
        process_group = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        return None
    return process_group if process_group > 1 else None


def process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_group(
    proc: subprocess.Popen[str],
    log: Callable[[str], None],
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Terminate the complete child process group, including surviving descendants."""
    process_group = process_group_for(proc)
    if process_group is None or process_group == os.getpgrp():
        return

    log(f"terminating process group pgid={process_group}")
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        log(f"cannot terminate process group pgid={process_group}: {exc}")
        return

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        proc.poll()
        if not process_group_alive(process_group):
            return
        time.sleep(0.05)

    proc.poll()
    if not process_group_alive(process_group):
        return
    log(f"killing process group pgid={process_group}")
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        log(f"cannot kill process group pgid={process_group}: {exc}")
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def terminate_active_processes(
    log: Callable[[str], None],
    *,
    grace_seconds: float = 5.0,
) -> int:
    """Stop every registered process group within one shared shutdown deadline."""
    with _process_lock:
        processes = list(_active_processes.values())

    groups: dict[int, subprocess.Popen[Any]] = {}
    for proc in processes:
        process_group = process_group_for(proc)
        if process_group is not None and process_group != os.getpgrp():
            groups[process_group] = proc
    if not groups:
        return 0

    remaining = set(groups)
    for process_group in groups:
        log(f"terminating active process group pgid={process_group}")
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            remaining.discard(process_group)
        except PermissionError as exc:
            log(f"cannot terminate active process group pgid={process_group}: {exc}")
            remaining.discard(process_group)

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while remaining and time.monotonic() < deadline:
        for proc in processes:
            proc.poll()
        remaining = {
            process_group
            for process_group in remaining
            if process_group_alive(process_group)
        }
        if remaining:
            time.sleep(0.05)

    for process_group in sorted(remaining):
        log(f"killing active process group pgid={process_group}")
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            log(f"cannot kill active process group pgid={process_group}: {exc}")
    reap_deadline = time.monotonic() + 1.0
    for proc in processes:
        remaining_seconds = max(0.0, reap_deadline - time.monotonic())
        if remaining_seconds == 0.0:
            proc.poll()
            continue
        try:
            proc.wait(timeout=remaining_seconds)
        except subprocess.TimeoutExpired:
            pass
    return len(groups)


def terminate_remaining_process_group(
    proc: subprocess.Popen[str],
    log: Callable[[str], None],
) -> bool:
    """Terminate descendants left behind after their direct parent has exited."""
    process_group = process_group_for(proc)
    if process_group is None or not process_group_alive(process_group):
        return False
    log(f"background process leak detected pgid={process_group}")
    terminate_process_group(proc, log)
    return True


def run_argv_bounded(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int,
    log: Callable[[str], None],
    input_text: str | None = None,
) -> dict[str, object]:
    """Run one argv command with bounded output, timeout and descendant cleanup."""
    started = time.monotonic()
    proc = spawn_argv(args, cwd=cwd, env=env, input_text=input_text)
    input_writer = start_input_writer(proc, input_text)
    pump = start_output_pump(proc)
    output = BoundedTextBuffer(output_limit)
    reader_done = False
    timed_out = False
    background_process_leak = False
    direct_process_finished = False

    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout and not timed_out and proc.poll() is None:
                timed_out = True
                terminate_process_group(proc, log)

            if proc.poll() is not None and not direct_process_finished:
                direct_process_finished = True
                background_process_leak = terminate_remaining_process_group(proc, log)

            try:
                item = pump.queue.get(timeout=0.05 if direct_process_finished else 0.25)
                if item is None:
                    reader_done = True
                else:
                    output.append(item)
            except queue.Empty:
                pass

            if direct_process_finished and reader_done:
                break
    finally:
        if proc.poll() is None:
            terminate_process_group(proc, log)
        pump.stop()
        if input_writer is not None:
            input_writer.join(timeout=1)
        unregister_process(proc)

    exit_code = proc.returncode if proc.returncode is not None else 124
    if timed_out:
        exit_code = 124
    elif background_process_leak:
        exit_code = 126
    result: dict[str, object] = {
        "exit_code": exit_code,
        "output": output.text(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if timed_out:
        result["timed_out"] = True
    if output.truncated:
        result["output_truncated"] = True
    if background_process_leak:
        result["background_process_leak"] = True
        result["failure_reason"] = "background_process_leak"
    return result
