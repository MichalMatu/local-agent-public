from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "host_ops_quality" / "check_architecture.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("host_ops_architecture_checker", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _rules(violations) -> set[str]:
    return {violation.rule for violation in violations}


def test_repository_currently_satisfies_architecture_contract(checker) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    assert checker.scan_tree(repository_root) == []


def test_rejects_dumping_ground_module_name(tmp_path: Path, checker) -> None:
    _write(tmp_path, "local_agent/host_ops/core/utils.py", "VALUE = 1\n")
    assert "forbidden-module-name" in _rules(checker.scan_tree(tmp_path))


def test_rejects_core_dependency_on_concrete_capability(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/core/config/loader.py",
        "from local_agent.host_ops.capabilities.browser.playwright import session\n",
    )
    assert "core-imports-capability" in _rules(checker.scan_tree(tmp_path))


def test_rejects_relative_core_dependency_on_concrete_capability(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/core/config/loader.py",
        "from ...capabilities.remote import ssh\n",
    )
    assert "core-imports-capability" in _rules(checker.scan_tree(tmp_path))


def test_rejects_core_dependency_on_workflow(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/core/config/loader.py",
        "from local_agent.host_ops.workflows.remote_git import RemoteGitRunner\n",
    )
    assert "core-imports-workflow" in _rules(checker.scan_tree(tmp_path))


def test_rejects_vendor_adapter_import_from_core(tmp_path: Path, checker) -> None:
    _write(tmp_path, "local_agent/host_ops/core/config/loader.py", "import playwright.sync_api\n")
    assert "core-imports-adapter-vendor" in _rules(checker.scan_tree(tmp_path))


def test_rejects_direct_sibling_capability_import(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/capabilities/browser/playwright/session.py",
        "from local_agent.host_ops.capabilities.android.adb import transport\n",
    )
    assert "cross-capability-import" in _rules(checker.scan_tree(tmp_path))


def test_rejects_same_domain_sibling_capability_import(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/capabilities/remote/transfer/client.py",
        "from local_agent.host_ops.capabilities.remote.ssh import SshClient\n",
    )
    assert "cross-capability-import" in _rules(checker.scan_tree(tmp_path))


def test_rejects_relative_sibling_capability_import(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/capabilities/browser/playwright/session.py",
        "from ...android.adb import transport\n",
    )
    assert "cross-capability-import" in _rules(checker.scan_tree(tmp_path))


def test_rejects_capability_dependency_on_workflow(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/capabilities/remote/ssh/client.py",
        "from local_agent.host_ops.workflows.remote_git import RemoteGitRunner\n",
    )
    assert "capability-imports-workflow" in _rules(checker.scan_tree(tmp_path))


def test_rejects_workflow_dependency_on_cli(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/workflows/remote_git/runner.py",
        "from local_agent.host_ops.cli.main import main\n",
    )
    assert "workflow-imports-cli" in _rules(checker.scan_tree(tmp_path))


def test_rejects_raw_subprocess_from_capability(tmp_path: Path, checker) -> None:
    _write(tmp_path, "local_agent/host_ops/capabilities/remote/ssh/client.py", "import subprocess\n")
    assert "raw-subprocess-outside-execution" in _rules(checker.scan_tree(tmp_path))


def test_allows_subprocess_inside_shared_execution_owner(tmp_path: Path, checker) -> None:
    _write(tmp_path, "local_agent/host_ops/core/execution/process.py", "import subprocess\n")
    assert checker.scan_tree(tmp_path) == []
