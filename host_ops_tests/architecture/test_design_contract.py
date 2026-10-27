from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "host_ops_quality" / "check_design.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("host_ops_design_checker", CHECKER_PATH)
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


def test_repository_currently_satisfies_design_contract(checker) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    assert checker.scan_tree(repository_root) == []


def test_rejects_oversized_module(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/core/large.py",
        "\n".join(f"VALUE_{index} = {index}" for index in range(checker.MAX_MODULE_LINES + 1)),
    )
    assert "module-size-budget" in _rules(checker.scan_tree(tmp_path))


def test_rejects_oversized_function(tmp_path: Path, checker) -> None:
    body = "\n".join("    value += 1" for _ in range(checker.MAX_FUNCTION_LINES))
    _write(
        tmp_path,
        "local_agent/host_ops/core/large_function.py",
        f"def too_large() -> int:\n    value = 0\n{body}\n    return value\n",
    )
    assert "function-size-budget" in _rules(checker.scan_tree(tmp_path))


def test_rejects_too_many_public_methods(tmp_path: Path, checker) -> None:
    methods = "\n".join(
        f"    def method_{index}(self) -> None:\n        pass"
        for index in range(checker.MAX_PUBLIC_METHODS + 1)
    )
    _write(tmp_path, "local_agent/host_ops/core/god.py", f"class God:\n{methods}\n")
    assert "public-method-budget" in _rules(checker.scan_tree(tmp_path))


def test_rejects_wide_constructor(tmp_path: Path, checker) -> None:
    parameters = ", ".join(
        f"dep_{index}: object" for index in range(checker.MAX_CONSTRUCTOR_PARAMETERS + 1)
    )
    _write(
        tmp_path,
        "local_agent/host_ops/core/wide.py",
        f"class Wide:\n    def __init__(self, {parameters}) -> None:\n        pass\n",
    )
    assert "constructor-parameter-budget" in _rules(checker.scan_tree(tmp_path))


def test_allows_small_cohesive_class(tmp_path: Path, checker) -> None:
    _write(
        tmp_path,
        "local_agent/host_ops/core/small.py",
        "class Small:\n"
        "    def __init__(self, value: int) -> None:\n"
        "        self._value = value\n\n"
        "    def value(self) -> int:\n"
        "        return self._value\n",
    )
    assert checker.scan_tree(tmp_path) == []
