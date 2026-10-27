from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from local_agent.host_ops.capabilities.local.browser.models import BrowserInspection

managed_session = importlib.import_module("local_agent.host_ops.capabilities.local.browser.managed_session")


class EmptyInspector:
    def inspect(self, *, endpoints=(), timeout_seconds=5.0, limits=None):
        del endpoints, timeout_seconds, limits
        return BrowserInspection(processes=(), endpoints=(), warnings=())


def _executable(tmp_path: Path) -> Path:
    path = tmp_path / "chrome"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_prepare_download_dir_creates_private_directory(tmp_path: Path) -> None:
    path = tmp_path / "bridge-inbox"

    result = managed_session._prepare_download_dir(str(path))

    assert result == path.resolve()
    assert result.is_dir()
    assert result.stat().st_mode & 0o777 == 0o700


def test_prepare_download_dir_rejects_relative_file_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        managed_session._prepare_download_dir("relative")

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        managed_session._prepare_download_dir(str(file_path))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        managed_session._prepare_download_dir(str(link))


def test_download_preferences_preserve_existing_profile_preferences(tmp_path: Path) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    default_dir = profile / "Default"
    default_dir.mkdir()
    preferences = default_dir / "Preferences"
    preferences.write_text(
        json.dumps(
            {
                "browser": {"check_default_browser": False},
                "download": {"open_pdf_in_system_reader": True},
            }
        ),
        encoding="utf-8",
    )
    downloads = managed_session._prepare_download_dir(str(tmp_path / "downloads"))

    managed_session._configure_download_preferences(profile, downloads)

    payload = json.loads(preferences.read_text(encoding="utf-8"))
    assert payload["browser"] == {"check_default_browser": False}
    assert payload["download"] == {
        "default_directory": str(downloads),
        "directory_upgrade": True,
        "open_pdf_in_system_reader": True,
        "prompt_for_download": False,
    }
    assert preferences.stat().st_mode & 0o777 == 0o600


def test_download_preferences_fail_closed_on_invalid_or_symlink_file(tmp_path: Path) -> None:
    profile = managed_session._initialize_managed_profile(str(tmp_path / "profile"))
    downloads = managed_session._prepare_download_dir(str(tmp_path / "downloads"))
    default_dir = profile / "Default"
    default_dir.mkdir()
    preferences = default_dir / "Preferences"
    preferences.write_text("{broken", encoding="utf-8")

    with pytest.raises(managed_session.ManagedBrowserSessionError, match="invalid JSON"):
        managed_session._configure_download_preferences(profile, downloads)

    preferences.unlink()
    target = tmp_path / "preferences-target"
    target.write_text("{}", encoding="utf-8")
    preferences.symlink_to(target)
    with pytest.raises(managed_session.ManagedBrowserSessionError, match="symlink"):
        managed_session._configure_download_preferences(profile, downloads)


def test_start_applies_download_policy_before_browser_launch(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    downloads = tmp_path / "bridge-inbox"
    executable = _executable(tmp_path)
    controller = managed_session.ManagedBrowserSessionController(
        EmptyInspector(),
        launcher=lambda _argv: 0,
    )

    with pytest.raises(managed_session.ManagedBrowserSessionError, match="invalid pid"):
        controller.start(
            str(profile),
            str(executable),
            download_dir=str(downloads),
            timeout_seconds=2,
        )

    payload = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert payload["download"]["default_directory"] == str(downloads.resolve())
    assert payload["download"]["prompt_for_download"] is False
