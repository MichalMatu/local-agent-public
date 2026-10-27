from __future__ import annotations

import importlib
import json

from local_agent.host_ops.capabilities.local.macos import MacOSStorageActionResult, MacOSStorageControlError
from local_agent.host_ops.cli.commands import macos

cli_main = importlib.import_module("local_agent.host_ops.cli.main")


class FakeController:
    def mount(self, identifier: str):
        return MacOSStorageActionResult("mount", identifier, "mounted")

    def unmount(self, identifier: str):
        return MacOSStorageActionResult("unmount", identifier, "unmounted")

    def eject(self, identifier: str):
        return MacOSStorageActionResult("eject", identifier, "ejected")


class FailingController:
    def mount(self, identifier: str):
        raise MacOSStorageControlError(f"refusing {identifier}")

    unmount = mount
    eject = mount


def test_macos_storage_action_parser() -> None:
    parser = cli_main.build_parser()

    for action in ("mount", "unmount", "eject"):
        args = parser.parse_args(["macos", action, "disk4s1", "--json"])
        assert args.command == "macos"
        assert args.macos_command == action
        assert args.identifier == "disk4s1"
        assert args.as_json is True


def test_macos_mount_json_dispatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSStorageController", FakeController)

    result = cli_main.main(["macos", "mount", "disk4s1", "--json"])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "mount",
        "identifier": "disk4s1",
        "message": "mounted",
    }


def test_macos_storage_action_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(macos, "MacOSStorageController", FailingController)

    result = cli_main.main(["macos", "eject", "disk4", "--json"])

    assert result == 1
    assert "refusing disk4" in json.loads(capsys.readouterr().err)["error"]
