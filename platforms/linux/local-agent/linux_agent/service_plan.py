"""Pure Debian systemd user-unit planning; never installs/enables/restarts services.

The candidate and rollback release roots must be independent immutable
exact-SHA directories. Rendering a unit does NOT verify installed releases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE = re.compile(r"^/[A-Za-z0-9_./+@-]+$")


def source_layout(root: Path) -> tuple[Path, Path]:
    """Exact-source import root/entrypoint in old or new release layout.

    An immutable legacy rollback may use the old layout. New releases
    require their Debian adapter and shared core from the same root.
    """
    adapter = root / "platforms/linux/local-agent"
    legacy = adapter / "linux_agent/__main__.py"
    core = root / "platforms/linux/core/linux_agent/__main__.py"
    # Do not treat a malformed new release as an old rollback. A symlink
    # cannot be silently accepted as a legacy entrypoint fallback.
    for item in (
        root / "platforms", root / "platforms/linux",
        adapter, adapter / "linux_agent",
        root / "platforms/linux/core", core.parent, core, legacy,
    ):
        if item.is_symlink():
            raise ValueError("Linux release source path must not contain symlinks")
    if core.is_file():
        bridge = adapter / "linux_agent/__init__.py"
        if not bridge.is_file() or bridge.is_symlink():
            raise ValueError("shared core requires the source-local Debian adapter")
        return adapter, core
    if legacy.is_file():
        return adapter, legacy
    raise ValueError("release missing independent Linux Agent source")


def source_pythonpath(root: Path) -> str:
    adapter, entrypoint = source_layout(root)
    if entrypoint.is_relative_to(root / "platforms/linux/core"):
        return str(adapter) + ":" + str(root / "platforms/linux/core")
    return str(adapter)



def _safe_absolute(path: Path, name: str) -> str:
    value = str(path)
    if not path.is_absolute() or not _SAFE.fullmatch(value) or ".." in path.parts:
        raise ValueError(f"unsafe {name} absolute path")
    if path.is_symlink():
        raise ValueError(f"{name} may not be a symlink")
    return value


@dataclass(frozen=True)
class ReleasePair:
    candidate: Path
    candidate_sha: str
    rollback: Path
    rollback_sha: str
    python: Path
    config: Path
    state: Path

    def validate(self) -> None:
        if not _SHA.fullmatch(self.candidate_sha) or not _SHA.fullmatch(self.rollback_sha):
            raise ValueError("releases must be identified by full lowercase SHA-1")
        if self.candidate_sha == self.rollback_sha:
            raise ValueError("rollback must be a distinct accepted revision")
        for label, root, sha in (
            ("candidate", self.candidate, self.candidate_sha),
            ("rollback", self.rollback, self.rollback_sha),
        ):
            _safe_absolute(root, label)
            if root.name != sha:
                raise ValueError(f"{label} directory must have its exact revision name")
            try:
                source_layout(root)
            except ValueError as exc:
                raise ValueError(f"{label} must contain independent Linux Agent source") from exc
        for label, path in (
            ("python", self.python), ("config", self.config), ("state", self.state),
        ):
            _safe_absolute(path, label)
        if not self.python.is_file():
            raise ValueError("reviewed Python environment missing")
        if not self.config.is_file() or self.config.stat().st_mode & 0o077:
            raise ValueError("private installation configuration missing or too permissive")
        if not self.state.is_dir() or self.state.stat().st_mode & 0o077:
            raise ValueError("private installation state missing or too permissive")

    def unit_for(self, root: Path, sha: str) -> str:
        self.validate()
        if (root, sha) not in (
            (self.candidate, self.candidate_sha), (self.rollback, self.rollback_sha)
        ):
            raise ValueError("unrecognized release target")
        source = source_pythonpath(root)
        return (
            "[Unit]\n"
            f"Description=Local Agent Linux Debian user service ({sha[:12]})\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "StartLimitIntervalSec=180\n"
            "StartLimitBurst=3\n"
            "\n[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={root}\n"
            f"Environment=PYTHONPATH={source}\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\n"
            f"ExecStart={self.python} -B -m linux_agent --config {self.config} --state {self.state} serve\n"
            "UMask=0077\n"
            "KillMode=control-group\n"
            "TimeoutStopSec=40\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n[Install]\n"
            "WantedBy=default.target\n"
        )

    def dry_run(self) -> dict[str, str]:
        """Two independently selectable units; caller must test external rollback."""
        return {
            "status": "plan_only_no_host_mutations",
            "unit_name": "local-agent-linux.service",
            "candidate_sha": self.candidate_sha,
            "rollback_sha": self.rollback_sha,
            "candidate_unit": self.unit_for(self.candidate, self.candidate_sha),
            "rollback_unit": self.unit_for(self.rollback, self.rollback_sha),
        }
