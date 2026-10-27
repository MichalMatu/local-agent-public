"""Fail-closed layout for a Conversation Fabric development lab beside production.

Stage 3 intentionally does not start a second Local Agent executor. It creates a
small synthetic-only namespace for workflow/browser experiments and makes
production path overlap a validation error before any filesystem mutation.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from local_agent.foundation.process import atomic_write_text
from local_agent.platform.macos_launchd import default_launch_agent_path

LAB_SCHEMA_VERSION = 1
LAB_MODE = "synthetic-only"
LAB_MARKER_NAME = "lab.json"
PROTECTED_OPERATIONAL_BRANCHES = ("chat-bridge-state", "operator-control")


@dataclass(frozen=True, slots=True)
class DevLabLayout:
    home: Path
    checkout: Path
    root: Path
    state_dir: Path
    repositories_dir: Path
    browser_profile_dir: Path
    logs_dir: Path
    fixtures_dir: Path
    marker_path: Path
    production_checkout: Path

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": LAB_SCHEMA_VERSION,
            "mode": LAB_MODE,
            "capabilities": {
                "executor_enabled": False,
                "remote_control_enabled": False,
                "real_chrome_profile_enabled": False,
                "native_host_registration_enabled": False,
            },
            "protected_operational_branches": list(PROTECTED_OPERATIONAL_BRANCHES),
            "paths": {
                "checkout": str(self.checkout),
                "root": str(self.root),
                "state": str(self.state_dir),
                "repositories": str(self.repositories_dir),
                "browser_profile": str(self.browser_profile_dir),
                "logs": str(self.logs_dir),
                "fixtures": str(self.fixtures_dir),
            },
            "production_checkout": str(self.production_checkout),
        }


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    left = _resolved(first)
    right = _resolved(second)
    if left == right or left in right.parents or right in left.parents:
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _require_descendant(path: Path, root: Path, *, field: str) -> None:
    resolved_path = _resolved(path)
    resolved_root = _resolved(root)
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise ValueError(f"DEV lab {field} must stay below lab root: {resolved_path}")


def protected_production_paths(
    *,
    home: Path,
    production_checkout: Path,
) -> dict[str, Path]:
    home = _resolved(home)
    production_checkout = _resolved(production_checkout)
    application_support = home / "Library" / "Application Support"
    chrome_root = application_support / "Google" / "Chrome"
    return {
        "checkout": production_checkout,
        "state": application_support / "local-agent",
        "workspace": home / "agent-workspace",
        "launch_agent": default_launch_agent_path(home),
        "stdout_log": home / "Library" / "Logs" / "local-agent.log",
        "stderr_log": home / "Library" / "Logs" / "local-agent-error.log",
        "chrome_profile_root": chrome_root,
    }


def build_dev_lab_layout(
    *,
    home: Path | None = None,
    root: Path | None = None,
    checkout: Path | None = None,
    production_checkout: Path | None = None,
) -> DevLabLayout:
    resolved_home = _resolved(home or Path.home())
    resolved_root = _resolved(
        root
        or resolved_home
        / "Library"
        / "Application Support"
        / "local-agent-dev"
    )
    resolved_checkout = _resolved(checkout or resolved_home / "local-agent-dev")
    resolved_production_checkout = _resolved(
        production_checkout or resolved_home / "local-agent"
    )
    layout = DevLabLayout(
        home=resolved_home,
        checkout=resolved_checkout,
        root=resolved_root,
        state_dir=resolved_root / "state",
        repositories_dir=resolved_root / "repositories",
        browser_profile_dir=resolved_root / "browser-profile",
        logs_dir=resolved_root / "logs",
        fixtures_dir=resolved_root / "fixtures",
        marker_path=resolved_root / LAB_MARKER_NAME,
        production_checkout=resolved_production_checkout,
    )
    validate_dev_lab_layout(layout)
    return layout


def validate_dev_lab_layout(layout: DevLabLayout) -> None:
    if _paths_overlap(layout.root, layout.checkout):
        raise ValueError(
            "DEV lab state root and checkout must be disjoint so checkout cleanup "
            "cannot delete persistent lab state"
        )

    for field, path in (
        ("state", layout.state_dir),
        ("repositories", layout.repositories_dir),
        ("browser profile", layout.browser_profile_dir),
        ("logs", layout.logs_dir),
        ("fixtures", layout.fixtures_dir),
        ("marker", layout.marker_path),
    ):
        _require_descendant(path, layout.root, field=field)

    protected = protected_production_paths(
        home=layout.home,
        production_checkout=layout.production_checkout,
    )
    for dev_name, dev_path in (("root", layout.root), ("checkout", layout.checkout)):
        for prod_name, prod_path in protected.items():
            if _paths_overlap(dev_path, prod_path):
                raise ValueError(
                    f"DEV lab {dev_name} {dev_path} overlaps production "
                    f"{prod_name} {prod_path}"
                )


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _lab_directories(layout: DevLabLayout) -> tuple[Path, ...]:
    return (
        layout.state_dir,
        layout.repositories_dir,
        layout.browser_profile_dir,
        layout.logs_dir,
        layout.fixtures_dir,
    )


def _validate_existing_lab_entries(layout: DevLabLayout) -> None:
    """Reject unsafe pre-existing entries before init mutates the namespace."""
    marker = layout.marker_path
    if marker.is_symlink():
        raise RuntimeError(f"DEV lab marker must not be a symlink: {marker}")
    if marker.exists() and not marker.is_file():
        raise RuntimeError(f"DEV lab marker is not a regular file: {marker}")

    for path in _lab_directories(layout):
        if path.is_symlink():
            raise RuntimeError(f"DEV lab directory must not be a symlink: {path}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"DEV lab directory path is not a directory: {path}")


def _validate_adoptable_browser_profile(layout: DevLabLayout) -> None:
    root = layout.root
    if not root.is_dir():
        raise RuntimeError(f"browser profile adoption requires an existing DEV lab root: {root}")
    _validate_existing_lab_entries(layout)
    if layout.marker_path.exists() or layout.marker_path.is_symlink():
        raise RuntimeError("browser profile adoption requires an unmarked DEV lab root")

    entries = tuple(root.iterdir())
    if len(entries) != 1 or entries[0] != layout.browser_profile_dir:
        raise RuntimeError(
            "browser profile adoption requires the unmarked root to contain only browser-profile"
        )

    profile = layout.browser_profile_dir
    if not profile.is_dir() or profile.is_symlink():
        raise RuntimeError("browser profile adoption requires a regular browser-profile directory")

    local_state = profile / "Local State"
    if local_state.is_symlink() or not local_state.is_file():
        raise RuntimeError("browser profile adoption requires a regular Local State file")

    usable_profiles = []
    for child in profile.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        preferences = child / "Preferences"
        if preferences.is_file() and not preferences.is_symlink():
            usable_profiles.append(child)
    if not usable_profiles:
        raise RuntimeError("browser profile adoption requires at least one profile Preferences file")

    for path in profile.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(
                f"browser profile adoption refuses symbolic links inside browser-profile: {path}"
            )


def adopt_existing_browser_profile(layout: DevLabLayout) -> dict[str, Any]:
    """Adopt one isolated browser profile without copying or modifying its browser data.

    This is deliberately narrower than normal initialization: the unmarked lab root
    must contain exactly one pre-existing ``browser-profile`` directory with a
    recognizable Chromium profile shape and no symbolic links. The operation only
    creates the missing inert lab directories plus the exact layout marker.
    """
    validate_dev_lab_layout(layout)
    _validate_adoptable_browser_profile(layout)
    expected = layout.manifest()

    for path in _lab_directories(layout):
        path.mkdir(parents=True, exist_ok=True)
    atomic_write_text(layout.marker_path, _json_text(expected))
    return expected


def rebind_dev_lab_checkout(layout: DevLabLayout) -> dict[str, Any]:
    """Atomically rebind an unused healthy lab marker to one new isolated checkout."""
    validate_dev_lab_layout(layout)
    _validate_existing_lab_entries(layout)
    if layout.checkout.is_symlink() or not layout.checkout.is_dir():
        raise RuntimeError("checkout rebind requires an existing regular checkout directory")
    if not layout.marker_path.is_file() or layout.marker_path.is_symlink():
        raise RuntimeError("checkout rebind requires a regular existing DEV lab marker")

    try:
        actual = json.loads(layout.marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("checkout rebind requires a valid existing DEV lab marker") from exc
    if not isinstance(actual, dict):
        raise RuntimeError("checkout rebind requires a valid existing DEV lab marker")

    expected = layout.manifest()
    if actual == expected:
        return expected

    paths = actual.get("paths")
    old_checkout_value = paths.get("checkout") if isinstance(paths, dict) else None
    if not isinstance(old_checkout_value, str) or not old_checkout_value.strip():
        raise RuntimeError("checkout rebind requires a canonical existing DEV lab marker")
    old_layout = build_dev_lab_layout(
        home=layout.home,
        root=layout.root,
        checkout=Path(old_checkout_value),
        production_checkout=layout.production_checkout,
    )
    if actual != old_layout.manifest():
        raise RuntimeError("checkout rebind requires a canonical existing DEV lab marker")
    old_status = dev_lab_status(old_layout)
    if not old_status["healthy"]:
        raise RuntimeError("checkout rebind requires a healthy existing DEV lab")

    for path in (
        layout.state_dir,
        layout.repositories_dir,
        layout.logs_dir,
        layout.fixtures_dir,
    ):
        if any(path.iterdir()):
            raise RuntimeError("checkout rebind requires inert lab state")

    atomic_write_text(layout.marker_path, _json_text(expected))
    return expected


def initialize_dev_lab(layout: DevLabLayout) -> dict[str, Any]:
    """Create only the inert DEV namespace; never clone, launch or register Chrome."""
    validate_dev_lab_layout(layout)
    expected = layout.manifest()

    if layout.root.exists():
        if not layout.root.is_dir():
            raise RuntimeError(f"DEV lab root is not a directory: {layout.root}")
        _validate_existing_lab_entries(layout)
        existing_entries = tuple(layout.root.iterdir())
        if existing_entries and not layout.marker_path.is_file():
            raise RuntimeError(
                "refusing to adopt a non-empty DEV lab root without an existing lab marker: "
                f"{layout.root}"
            )

    if layout.marker_path.is_file():
        try:
            existing = json.loads(layout.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid existing DEV lab marker: {layout.marker_path}") from exc
        if existing != expected:
            raise RuntimeError(
                "existing DEV lab marker does not match requested layout; refusing mutation"
            )

    layout.root.mkdir(parents=True, exist_ok=True)
    _validate_existing_lab_entries(layout)
    for path in _lab_directories(layout):
        path.mkdir(parents=True, exist_ok=True)
    atomic_write_text(layout.marker_path, _json_text(expected))
    return expected


def dev_lab_status(layout: DevLabLayout) -> dict[str, Any]:
    validate_dev_lab_layout(layout)
    expected = layout.manifest()
    problems: list[str] = []

    if not layout.marker_path.is_file() or layout.marker_path.is_symlink():
        problems.append("marker_missing_or_not_regular")
        actual: Any = None
    else:
        try:
            actual = json.loads(layout.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            actual = None
            problems.append("marker_invalid")

    if actual is not None and actual != expected:
        problems.append("marker_layout_mismatch")

    for path in _lab_directories(layout):
        if not path.is_dir() or path.is_symlink():
            problems.append(f"directory_missing_or_unsafe:{path.name}")

    return {
        "healthy": not problems,
        "problems": problems,
        "manifest": expected,
        "marker": str(layout.marker_path),
    }


def _path_arg(value: str | None) -> Path | None:
    return None if value is None else Path(value)


def _build_from_args(args: argparse.Namespace) -> DevLabLayout:
    return build_dev_lab_layout(
        home=_path_arg(args.home),
        root=_path_arg(args.root),
        checkout=_path_arg(args.checkout),
        production_checkout=_path_arg(args.production_checkout),
    )


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, initialize, adopt, rebind or inspect the synthetic-only "
            "Conversation Fabric DEV lab"
        )
    )
    parser.add_argument(
        "command",
        choices=("plan", "init", "adopt-profile", "rebind-checkout", "status"),
    )
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--checkout")
    parser.add_argument("--production-checkout")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        layout = _build_from_args(args)
        if args.command == "plan":
            payload = layout.manifest()
            return_code = 0
        elif args.command == "init":
            payload = initialize_dev_lab(layout)
            return_code = 0
        elif args.command == "adopt-profile":
            payload = adopt_existing_browser_profile(layout)
            return_code = 0
        elif args.command == "rebind-checkout":
            payload = rebind_dev_lab_checkout(layout)
            return_code = 0
        else:
            payload = dev_lab_status(layout)
            return_code = 0 if payload["healthy"] else 1
        print(_json_text(payload), end="")
        return return_code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DEV lab error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
