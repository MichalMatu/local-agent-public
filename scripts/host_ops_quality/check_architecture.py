#!/usr/bin/env python3
"""Enforce host-ops architecture invariants using only the Python standard library."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_MODULE_BASENAMES = {
    "common.py",
    "helpers.py",
    "misc.py",
    "utils.py",
}

CORE_FORBIDDEN_IMPORT_ROOTS = {
    "Quartz",
    "paramiko",
    "playwright",
    "pyautogui",
}


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    rule: str
    detail: str

    def render(self, root: Path) -> str:
        try:
            relative = self.path.relative_to(root)
        except ValueError:
            relative = self.path
        return f"{relative}: {self.rule}: {self.detail}"


def _python_module_name(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from_import(
    node: ast.ImportFrom,
    *,
    path: Path,
    source_root: Path,
) -> list[str]:
    if node.level == 0:
        base_parts = node.module.split(".") if node.module else []
    else:
        current_module = _python_module_name(path, source_root)
        package_parts = current_module.split(".")
        if path.name != "__init__.py":
            package_parts = package_parts[:-1]

        ascend = node.level - 1
        if ascend > len(package_parts):
            return []
        base_parts = package_parts[: len(package_parts) - ascend]
        if node.module:
            base_parts.extend(node.module.split("."))

    names: list[str] = []
    base = ".".join(base_parts)
    if base:
        names.append(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        imported = ".".join((*base_parts, *alias.name.split(".")))
        if imported:
            names.append(imported)
    return names


def _module_names(tree: ast.AST, *, path: Path, source_root: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.extend(_resolve_from_import(node, path=path, source_root=source_root))
    return names


def _capability_name(path: Path, source_root: Path) -> str | None:
    capabilities_root = source_root / "local_agent" / "host_ops" / "capabilities"
    try:
        relative = path.relative_to(capabilities_root)
    except ValueError:
        return None
    if len(relative.parts) < 3:
        return None
    return ".".join(relative.parts[:2])


def _scan_python_file(path: Path, source_root: Path) -> list[Violation]:
    violations: list[Violation] = []

    if path.name in FORBIDDEN_MODULE_BASENAMES:
        violations.append(
            Violation(
                path,
                "forbidden-module-name",
                "use a cohesive responsibility-specific module name instead",
            )
        )

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [Violation(path, "unparseable-python", str(exc))]

    imports = _module_names(tree, path=path, source_root=source_root)
    core_root = source_root / "local_agent" / "host_ops" / "core"
    workflows_root = source_root / "local_agent" / "host_ops" / "workflows"
    execution_root = core_root / "execution"

    if path.is_relative_to(core_root):
        for module in imports:
            if module == "local_agent.host_ops.capabilities" or module.startswith("local_agent.host_ops.capabilities."):
                violations.append(
                    Violation(
                        path,
                        "core-imports-capability",
                        f"core must not depend on concrete capabilities: {module}",
                    )
                )
            if module == "local_agent.host_ops.workflows" or module.startswith("local_agent.host_ops.workflows."):
                violations.append(
                    Violation(
                        path,
                        "core-imports-workflow",
                        f"core must not depend on application workflows: {module}",
                    )
                )
            import_root = module.split(".", 1)[0]
            if import_root in CORE_FORBIDDEN_IMPORT_ROOTS:
                violations.append(
                    Violation(
                        path,
                        "core-imports-adapter-vendor",
                        f"core must not import adapter/vendor package: {module}",
                    )
                )

    capability = _capability_name(path, source_root)
    if capability:
        prefix = "local_agent.host_ops.capabilities."
        for module in imports:
            if module == "local_agent.host_ops.workflows" or module.startswith("local_agent.host_ops.workflows."):
                violations.append(
                    Violation(
                        path,
                        "capability-imports-workflow",
                        f"capability must not depend on application workflow: {module}",
                    )
                )
            if not module.startswith(prefix):
                continue
            imported_parts = module[len(prefix) :].split(".")
            if len(imported_parts) < 2:
                continue
            imported_capability = ".".join(imported_parts[:2])
            if imported_capability != capability:
                violations.append(
                    Violation(
                        path,
                        "cross-capability-import",
                        f"{capability} must not import sibling capability {imported_capability}",
                    )
                )

    if path.is_relative_to(workflows_root):
        for module in imports:
            if module == "local_agent.host_ops.cli" or module.startswith("local_agent.host_ops.cli."):
                violations.append(
                    Violation(
                        path,
                        "workflow-imports-cli",
                        f"workflow must not depend on CLI presentation: {module}",
                    )
                )

    if not path.is_relative_to(execution_root):
        for module in imports:
            if module == "subprocess" or module.startswith("subprocess."):
                violations.append(
                    Violation(
                        path,
                        "raw-subprocess-outside-execution",
                        "process spawning belongs to local_agent.host_ops.core.execution",
                    )
                )

    return violations


def scan_tree(repository_root: Path) -> list[Violation]:
    source_root = repository_root
    if not source_root.exists():
        return []

    violations: list[Violation] = []
    for path in sorted((repository_root / "local_agent" / "host_ops").rglob("*.py")):
        violations.extend(_scan_python_file(path, source_root))
    return violations


def main() -> int:
    repository_root = Path(__file__).resolve().parents[2]
    violations = scan_tree(repository_root)
    if violations:
        print("Architecture contract violations:")
        for violation in violations:
            print(f"- {violation.render(repository_root)}")
        return 1

    print("Architecture contract: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
