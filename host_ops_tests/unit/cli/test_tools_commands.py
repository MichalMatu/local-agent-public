from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.tools import ToolInspectionResult
from local_agent.host_ops.cli.commands import tools

cli_main = importlib.import_module("local_agent.host_ops.cli.main")


class FakeInspector:
    def inspect(self, names, *, limits=None):
        return (
            ToolInspectionResult(
                name="python3",
                present=True,
                resolved_path="/opt/bin/python3",
                version="Python 3.13.9",
                version_exit_code=0,
            ),
            ToolInspectionResult(name="missing", present=False),
        )


def test_tools_parser_exposes_inspect_command() -> None:
    args = cli_main.build_parser().parse_args(
        ["tools", "inspect", "python3", "git", "--timeout", "2", "--json"]
    )

    assert args.command == "tools"
    assert args.tools_command == "inspect"
    assert args.names == ["python3", "git"]
    assert args.timeout_seconds == 2.0
    assert args.as_json is True


def test_tools_inspect_json_dispatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tools, "ToolInspector", FakeInspector)

    result = cli_main.main(["tools", "inspect", "python3", "missing", "--json"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "python3"
    assert payload[0]["present"] is True
    assert payload[0]["version"] == "Python 3.13.9"
    assert payload[1]["name"] == "missing"
    assert payload[1]["present"] is False


def test_tools_inspect_human_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(tools, "ToolInspector", FakeInspector)

    assert cli_main.main(["tools", "inspect", "python3", "missing"]) == 0
    output = capsys.readouterr().out
    assert "name=python3 present=true" in output
    assert "version=Python 3.13.9" in output
    assert "name=missing present=false" in output


def test_tools_inspect_rejects_invalid_timeout(capsys) -> None:
    result = cli_main.main(["tools", "inspect", "python3", "--timeout", "0", "--json"])

    assert result == 1
    payload = json.loads(capsys.readouterr().err)
    assert "timeout_seconds" in payload["error"]
