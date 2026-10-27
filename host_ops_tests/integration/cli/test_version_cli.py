from __future__ import annotations

import pytest

from local_agent.host_ops.cli.main import main
from local_agent.host_ops.version import JSON_CONTRACT_VERSION


def test_version_reports_installed_host_ops_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out.strip()
    assert output.startswith("hostops ")
    assert len(output.split()) == 2


def test_json_contract_version_reports_machine_readable_contract(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--json-contract-version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == str(JSON_CONTRACT_VERSION)
