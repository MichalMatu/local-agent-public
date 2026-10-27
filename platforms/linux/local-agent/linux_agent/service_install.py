"""Owner-operated, reversible *staging only* of a persistent Linux user unit.

Staging NEVER starts, enables or restarts the service. A separate, future
installer acceptance gate must implement activation with an independently
accepted rollback. Do not call this component from an agent task.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Iterator

from linux_agent.foundation.process import atomic_write_text, fsync_directory
from linux_agent.agent import config_from_file, validate_config
from linux_agent.release_controller import ServiceSwitchError, verify_clean_exact_source
from linux_agent.service_plan import ReleasePair


UNIT_NAME = "local-agent-linux.service"
_JOURNAL = "linux-service-stage-v1.json"
_LOCK = "linux-service-stage-v1.lock"


def _no_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ServiceSwitchError("duplicate durable journal field")
        result[key] = value
    return result


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _private_dir(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise ServiceSwitchError("symlink in installation directory")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ServiceSwitchError("installation directory owner/type mismatch")
    if path.stat().st_mode & 0o077:
        raise ServiceSwitchError("installation directory is not owner-private")


@contextmanager
def _stage_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise ServiceSwitchError("unsafe stage lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _run_systemctl(*args: str, allow_inactive: bool = False) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceSwitchError("systemd inspection/reload unavailable") from exc
    if allow_inactive and result.returncode == 3 and result.stdout.strip() == "inactive":
        return "inactive"
    # systemctl is-enabled returns rc=1 for a correctly disabled unit.
    if args[0] == "is-enabled" and result.returncode in {0, 1} \
            and result.stdout.strip() in {"disabled", "static", "enabled"}:
        return result.stdout.strip()
    if result.returncode != 0:
        raise ServiceSwitchError("systemd " + args[0] + " failed or uncertain")
    return result.stdout.strip()


class PersistentUnitStage:
    """Prepare a persistent user unit, keep intake STOPPED, without activation."""

    def __init__(self, pair: ReleasePair):
        self.pair = pair
        self.home = Path.home()
        self.unit_dir = self.home / ".config/systemd/user"
        self.unit_file = self.unit_dir / UNIT_NAME
        self.journal = pair.state / _JOURNAL
        self.lock = pair.state / _LOCK

    def _preflight(self) -> str:
        self.pair.validate()
        # ReleasePair checks syntax and paths; verify actual clean, independent
        # source content before generating installable bytes.
        verify_clean_exact_source(self.pair.candidate, self.pair.candidate_sha)
        verify_clean_exact_source(self.pair.rollback, self.pair.rollback_sha)
        _private_dir(self.pair.state)
        validate_config(config_from_file(self.pair.config), self.pair.state)
        stop = self.pair.state / "EMERGENCY_STOP"
        if stop.is_symlink() or not stop.is_file():
            raise ServiceSwitchError("EMERGENCY_STOP must remain active while staging")
        if self.pair.state.is_symlink() or self.journal.is_symlink():
            raise ServiceSwitchError("unsafe installation journal")
        if self.unit_dir.is_symlink() or self.unit_file.is_symlink():
            raise ServiceSwitchError("unsafe user unit location")
        # Existing real user unit names are NEVER overwritten.
        return self.pair.unit_for(self.pair.candidate, self.pair.candidate_sha)

    def _expected(self, phase: str, unit: str) -> dict[str, object]:
        return {
            "version": 1, "phase": phase, "unit_name": UNIT_NAME,
            "candidate_sha": self.pair.candidate_sha,
            "rollback_sha": self.pair.rollback_sha,
            "unit_sha256": _digest(unit),
        }

    def _journal(self, unit: str) -> dict[str, object] | None:
        if self.journal.is_symlink():
            raise ServiceSwitchError("unsafe staging journal")
        if not self.journal.exists():
            return None
        if not self.journal.is_file() or self.journal.stat().st_size > 4096:
            raise ServiceSwitchError("unsafe staging journal bytes")
        try:
            data = json.loads(self.journal.read_text(), object_pairs_hook=_no_duplicate)
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            raise ServiceSwitchError("corrupt staging journal") from exc
        if not isinstance(data, dict) or data not in (
            self._expected("preparing", unit),
            self._expected("staged", unit),
            self._expected("withdrawing", unit),
            self._expected("withdrawn", unit),
        ):
            raise ServiceSwitchError("staging journal identity or phase mismatch")
        return data

    def _write_phase(self, phase: str, unit: str) -> None:
        atomic_write_text(
            self.journal,
            json.dumps(self._expected(phase, unit), sort_keys=True,
                       separators=(",", ":")) + "\n",
        )

    def preview(self) -> dict[str, str]:
        """Inspection only: no lock, journal, Git transport or systemctl edits."""
        unit = self._preflight()
        journal = self._journal(unit)
        if self.unit_file.exists() or journal is not None:
            raise ServiceSwitchError("existing unit or staging journal: inspect before staging")
        return {
            "status": "ready_to_stage_not_installed",
            "unit_path": str(self.unit_file),
            "unit_sha256": _digest(unit),
            "candidate_sha": self.pair.candidate_sha,
            "rollback_sha": self.pair.rollback_sha,
            "activation": "not_authorized_or_performed",
        }

    def stage(self) -> dict[str, str]:
        """Create only an unenabled unit after durable intent; no start/enable."""
        unit = self._preflight()
        # Check for a collision BEFORE creating any parent or lock file.
        if self.unit_file.exists() or self.unit_file.is_symlink() \
                or self._journal(unit) is not None:
            raise ServiceSwitchError("existing unit or staging journal: refusing overwrite")
        if self.home.is_symlink() or not self.home.is_dir() \
                or self.home.stat().st_uid != os.getuid():
            raise ServiceSwitchError("home directory owner/type mismatch")
        config = self.home / ".config"
        if config.is_symlink():
            raise ServiceSwitchError("user config directory is symlink")
        if not config.exists():
            config.mkdir(mode=0o700)
            fsync_directory(self.home)
        if not config.is_dir() or config.stat().st_uid != os.getuid():
            raise ServiceSwitchError("user config directory owner/type mismatch")
        systemd_dir = config / "systemd"
        if systemd_dir.is_symlink():
            raise ServiceSwitchError("systemd directory is symlink")
        if not systemd_dir.exists():
            systemd_dir.mkdir(mode=0o700)
            fsync_directory(config)
        if not systemd_dir.is_dir() or systemd_dir.stat().st_uid != os.getuid():
            raise ServiceSwitchError("systemd directory owner/type mismatch")
        _private_dir(self.unit_dir, create=True)
        with _stage_lock(self.lock):
            if self.unit_file.exists() or self.unit_file.is_symlink() or self._journal(unit):
                raise ServiceSwitchError("existing unit or staging journal: refusing overwrite")
            # A user unit of the SAME NAME can also be loaded from another
            # systemd search directory. Do not shadow such a unit.
            loaded = _run_systemctl(
                "show", UNIT_NAME, "--property=LoadState", "--value"
            )
            if loaded != "not-found":
                raise ServiceSwitchError("systemd already knows unit name; stage refused")
            self._write_phase("preparing", unit)  # fsync intent before installable file
            # Only create: neither existing files nor symlinks may be replaced.
            fd = os.open(self.unit_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(unit)
                fp.flush()
                os.fsync(fp.fileno())
            fsync_directory(self.unit_dir)
            # A reload does NOT start or enable any service.
            _run_systemctl("daemon-reload")
            self._confirm_staged_disabled()
            self._write_phase("staged", unit)
            return {"status": "staged_disabled", "unit_path": str(self.unit_file),
                    "candidate_sha": self.pair.candidate_sha}

    def _check_inactive_disabled(self) -> None:
        active = _run_systemctl("is-active", UNIT_NAME, allow_inactive=True)
        if active != "inactive":
            raise ServiceSwitchError("staged unit not verifiably inactive")
        enabled = _run_systemctl("is-enabled", UNIT_NAME)
        if enabled not in {"disabled", "static"}:
            raise ServiceSwitchError("unit was enabled; withdrawal refused")

    def _confirm_staged_disabled(self) -> None:
        """Systemd must load OUR exact staged unit, without activating it."""
        load = _run_systemctl(
            "show", UNIT_NAME, "--property=LoadState", "--value"
        )
        fragment = _run_systemctl(
            "show", UNIT_NAME, "--property=FragmentPath", "--value"
        )
        if load != "loaded" or fragment != str(self.unit_file):
            raise ServiceSwitchError("systemd did not load exact staged unit")
        self._check_inactive_disabled()

    def _confirm_withdrawn(self) -> None:
        """Do not mark removal complete while systemd retains a unit/process."""
        load = _run_systemctl(
            "show", UNIT_NAME, "--property=LoadState", "--value"
        )
        active = _run_systemctl(
            "show", UNIT_NAME, "--property=ActiveState", "--value"
        )
        fragment = _run_systemctl(
            "show", UNIT_NAME, "--property=FragmentPath", "--value"
        )
        if load != "not-found" or active != "inactive" or fragment:
            raise ServiceSwitchError("systemd still knows withdrawn unit")

    def reconcile(self) -> dict[str, str]:
        """Complete a known interrupted, still-disabled staging transition.

        Refuse changed unit bytes or evidence that systemd is running or
        enabling the unit. Never use reconciliation to activate intake.
        """
        unit = self._preflight()
        with _stage_lock(self.lock):
            data = self._journal(unit)
            if data is None or data["phase"] not in {"preparing", "withdrawing"}:
                raise ServiceSwitchError("no interrupted stage to reconcile")
            phase = data["phase"]
            if self.unit_file.is_symlink():
                raise ServiceSwitchError("unsafe staged unit path")
            if self.unit_file.exists():
                if not self.unit_file.is_file() or self.unit_file.read_text() != unit:
                    raise ServiceSwitchError("staged unit contents changed")
                self._check_inactive_disabled()
            if phase == "preparing":
                if self.unit_file.exists():
                    _run_systemctl("daemon-reload")
                    self._confirm_staged_disabled()
                    self._write_phase("staged", unit)
                    return {"status": "staged_disabled_recovered"}
                _run_systemctl("daemon-reload")
                self._confirm_withdrawn()
                self._write_phase("withdrawn", unit)
                return {"status": "withdrawn_no_unit_was_created"}
            if self.unit_file.exists():
                self.unit_file.unlink()
                fsync_directory(self.unit_dir)
            _run_systemctl("daemon-reload")
            self._confirm_withdrawn()
            self._write_phase("withdrawn", unit)
            return {"status": "withdrawn_recovered"}

    def withdraw(self) -> dict[str, str]:
        """Remove only the EXACT bytes staged by us, when unit is inactive.

        Interrupted stages are reconciled separately under a durable journal.
        The original journal remains evidence, including on failure.
        """
        unit = self._preflight()
        with _stage_lock(self.lock):
            data = self._journal(unit)
            if data is None or data["phase"] != "staged":
                raise ServiceSwitchError("not in safely staged phase")
            if self.unit_file.is_symlink() or not self.unit_file.is_file():
                raise ServiceSwitchError("staged unit missing or unsafe")
            if self.unit_file.read_text() != unit:
                raise ServiceSwitchError("staged unit contents changed")
            # Persist the intent before removing a recoverable unit.
            self._check_inactive_disabled()
            self._write_phase("withdrawing", unit)
            self.unit_file.unlink()
            fsync_directory(self.unit_dir)
            _run_systemctl("daemon-reload")
            self._confirm_withdrawn()
            self._write_phase("withdrawn", unit)
            return {"status": "withdrawn", "journal_retained": str(self.journal)}

def main(argv: list[str] | None = None) -> int:
    """External operator CLI. No 'start', 'enable' or 'install' action exists."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "stage", "withdraw", "reconcile"))
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
        python=Path(sys.executable).resolve(), config=args.config, state=args.state,
    )
    try:
        stage = PersistentUnitStage(pair)
        result = getattr(stage, args.action)()
    except (ServiceSwitchError, ValueError, OSError) as exc:
        print(f"Linux service staging REFUSED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
