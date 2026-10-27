from __future__ import annotations

import importlib
import json
from pathlib import Path

cli_main = importlib.import_module("local_agent.host_ops.cli.main")


def test_artifact_parser_exposes_inspect_contract() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "artifact",
            "inspect",
            "input.bin",
            "--max-bytes",
            "1024",
            "--timeout",
            "7",
            "--json",
        ]
    )

    assert args.command == "artifact"
    assert args.artifact_command == "inspect"
    assert args.source == "input.bin"
    assert args.max_bytes == 1024
    assert args.timeout_seconds == 7
    assert args.as_json is True


def test_artifact_parser_exposes_deploy_contract() -> None:
    args = cli_main.build_parser().parse_args(
        [
            "artifact",
            "deploy",
            "input.bin",
            "/tmp/target",
            "--name",
            "firmware.bin",
            "--replace",
            "--max-bytes",
            "2048",
            "--timeout",
            "9",
            "--json",
        ]
    )

    assert args.command == "artifact"
    assert args.artifact_command == "deploy"
    assert args.source == "input.bin"
    assert args.destination_name == "firmware.bin"
    assert args.replace is True
    assert args.max_bytes == 2048
    assert args.timeout_seconds == 9
    assert args.as_json is True


def test_artifact_inspect_json_round_trip(tmp_path: Path, capsys) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")

    result = cli_main.main(["artifact", "inspect", str(source), "--json"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["real_path"] == str(source.resolve())
    assert payload["file_type"] == "regular_file"
    assert payload["size_bytes"] == 7
    assert len(payload["sha256"]) == 64


def test_artifact_inspect_reports_invalid_source(tmp_path: Path, capsys) -> None:
    result = cli_main.main(["artifact", "inspect", str(tmp_path), "--json"])

    assert result == 1
    assert "not a regular file" in json.loads(capsys.readouterr().err)["error"]


def test_artifact_deploy_json_round_trip(tmp_path: Path, capsys) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target"
    target.mkdir()

    result = cli_main.main(
        ["artifact", "deploy", str(source), str(target), "--name", "firmware.bin", "--json"]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["destination"] == str((target / "firmware.bin").resolve())
    assert payload["size_bytes"] == 7


def test_artifact_deploy_reports_existing_destination(tmp_path: Path, capsys) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"new")
    target = tmp_path / "target"
    target.mkdir()
    (target / "input.bin").write_bytes(b"old")

    result = cli_main.main(["artifact", "deploy", str(source), str(target), "--json"])

    assert result == 1
    assert "explicit replace intent" in json.loads(capsys.readouterr().err)["error"]


def test_artifact_inspect_rejects_size_limit_and_invalid_timeout_as_json(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")

    assert (
        cli_main.main(
            ["artifact", "inspect", str(source), "--max-bytes", "3", "--json"]
        )
        == 1
    )
    assert "exceeds max_bytes" in json.loads(capsys.readouterr().err)["error"]

    assert (
        cli_main.main(
            ["artifact", "inspect", str(source), "--timeout", "0", "--json"]
        )
        == 1
    )
    assert "timeout_seconds" in json.loads(capsys.readouterr().err)["error"]


def test_artifact_deploy_rejects_size_limit_without_destination(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target"
    target.mkdir()

    assert (
        cli_main.main(
            [
                "artifact",
                "deploy",
                str(source),
                str(target),
                "--max-bytes",
                "3",
                "--json",
            ]
        )
        == 1
    )
    assert "exceeds max_bytes" in json.loads(capsys.readouterr().err)["error"]
    assert not (target / "input.bin").exists()
