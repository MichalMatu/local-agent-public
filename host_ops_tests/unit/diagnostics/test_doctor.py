from __future__ import annotations

from pathlib import Path

from local_agent.host_ops.core.diagnostics import CheckStatus, CommandRequirement, run_doctor


def test_doctor_warns_for_optional_missing_command(tmp_path: Path) -> None:
    report = run_doctor(
        command_requirements=(CommandRequirement("adb", "Android unavailable"),),
        config_path=tmp_path / "missing.toml",
        command_resolver=lambda _name: None,
    )

    by_name = {check.name: check for check in report.checks}
    assert by_name["command:adb"].status is CheckStatus.WARN
    assert report.ok


def test_doctor_fails_for_required_missing_command(tmp_path: Path) -> None:
    report = run_doctor(
        command_requirements=(CommandRequirement("ssh", "SSH required", required=True),),
        config_path=tmp_path / "missing.toml",
        command_resolver=lambda _name: None,
    )

    assert not report.ok
    assert report.checks[-1].status is CheckStatus.FAIL


def test_doctor_passes_existing_command(tmp_path: Path) -> None:
    report = run_doctor(
        command_requirements=(CommandRequirement("ssh", "SSH"),),
        config_path=tmp_path / "missing.toml",
        command_resolver=lambda name: f"/usr/bin/{name}",
    )

    assert report.checks[-1].status is CheckStatus.PASS


def test_doctor_validates_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("version = 1\n", encoding="utf-8")

    report = run_doctor(config_path=config_path)
    by_name = {check.name: check for check in report.checks}

    assert by_name["config"].status is CheckStatus.PASS
    assert report.ok


def test_doctor_rejects_invalid_config_without_echoing_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[hosts.phone]\nhost = 'example'\npassword = 'super-secret-value'\n",
        encoding="utf-8",
    )

    report = run_doctor(config_path=config_path)
    by_name = {check.name: check for check in report.checks}

    assert by_name["config"].status is CheckStatus.FAIL
    assert "super-secret-value" not in by_name["config"].summary
    assert not report.ok
