"""External operator controller for a *previously staged* Linux Agent user unit.

Unlike service_install, this component can activate a real persistent unit.
It NEVER stages, overwrites or removes a unit, and must never run as a
Linux Agent task. First installation has a cold (stopped) rollback: rearm
STOP, disable and stop only our verified unit. No Git task is replayed.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterator

from linux_agent.agent import config_from_file, daemon_lock, validate_config
from linux_agent.foundation.process import atomic_write_text, fsync_directory
from linux_agent.release_controller import ServiceSwitchError, verify_clean_exact_source
from linux_agent.service_install import (
    PersistentUnitStage, UNIT_NAME, _run_systemctl, _private_dir, _no_duplicate,
)
from linux_agent.service_plan import ReleasePair, source_pythonpath

_ACTIVATION = "linux-service-activation-v1.json"
_PHASES = {"starting", "running", "stopping", "stopped"}


def _run_command(*command: str) -> str:
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceSwitchError("required host manager command unavailable") from exc
    if result.returncode:
        raise ServiceSwitchError("host manager command failed: " + command[0])
    return result.stdout.strip()


def _write_stop(state: Path) -> None:
    # Match Linux Agent's own disable operation: atomic durable local STOP.
    stop = state / "EMERGENCY_STOP"
    if stop.is_symlink():
        raise ServiceSwitchError("unsafe emergency STOP path")
    atomic_write_text(stop, "disabled\n")


@contextmanager
def _activation_lock(state: Path) -> Iterator[None]:
    path = state / "linux-service-activation-v1.lock"
    if path.is_symlink():
        raise ServiceSwitchError("unsafe activation lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class PersistentServiceActivation:
    """Drive the already staged exact unit with durable start/stop receipts."""

    def __init__(self, pair: ReleasePair):
        self.pair = pair
        self.stage = PersistentUnitStage(pair)
        self.journal = pair.state / _ACTIVATION

    def _preflight(self) -> str:
        # The staging preflight deliberately REQUIRES STOP and therefore
        # cannot inspect or stop a running service. Validate those same exact
        # release/config/ownership properties without demanding active STOP.
        self.pair.validate()
        verify_clean_exact_source(self.pair.candidate, self.pair.candidate_sha)
        verify_clean_exact_source(self.pair.rollback, self.pair.rollback_sha)
        _private_dir(self.pair.state)
        # This installation sits beside the original disposable pilot. Its
        # independently owned emergency STOP must never be removed by cutover.
        legacy_pilot = self.pair.state.parent / "pilot"
        if legacy_pilot.exists():
            original_stop = legacy_pilot / "EMERGENCY_STOP"
            if original_stop.is_symlink() or not original_stop.is_file():
                raise ServiceSwitchError("original pilot emergency STOP missing")
        validate_config(config_from_file(self.pair.config), self.pair.state)
        unit = self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha)
        if (self.stage._journal(unit) or {}).get("phase") != "staged":
            raise ServiceSwitchError("unit must already have an accepted staged journal")
        path = self.stage.unit_file
        if path.is_symlink() or not path.is_file() or path.read_text() != unit:
            raise ServiceSwitchError("staged unit bytes changed or missing")
        if _run_systemctl("show", UNIT_NAME, "--property=LoadState", "--value") != "loaded":
            raise ServiceSwitchError("staged service is not loaded")
        if _run_systemctl("show", UNIT_NAME, "--property=FragmentPath", "--value") != str(path):
            raise ServiceSwitchError("systemd fragment does not identify staged unit")
        if os.environ.get("LOCAL_AGENT_LEASE_FDS") or os.environ.get("LOCAL_AGENT_LEASE_KEYS_DIGEST"):
            raise ServiceSwitchError("operator service control forbidden inside agent task")
        return unit

    def _identity(self, unit: str) -> dict[str, object]:
        import hashlib
        return {
            "version": 1,
            "unit": UNIT_NAME,
            "candidate_sha": self.pair.candidate_sha,
            "rollback_sha": self.pair.rollback_sha,
            "unit_sha256": hashlib.sha256(unit.encode()).hexdigest(),
        }

    def _read_journal(self, unit: str) -> str | None:
        if self.journal.is_symlink():
            raise ServiceSwitchError("activation journal is symlink")
        if not self.journal.exists():
            return None
        if not self.journal.is_file() or self.journal.stat().st_size > 4096:
            raise ServiceSwitchError("activation journal unsafe or oversized")
        try:
            data = json.loads(self.journal.read_text(), object_pairs_hook=_no_duplicate)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            raise ServiceSwitchError("activation journal unreadable") from exc
        expected = self._identity(unit)
        if (not isinstance(data, dict) or set(data) != set(expected) | {"phase"}
                or any(data.get(k) != v for k, v in expected.items())
                or data.get("phase") not in _PHASES):
            raise ServiceSwitchError("activation journal identity mismatch")
        return str(data["phase"])

    def _record(self, phase: str, unit: str) -> None:
        if phase not in _PHASES:
            raise AssertionError("unknown activation phase")
        self._read_journal(unit)
        atomic_write_text(
            self.journal,
            json.dumps({**self._identity(unit), "phase": phase},
                       separators=(",", ":"), sort_keys=True) + "\n",
        )

    def _systemd(self, field: str) -> str:
        return _run_systemctl("show", UNIT_NAME, "--property=" + field, "--value")

    def _is_running(self) -> int | None:
        if self._systemd("ActiveState") != "active":
            return None
        try:
            pid = int(self._systemd("MainPID"))
        except ValueError as exc:
            raise ServiceSwitchError("systemd MainPID invalid") from exc
        control_group = self._systemd("ControlGroup")
        if pid <= 1 or not control_group.startswith("/"):
            raise ServiceSwitchError("active unit lacks verified process/cgroup")
        proc = Path("/proc") / str(pid)
        expected_argv = [
            str(self.pair.python).encode(), b"-B", b"-m", b"linux_agent",
            b"--config", str(self.pair.config).encode(),
            b"--state", str(self.pair.state).encode(), b"serve",
        ]
        expected_environment = ("PYTHONPATH=" + source_pythonpath(self.pair.candidate)).encode()
        try:
            # PYTHONPATH alone does not establish executable identity.
            # Verify the uid, argv and kernel cgroup of the reported MainPID.
            if proc.stat().st_uid != os.getuid():
                raise ServiceSwitchError("live process belongs to another OS user")
            environment = (proc / "environ").read_bytes().split(b"\x00")
            argv = (proc / "cmdline").read_bytes().split(b"\x00")
            if argv and argv[-1] == b"":
                argv.pop()
            cgroups = (proc / "cgroup").read_text().splitlines()
        except OSError as exc:
            raise ServiceSwitchError("cannot verify live process") from exc
        if expected_environment not in environment:
            raise ServiceSwitchError("live service uses wrong source revision")
        if argv != expected_argv:
            raise ServiceSwitchError("live service command/identity mismatch")
        cgroup_paths = [
            line.split(":", 2)[2] for line in cgroups if line.count(":") >= 2
        ]
        if not any(path == control_group or path.startswith(control_group + "/")
                   for path in cgroup_paths):
            raise ServiceSwitchError("live process does not belong to service cgroup")
        return pid

    def _is_enabled(self) -> bool:
        return _run_systemctl("is-enabled", UNIT_NAME) == "enabled"

    def _require_linger(self) -> None:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        if _run_command("loginctl", "show-user", user,
                        "--property=Linger", "--value") != "yes":
            raise ServiceSwitchError("user linger is disabled; persistent login lifecycle unavailable")

    def _quiesce(self) -> None:
        """Try both external fences even if writing STOP or disable fails.

        No exception can skip the attempt to stop the service. The caller
        leaves its 'starting'/'stopping' receipt intact when any part of
        cleanup is uncertain so that a separate operator can reconcile.
        """
        failures: list[str] = []
        try:
            _write_stop(self.pair.state)
        except (OSError, ServiceSwitchError) as exc:
            failures.append("emergency STOP could not be rearmed: " + type(exc).__name__)
        for operation in ("disable", "stop"):
            try:
                _run_systemctl(operation, UNIT_NAME)
            except (OSError, ServiceSwitchError) as exc:
                failures.append("systemd " + operation + " failed: " + type(exc).__name__)
        try:
            if self._is_running() is not None or self._is_enabled():
                failures.append("systemd unit still running or enabled")
        except (OSError, ServiceSwitchError) as exc:
            failures.append("systemd state cannot be verified: " + type(exc).__name__)
        stop_file = self.pair.state / "EMERGENCY_STOP"
        if stop_file.is_symlink() or not stop_file.is_file():
            failures.append("emergency STOP absent after cleanup")
        if failures:
            raise ServiceSwitchError("; ".join(failures))

    def inspect(self) -> dict[str, object]:
        """Read staged identity, service state and durable local STOP."""
        unit = self._preflight()
        phase = self._read_journal(unit)
        pid = self._is_running()
        stop = self.pair.state / "EMERGENCY_STOP"
        return {
            "phase": phase or "not_started",
            "active_pid": pid,
            "enabled": self._is_enabled(),
            "emergency_stop": stop.is_file() and not stop.is_symlink(),
            "candidate_sha": self.pair.candidate_sha,
            "unit": UNIT_NAME,
        }

    def start(self) -> dict[str, object]:
        """Explicit first-install activation of an externally staged unit.

        No service starts while STOP is in place. Any uncertain start
        ends disabled with STOP rearmed; the durable journal is retained.
        """
        unit = self._preflight()
        self._require_linger()
        with _activation_lock(self.pair.state):
            phase = self._read_journal(unit)
            if phase == "running":
                info = self.inspect()
                if info["active_pid"] and info["enabled"] and not info["emergency_stop"]:
                    return info
                raise ServiceSwitchError("persisted running phase disagrees with systemd")
            if phase not in (None, "stopped"):
                raise ServiceSwitchError("interrupted activation: fail closed")
            if self._is_running() is not None or self._is_enabled():
                raise ServiceSwitchError("unit already running/enabled unexpectedly")
            stop = self.pair.state / "EMERGENCY_STOP"
            if stop.is_symlink() or not stop.is_file():
                raise ServiceSwitchError("first start requires the separate emergency STOP")
            validate_config(config_from_file(self.pair.config), self.pair.state)
            # Sample and release the daemon lock BEFORE starting systemd:
            # the actual Linux Agent must take that same lock for its lifetime.
            with daemon_lock(self.pair.state):
                pass
            self._record("starting", unit)
            try:
                stop.unlink()
                fsync_directory(self.pair.state)
                _run_systemctl("start", UNIT_NAME)
                deadline = time.monotonic() + 15
                pid = None
                while time.monotonic() < deadline:
                    pid = self._is_running()
                    if pid:
                        break
                    time.sleep(0.2)
                if not pid:
                    raise ServiceSwitchError("service failed to start on exact candidate")
                # Durable confirmation BEFORE enabling unattended boot.
                # A crash after enable but before the journal were committed
                # could otherwise boot an uncertain installation with intake
                # open and no accepted running receipt.
                self._record("running", unit)
                _run_systemctl("enable", UNIT_NAME)
                if not self._is_enabled() or self._is_running() != pid:
                    raise ServiceSwitchError("service did not stay enabled and alive")
                final = self.inspect()
                if (final["active_pid"] != pid or not final["enabled"]
                        or final["emergency_stop"] or final["phase"] != "running"):
                    raise ServiceSwitchError("post-activation readback mismatch")
                return final
            except BaseException:
                # Keep the initiating error distinct; cleanup still attempts
                # systemctl stop even when rearming STOP or disable fails.
                try:
                    self._quiesce()
                    self._record("stopped", unit)
                except BaseException as stop_error:
                    raise ServiceSwitchError("activation failed; service cleanup NOT VERIFIED") from stop_error
                raise

    def reconcile(self) -> dict[str, object]:
        """Operator-only cold recovery of an interrupted lifecycle transition.

        A crash between durable 'running' and systemd enable leaves a
        legitimately recorded running process but no boot enablement. If that
        record disagrees with the OS, reconcile *stops* rather than silently
        enabling or restarting. A healthy 'running' service is never stopped
        by a stale reconciliation request.
        """
        unit = self._preflight()
        with _activation_lock(self.pair.state):
            phase = self._read_journal(unit)
            if phase == "running":
                info = self.inspect()
                if info["active_pid"] and info["enabled"] and not info["emergency_stop"]:
                    raise ServiceSwitchError("healthy running service: nothing to reconcile")
            elif phase not in ("starting", "stopping"):
                raise ServiceSwitchError("no interrupted activation to reconcile")
            # Close admission before attempting an external stop. Any failure
            # retains the phase receipt and STOP for later independent repair.
            self._quiesce()
            self._record("stopped", unit)
            return self.inspect()

    def stop(self) -> dict[str, object]:
        """External emergency operator stop (never deletes durable tasks)."""
        unit = self._preflight()
        with _activation_lock(self.pair.state):
            phase = self._read_journal(unit)
            if phase not in ("running", "stopped"):
                raise ServiceSwitchError("no known running/stopped activation to stop")
            self._record("stopping", unit)
            self._quiesce()
            self._record("stopped", unit)
            return self.inspect()


def main(argv: list[str] | None = None) -> int:
    """External user-terminal only. NEVER dispatch through an agent task."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "start", "stop", "reconcile"))
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--rollback-root", type=Path, required=True)
    parser.add_argument("--rollback-sha", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    pair = ReleasePair(
        candidate=args.candidate_root, candidate_sha=args.candidate_sha,
        rollback=args.rollback_root, rollback_sha=args.rollback_sha,
        python=Path(sys.executable).resolve(),
        config=args.config, state=args.state,
    )
    try:
        result = getattr(PersistentServiceActivation(pair), args.action)()
    except (ServiceSwitchError, ValueError, OSError, AssertionError) as exc:
        print("Persistent Linux service REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
