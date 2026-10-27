"""Operator-only, durable upgrade of an ALREADY active Debian user systemd unit.

This is a standalone external terminal controller, never an Agent/Git/SSH task.
The original first-install stage/activation receipts remain immutable.
One candidate switch plus independently verifiable rollback per upgrade journal.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from linux_agent.agent import config_from_file, validate_config
from linux_agent.foundation.process import atomic_write_text, fsync_directory
from linux_agent.release_controller import ServiceSwitchError, verify_clean_exact_source
from linux_agent.service_activation import _activation_lock, _write_stop
from linux_agent.service_install import (
    UNIT_NAME, PersistentUnitStage, _no_duplicate, _private_dir, _run_systemctl,
)
from linux_agent.service_plan import ReleasePair, source_pythonpath

_JOURNAL = "linux-service-upgrade-v1.json"
_PHASES = {
    "fencing", "switch_requested", "committed",
    "rollback_requested", "rolled_back",
}
_MAX_JOURNAL = 4096


def _external_only() -> None:
    # Check before any mutation, including writing a journal/STOP.
    if os.environ.get("LOCAL_AGENT_LEASE_FDS") or os.environ.get("LOCAL_AGENT_LEASE_KEYS_DIGEST"):
        raise ServiceSwitchError("operator service control forbidden inside agent task")


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JOURNAL:
        raise ServiceSwitchError("required owner-private receipt missing or unsafe")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ServiceSwitchError("unreadable durable receipt") from exc
    if not isinstance(obj, dict):
        raise ServiceSwitchError("durable receipt must be object")
    return obj


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PersistentServiceUpgrade:
    """One exact baseline -> candidate switch, and candidate -> baseline rollback."""

    def __init__(self, pair: ReleasePair):
        self.pair = pair
        self.stage = PersistentUnitStage(pair)
        self.journal = pair.state / _JOURNAL
        self.lock = pair.state / "linux-service-upgrade-v1.lock"
        self.old_unit = pair.unit_for(pair.rollback, pair.rollback_sha)
        self.new_unit = pair.unit_for(pair.candidate, pair.candidate_sha)

    def _identity(self) -> dict[str, Any]:
        return {
            "version": 1, "unit": UNIT_NAME,
            "baseline_sha": self.pair.rollback_sha,
            "candidate_sha": self.pair.candidate_sha,
            "baseline_unit_sha256": _digest(self.old_unit),
            "candidate_unit_sha256": _digest(self.new_unit),
        }

    def _journal_phase(self) -> str | None:
        if self.journal.is_symlink():
            raise ServiceSwitchError("upgrade journal symlink")
        if not self.journal.exists():
            return None
        record = _read_private_json(self.journal)
        if (set(record) != set(self._identity()) | {"phase"}
                or any(record.get(k) != v for k, v in self._identity().items())
                or record.get("phase") not in _PHASES):
            raise ServiceSwitchError("upgrade journal identity/phase mismatch")
        return str(record["phase"])

    def _record(self, phase: str) -> None:
        if phase not in _PHASES:
            raise AssertionError("unsupported upgrade phase")
        self._journal_phase()
        atomic_write_text(
            self.journal,
            json.dumps({**self._identity(), "phase": phase}, sort_keys=True,
                       separators=(",", ":")) + "\n",
        )

    def _preflight(self) -> None:
        self.pair.validate()
        verify_clean_exact_source(self.pair.candidate, self.pair.candidate_sha)
        verify_clean_exact_source(self.pair.rollback, self.pair.rollback_sha)
        _private_dir(self.pair.state)
        validate_config(config_from_file(self.pair.config), self.pair.state)
        pilot = self.pair.state.parent / "pilot"
        if pilot.exists():
            old_stop = pilot / "EMERGENCY_STOP"
            if old_stop.is_symlink() or not old_stop.is_file():
                raise ServiceSwitchError("legacy pilot STOP not preserved")
        unit_file = self.stage.unit_file
        if (self.stage.unit_dir.is_symlink() or unit_file.is_symlink()
                or not unit_file.is_file()):
            raise ServiceSwitchError("installed service unit path unsafe")
        if (_run_systemctl("show", UNIT_NAME, "--property=LoadState", "--value")
                != "loaded"):
            raise ServiceSwitchError("service is not loaded")
        if (_run_systemctl("show", UNIT_NAME, "--property=FragmentPath", "--value")
                != str(unit_file)):
            raise ServiceSwitchError("unexpected systemd unit fragment")
        if _run_systemctl("is-enabled", UNIT_NAME) != "enabled":
            raise ServiceSwitchError("existing installation must be enabled")

    def _original_receipts(self) -> None:
        """Verify the already installed, operator-accepted source provenance."""
        staged = _read_private_json(self.pair.state / "linux-service-stage-v1.json")
        activated = _read_private_json(self.pair.state / "linux-service-activation-v1.json")
        digest = _digest(self.old_unit)
        if (staged.get("version") != 1 or staged.get("phase") != "staged"
                or staged.get("unit_name") != UNIT_NAME
                or staged.get("candidate_sha") != self.pair.rollback_sha
                or staged.get("unit_sha256") != digest
                or not isinstance(staged.get("rollback_sha"), str)
                or set(staged) != {"version", "phase", "unit_name", "candidate_sha",
                                  "rollback_sha", "unit_sha256"}):
            raise ServiceSwitchError("original staged unit provenance mismatch")
        if (activated != {
                "version": 1, "phase": "running", "unit": UNIT_NAME,
                "candidate_sha": self.pair.rollback_sha,
                "rollback_sha": staged["rollback_sha"],
                "unit_sha256": digest}):
            raise ServiceSwitchError("original running activation receipt mismatch")

    def _unit_bytes(self) -> str:
        path = self.stage.unit_file
        if path.is_symlink() or not path.is_file():
            raise ServiceSwitchError("installed unit unsafe or missing")
        if path.stat().st_size > 8192:
            raise ServiceSwitchError("installed unit oversized")
        content = path.read_text(encoding="utf-8")
        if content not in (self.old_unit, self.new_unit):
            raise ServiceSwitchError("installed unit changed outside release transaction")
        return content

    def _service(self, field: str) -> str:
        return _run_systemctl("show", UNIT_NAME, "--property=" + field, "--value")

    def _active_pid(self, root: Path) -> int:
        """Verify the exact unit's live PID, owner, argv, source and cgroup."""
        if self._service("ActiveState") != "active":
            raise ServiceSwitchError("service not active")
        try:
            pid = int(self._service("MainPID"))
        except ValueError as exc:
            raise ServiceSwitchError("systemd MainPID invalid") from exc
        cgroup = self._service("ControlGroup")
        if pid <= 1 or not cgroup.startswith("/"):
            raise ServiceSwitchError("service cgroup/PID unavailable")
        proc = Path("/proc") / str(pid)
        expected_argv = [
            str(self.pair.python).encode(), b"-B", b"-m", b"linux_agent",
            b"--config", str(self.pair.config).encode(),
            b"--state", str(self.pair.state).encode(), b"serve",
        ]
        source = ("PYTHONPATH=" + source_pythonpath(root)).encode()
        try:
            if proc.stat().st_uid != os.getuid():
                raise ServiceSwitchError("service process owned by another OS user")
            env = (proc / "environ").read_bytes().split(b"\0")
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            groups = (proc / "cgroup").read_text().splitlines()
        except OSError as exc:
            raise ServiceSwitchError("live process evidence unreadable") from exc
        if argv and argv[-1] == b"":
            argv.pop()
        if argv != expected_argv or source not in env:
            raise ServiceSwitchError("live process source/argv mismatch")
        if not any(
            (line.split(":", 2)[2] == cgroup
             or line.split(":", 2)[2].startswith(cgroup + "/"))
            for line in groups if line.count(":") >= 2
        ):
            raise ServiceSwitchError("live process cgroup mismatch")
        return pid

    def _require_no_stop(self) -> None:
        stop = self.pair.state / "EMERGENCY_STOP"
        if stop.is_symlink() or stop.exists():
            raise ServiceSwitchError("intake is not in the expected enabled state")

    def _drain(self, timeout: float = 40.0) -> None:
        """Require every claimed task to have a durable result, after STOP.

        STOP is the admission fence; result counts are never treated as Git queue
        size. Neither claims nor results are removed/modified.
        """
        deadline = time.monotonic() + timeout
        while True:
            pending = 0
            for claim in (self.pair.state / "claims").glob("*.json"):
                if claim.is_symlink() or not claim.is_file():
                    raise ServiceSwitchError("unsafe task claim in drain")
                result = self.pair.state / "results" / claim.name
                if result.is_symlink():
                    raise ServiceSwitchError("unsafe task result in drain")
                if not result.is_file():
                    pending += 1
            if not pending:
                # Let a poll/admission loop observe STOP and recheck.
                time.sleep(1)
                if any(not (self.pair.state / "results" / c.name).is_file()
                       for c in (self.pair.state / "claims").glob("*.json")):
                    continue
                return
            if time.monotonic() >= deadline:
                raise ServiceSwitchError("active/uncertain claims not drained; STOP retained")
            time.sleep(0.5)

    def _stop_service(self) -> None:
        _run_systemctl("stop", UNIT_NAME)
        if self._service("ActiveState") not in {"inactive", "failed"}:
            raise ServiceSwitchError("service did not stop")
        if self._service("MainPID") not in {"0", ""}:
            raise ServiceSwitchError("service MainPID persists after stop")

    def _write_unit(self, expected: str, replacement: str) -> None:
        if self._unit_bytes() != expected:
            raise ServiceSwitchError("unit changed before atomic replacement")
        atomic_write_text(self.stage.unit_file, replacement)
        _run_systemctl("daemon-reload")
        if self._unit_bytes() != replacement or self._service("LoadState") != "loaded":
            raise ServiceSwitchError("replacement unit not loaded")
        if self._service("FragmentPath") != str(self.stage.unit_file):
            raise ServiceSwitchError("reloaded wrong fragment")

    def _start_verified(self, root: Path) -> int:
        _run_systemctl("start", UNIT_NAME)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                pid = self._active_pid(root)
                if _run_systemctl("is-enabled", UNIT_NAME) == "enabled":
                    return pid
            except ServiceSwitchError:
                pass
            time.sleep(0.25)
        raise ServiceSwitchError("new systemd process never reached expected source/cgroup")

    def _unfence(self, root: Path, unit: str) -> int:
        if self._unit_bytes() != unit:
            raise ServiceSwitchError("unit changed before reopening intake")
        pid = self._active_pid(root)
        if _run_systemctl("is-enabled", UNIT_NAME) != "enabled":
            raise ServiceSwitchError("unit not enabled before intake")
        stop = self.pair.state / "EMERGENCY_STOP"
        if stop.is_symlink() or not stop.is_file():
            raise ServiceSwitchError("STOP missing before controlled intake reopen")
        stop.unlink()
        fsync_directory(self.pair.state)
        return pid

    def inspect(self) -> dict[str, Any]:
        self._preflight()
        phase = self._journal_phase()
        if phase is None:
            self._original_receipts()
        unit = self._unit_bytes()
        return {
            "phase": phase or "not_started",
            "unit": UNIT_NAME, "candidate_sha": self.pair.candidate_sha,
            "baseline_sha": self.pair.rollback_sha,
            "installed_sha": (self.pair.candidate_sha if unit == self.new_unit
                              else self.pair.rollback_sha),
            "active_state": self._service("ActiveState"),
            "enabled": True,
            "stop_present": (self.pair.state / "EMERGENCY_STOP").is_file(),
            "upgraded": phase == "committed",
        }

    def _locks(self) -> ExitStack:
        stack = ExitStack()
        try:
            stack.enter_context(_activation_lock(self.pair.state))
            from linux_agent.release_controller import _controller_lock
            stack.enter_context(_controller_lock(self.lock))
            return stack
        except BaseException:
            stack.close()
            raise

    def _restore_baseline(self) -> int:
        """Called with external locks and STOP already asserted."""
        _write_stop(self.pair.state)
        self._drain()
        current = self._unit_bytes()
        self._stop_service()
        if current == self.new_unit:
            self._write_unit(self.new_unit, self.old_unit)
        pid = self._start_verified(self.pair.rollback)
        self._record("rolled_back")
        self._unfence(self.pair.rollback, self.old_unit)
        return pid

    def switch(self) -> dict[str, Any]:
        _external_only()
        self._preflight()
        with self._locks():
            if self._journal_phase() is not None:
                raise ServiceSwitchError("prior upgrade journal exists; no replay/second switch")
            self._original_receipts()
            if self._unit_bytes() != self.old_unit:
                raise ServiceSwitchError("installed baseline unit changed")
            self._require_no_stop()
            baseline_pid = self._active_pid(self.pair.rollback)
            self._record("fencing")
            try:
                _write_stop(self.pair.state)
                self._drain()
                self._record("switch_requested")
                self._stop_service()
                self._write_unit(self.old_unit, self.new_unit)
                candidate_pid = self._start_verified(self.pair.candidate)
                # Persist verification before allowing the new intake.
                self._record("committed")
                self._unfence(self.pair.candidate, self.new_unit)
                return {"status": "candidate_active", "phase": "committed",
                        "baseline_pid": baseline_pid,
                        "candidate_pid": candidate_pid,
                        "candidate_sha": self.pair.candidate_sha}
            except BaseException as failure:
                phase = self._journal_phase()
                if phase == "committed":
                    # Never silently undo a committed switch; external reconcile
                    # reopens only after re-verifying exact candidate.
                    raise ServiceSwitchError(
                        "candidate committed but intake/recheck uncertain; reconcile externally"
                    ) from failure
                if phase == "fencing":
                    # The existing service has not been stopped or replaced.
                    # A drain timeout MUST NOT be turned into a service
                    # restart, especially if a claimed task is still active.
                    # Preserve STOP and journal for explicit operator reconcile.
                    raise ServiceSwitchError(
                        "cutover refused before systemd stop; baseline unchanged; "
                        "fence/STOP must be independently reconciled"
                    ) from failure
                try:
                    self._record("rollback_requested")
                    restored = self._restore_baseline()
                except BaseException as rollback_failure:
                    raise ServiceSwitchError(
                        "switch failed; rollback NOT verified; STOP retained"
                    ) from rollback_failure
                raise ServiceSwitchError(
                    f"switch failed; baseline restored at PID {restored}"
                ) from failure

    def rollback(self) -> dict[str, Any]:
        _external_only()
        self._preflight()
        with self._locks():
            if self._journal_phase() != "committed":
                raise ServiceSwitchError("rollback requires committed candidate")
            if self._unit_bytes() != self.new_unit:
                raise ServiceSwitchError("candidate unit not selected")
            self._require_no_stop()
            self._active_pid(self.pair.candidate)
            self._record("rollback_requested")
            try:
                pid = self._restore_baseline()
            except BaseException as exc:
                raise ServiceSwitchError(
                    "rollback incomplete; inspect/reconcile externally; STOP retained"
                ) from exc
            return {"status": "baseline_restored", "phase": "rolled_back",
                    "pid": pid, "baseline_sha": self.pair.rollback_sha}

    def reconcile(self) -> dict[str, Any]:
        """Explicit operator inspection and conservative recovery, never task replay."""
        _external_only()
        self._preflight()
        with self._locks():
            phase = self._journal_phase()
            if phase is None:
                raise ServiceSwitchError("no upgrade journal to reconcile")
            if phase == "committed":
                if self._unit_bytes() != self.new_unit:
                    raise ServiceSwitchError("committed candidate unit mismatch")
                pid = self._active_pid(self.pair.candidate)
                stop = self.pair.state / "EMERGENCY_STOP"
                if stop.is_file() and not stop.is_symlink():
                    self._unfence(self.pair.candidate, self.new_unit)
                else:
                    self._require_no_stop()
                return {"status": "candidate_verified", "pid": pid, "phase": phase}
            if phase == "rolled_back":
                if self._unit_bytes() != self.old_unit:
                    raise ServiceSwitchError("restored unit mismatch")
                pid = self._active_pid(self.pair.rollback)
                stop = self.pair.state / "EMERGENCY_STOP"
                if stop.is_file() and not stop.is_symlink():
                    self._unfence(self.pair.rollback, self.old_unit)
                else:
                    self._require_no_stop()
                return {"status": "baseline_verified", "pid": pid, "phase": phase}
            if phase == "fencing":
                # No service mutation happened yet. Wait out any original
                # in-flight work, then return baseline intake without a
                # pointless stop/start of the healthy production service.
                if self._unit_bytes() != self.old_unit:
                    raise ServiceSwitchError("pre-switch baseline unit changed")
                self._original_receipts()
                _write_stop(self.pair.state)
                self._drain()
                pid = self._active_pid(self.pair.rollback)
                self._record("rolled_back")
                self._unfence(self.pair.rollback, self.old_unit)
                return {"status": "baseline_fence_cleared", "pid": pid,
                        "phase": "rolled_back"}
            pid = self._restore_baseline()
            return {"status": "baseline_recovered", "pid": pid,
                    "phase": "rolled_back"}


def main(argv: list[str] | None = None) -> int:
    """Execute only from an independent Debian operator terminal."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "switch", "rollback", "reconcile"))
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    pair = ReleasePair(
        candidate=args.candidate_root, candidate_sha=args.candidate_sha,
        rollback=args.baseline_root, rollback_sha=args.baseline_sha,
        python=Path(sys.executable).resolve(),
        config=args.config, state=args.state,
    )
    try:
        output = getattr(PersistentServiceUpgrade(pair), args.action)()
    except (ServiceSwitchError, ValueError, OSError, AssertionError) as exc:
        print("Persistent Linux upgrade REFUSED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
