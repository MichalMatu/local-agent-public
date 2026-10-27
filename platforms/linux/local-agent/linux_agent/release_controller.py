"""Independent, LAB-ONLY systemd user release switch / rollback controller.

This is an external operator component, never invoked from an agent task.
Only uniquely named disposable units under XDG_RUNTIME_DIR/systemd/user are
supported. It cannot install, enable or replace a permanent Linux Agent unit.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import tempfile
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Iterator

from linux_agent.foundation.process import atomic_write_text
from linux_agent.service_plan import ReleasePair

_LAB = re.compile(r"^local-agent-linux-lab-[a-f0-9]{12,32}\.service$")


class ServiceSwitchError(RuntimeError):
    """State unknown or rollback failed: leave evidence and fail closed."""


def _run(*args: str) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=35,
                                check=False, env={**os.environ, "SYSTEMD_PAGER": ""})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceSwitchError(f"service controller failed: {args[0]}") from exc
    if result.returncode:
        raise ServiceSwitchError(f"service command failed: {args[0]} {args[1] if len(args) > 1 else ''} rc={result.returncode}")
    return result.stdout.strip()


def verify_clean_exact_source(root: Path, sha: str) -> None:
    """A SHA-named directory alone is NOT a verified immutable release."""
    if root.is_symlink() or root.name != sha:
        raise ServiceSwitchError("release root/sha mismatch")
    if _run("git", "-C", str(root), "rev-parse", "HEAD") != sha:
        raise ServiceSwitchError("release checkout HEAD differs from claimed SHA")
    if _run("git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"):
        raise ServiceSwitchError("release contains modified or untracked source")


@contextmanager
def _controller_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise ServiceSwitchError("controller lock is symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class DisposableUserServiceController:
    """Native staging and rollback rehearsal without persistent installation."""

    def __init__(self, pair: ReleasePair, unit_name: str):
        if not _LAB.fullmatch(unit_name):
            raise ValueError("only unique lab unit names are allowed")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime or Path(runtime).is_symlink() or not Path(runtime).is_dir():
            raise ServiceSwitchError("a real user systemd runtime directory is required")
        if Path(runtime).stat().st_uid != os.getuid():
            raise ServiceSwitchError("runtime directory belongs to another user")
        self.pair = pair
        self.unit_name = unit_name
        self.unit_dir = Path(runtime) / "systemd" / "user"
        self.unit_file = self.unit_dir / unit_name
        # Durable, outside both exact-SHA release trees and never shared with
        # the Mac/Pi service controller or another laboratory unit.
        self.transaction_path = pair.state / ("release-switch-" + unit_name + ".json")

    def _guard(self) -> None:
        self.pair.validate()
        # This prototype is deliberately NOT a production release tool.
        # Accept only a wholly disposable fixture with its own credentials,
        # identity, Git authority, source roots and durable ledgers.
        sandbox = self.pair.state.parent
        if (sandbox.is_symlink() or sandbox.parent.resolve() !=
                Path(tempfile.gettempdir()).resolve() or
                not sandbox.name.startswith("linux-agent-release-") or
                self.pair.config != sandbox / "config.json" or
                self.pair.candidate.parent != sandbox / "releases" or
                self.pair.rollback.parent != sandbox / "releases"):
            raise ServiceSwitchError("only isolated temporary Linux release fixtures allowed")
        try:
            config = json.loads(self.pair.config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceSwitchError("invalid lab-only configuration") from exc
        remote = config.get("repository") if isinstance(config, dict) else None
        if (not isinstance(remote, str) or
                remote != str(sandbox / "offline-control.git") or
                not (sandbox / "offline-control.git/HEAD").is_file() or
                not str(config.get("host_id", "")).startswith("release-lab-") or
                config.get("branch") != "agent-control"):
            raise ServiceSwitchError("lab must use isolated offline Git and identity")
        verify_clean_exact_source(self.pair.candidate, self.pair.candidate_sha)
        verify_clean_exact_source(self.pair.rollback, self.pair.rollback_sha)
        if self.unit_dir.is_symlink():
            raise ServiceSwitchError("user unit dir may not be a symlink")
        self.unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.unit_dir.stat().st_uid != os.getuid():
            raise ServiceSwitchError("user unit dir owner mismatch")
        if self.unit_file.is_symlink():
            raise ServiceSwitchError("user unit file may not be a symlink")

    def _journal(self) -> dict | None:
        path = self.transaction_path
        if path.is_symlink():
            raise ServiceSwitchError("release transaction journal is symlink")
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > 4096:
            raise ServiceSwitchError("release transaction journal is invalid")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceSwitchError("release transaction journal is corrupt") from exc
        allowed = {
            "baseline_starting", "baseline_active", "candidate_requested",
            "candidate_unverified", "candidate_active", "rollback_requested",
            "cleanup_requested", "cleaned",
        }
        expected = {
            "version": 1, "unit": self.unit_name,
            "candidate_sha": self.pair.candidate_sha,
            "rollback_sha": self.pair.rollback_sha,
        }
        if (not isinstance(state, dict) or set(state) != set(expected) | {"phase"}
                or any(state.get(key) != value for key, value in expected.items())
                or state.get("phase") not in allowed):
            raise ServiceSwitchError("release transaction identity/state mismatch")
        return state

    def _record(self, phase: str) -> None:
        # Validate an existing transaction before replacing its durable bytes.
        self._journal()
        payload = {
            "version": 1, "unit": self.unit_name,
            "candidate_sha": self.pair.candidate_sha,
            "rollback_sha": self.pair.rollback_sha, "phase": phase,
        }
        atomic_write_text(self.transaction_path, json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ) + "\n")

    def _require_phase(self, phase: str) -> None:
        journal = self._journal()
        if journal is None or journal["phase"] != phase:
            raise ServiceSwitchError("release transaction is not in " + phase)

    def _unit_bytes(self) -> str | None:
        if not self.unit_file.exists():
            return None
        return self.unit_file.read_text()

    def _write(self, content: str) -> None:
        atomic_write_text(self.unit_file, content)
        _run("systemctl", "--user", "daemon-reload")

    def _show(self, field: str) -> str:
        return _run("systemctl", "--user", "show", self.unit_name,
                    "--property=" + field, "--value")

    def _check_active(self, release: Path) -> int:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._show("ActiveState") == "active":
                try:
                    pid = int(self._show("MainPID"))
                    cg = self._show("ControlGroup")
                    unit_path = self._show("FragmentPath")
                    if pid > 1 and cg and unit_path == str(self.unit_file):
                        env = Path(f"/proc/{pid}/environ").read_bytes()
                        from linux_agent.service_plan import source_pythonpath
                        expected = ("PYTHONPATH=" + source_pythonpath(release)).encode()
                        if expected in env.split(b"\0"):
                            return pid
                except (ValueError, FileNotFoundError, ProcessLookupError):
                    pass
            time.sleep(0.15)
        raise ServiceSwitchError("unit never reached expected active SHA/cgroup")

    def _stop(self) -> None:
        _run("systemctl", "--user", "stop", self.unit_name)
        if self._show("ActiveState") not in {"inactive", "failed"}:
            raise ServiceSwitchError("unit did not stop")

    def _start(self, release: Path) -> int:
        # A newly written unit may not have a loaded failure record yet.
        # `reset-failed` returns nonzero for such units on Debian 13, even
        # though starting the unit itself is valid. Reset only a real failure.
        if self._show("ActiveState") == "failed":
            _run("systemctl", "--user", "reset-failed", self.unit_name)
        _run("systemctl", "--user", "start", self.unit_name)
        return self._check_active(release)

    def stage_baseline(self) -> int:
        """Create ONLY a disposable rollback lab unit; no enable/autostart."""
        self._guard()
        with _controller_lock(self.pair.state / ("release-switch-" + self.unit_name + ".lock")):
            if self._unit_bytes() is not None or self._journal() is not None:
                raise ServiceSwitchError("refusing to overwrite existing unit or journal")
            self._record("baseline_starting")
            self._write(self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha))
            pid = self._start(self.pair.rollback)
            self._record("baseline_active")
            return pid

    def switch_to_candidate(self) -> int:
        """Stop rollback, start candidate; auto-reinstate rollback on failure."""
        self._guard()
        with _controller_lock(self.pair.state / ("release-switch-" + self.unit_name + ".lock")):
            self._require_phase("baseline_active")
            rollback_unit = self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha)
            if self._unit_bytes() != rollback_unit:
                raise ServiceSwitchError("baseline unit changed: no cutover")
            self._check_active(self.pair.rollback)
            # Record intent durably BEFORE the first disruptive operation.
            # Any interruption leaves an unaccepted phase which a separate
            # controller process can reconcile without command replay.
            self._record("candidate_requested")
            try:
                self._stop()
                self._record("candidate_unverified")
                self._write(self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha))
                pid = self._start(self.pair.candidate)
                self._record("candidate_active")
                return pid
            except BaseException as original:
                try:
                    self._record("rollback_requested")
                    if self._unit_bytes() not in (rollback_unit, self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha)):
                        raise ServiceSwitchError("unexpected unit during rollback")
                    self._stop()
                    self._write(rollback_unit)
                    self._start(self.pair.rollback)
                    self._record("baseline_active")
                except BaseException as rollback_error:
                    raise ServiceSwitchError("candidate failed and rollback NOT verified") from rollback_error
                raise ServiceSwitchError("candidate failed; original service rollback restored") from original

    def rollback_candidate(self) -> int:
        """Externally reverse a candidate. Never changes claims/transport/results."""
        self._guard()
        with _controller_lock(self.pair.state / ("release-switch-" + self.unit_name + ".lock")):
            self._require_phase("candidate_active")
            candidate_unit = self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha)
            if self._unit_bytes() != candidate_unit:
                raise ServiceSwitchError("candidate unit changed: rollback refused")
            self._check_active(self.pair.candidate)
            self._record("rollback_requested")
            self._stop()
            self._write(self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha))
            pid = self._start(self.pair.rollback)
            self._record("baseline_active")
            return pid

    def recover_unverified(self) -> int | None:
        """External reconciliation of an interrupted unaccepted switch.

        A verified candidate remains selected until explicit rollback.
        No task claim, durable result or Git transport is ever mutated.
        """
        self._guard()
        with _controller_lock(self.pair.state / ("release-switch-" + self.unit_name + ".lock")):
            journal = self._journal()
            if journal is None:
                raise ServiceSwitchError("no release transaction to recover")
            phase = journal["phase"]
            if phase == "baseline_active":
                self._check_active(self.pair.rollback)
                return None
            if phase == "candidate_active":
                self._check_active(self.pair.candidate)
                return None
            if phase in {"cleaned", "cleanup_requested"}:
                raise ServiceSwitchError("cleanup transaction needs explicit completion")
            if phase not in {
                "baseline_starting", "candidate_requested",
                "candidate_unverified", "rollback_requested",
            }:
                raise ServiceSwitchError("unknown interrupted release phase")
            known = {
                None,
                self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha),
                self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha),
            }
            installed = self._unit_bytes()
            if installed not in known:
                raise ServiceSwitchError("unexpected unit: recovery blocked")
            self._record("rollback_requested")
            if installed is not None:
                self._stop()
            self._write(self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha))
            pid = self._start(self.pair.rollback)
            self._record("baseline_active")
            return pid

    def cleanup_lab(self) -> None:
        """Stop and remove only our unit; retain the durable terminal journal."""
        self._guard()
        with _controller_lock(self.pair.state / ("release-switch-" + self.unit_name + ".lock")):
            if self._unit_bytes() not in {
                self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha),
                self.pair.unit_for(self.pair.rollback, self.pair.rollback_sha),
                None,
            }:
                raise ServiceSwitchError("unexpected unit content; do not delete")
            journal = self._journal()
            if journal is None or journal["phase"] not in {
                "baseline_active", "candidate_active",
                "cleanup_requested", "cleaned",
            }:
                raise ServiceSwitchError("unreconciled release: recover before cleanup")
            if journal["phase"] == "cleaned":
                if self._unit_bytes() is not None:
                    raise ServiceSwitchError("cleaned journal but unexpected live unit")
                return
            self._record("cleanup_requested")
            if self._unit_bytes() is not None:
                _run("systemctl", "--user", "stop", self.unit_name)
                self.unit_file.unlink()
            _run("systemctl", "--user", "daemon-reload")
            self._record("cleaned")
