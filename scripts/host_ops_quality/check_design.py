#!/usr/bin/env python3
"""Fail on structural growth patterns that commonly hide god objects."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_MODULE_LINES = 600
MAX_CLASS_LINES = 300
MAX_FUNCTION_LINES = 150
MAX_PUBLIC_METHODS = 12
MAX_CONSTRUCTOR_PARAMETERS = 8


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    rule: str
    detail: str
    line: int | None = None

    def render(self, root: Path) -> str:
        try:
            relative = self.path.relative_to(root)
        except ValueError:
            relative = self.path
        location = f":{self.line}" if self.line is not None else ""
        return f"{relative}{location}: {self.rule}: {self.detail}"


def _span(node: ast.AST) -> int:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return 0
    return end - start + 1


def _explicit_parameter_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    positional = [*node.args.posonlyargs, *node.args.args]
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    count = len(positional) + len(node.args.kwonlyargs)
    if node.args.vararg is not None:
        count += 1
    if node.args.kwarg is not None:
        count += 1
    return count


def _scan_class(path: Path, node: ast.ClassDef) -> list[Violation]:
    violations: list[Violation] = []
    span = _span(node)
    if span > MAX_CLASS_LINES:
        violations.append(
            Violation(
                path,
                "class-size-budget",
                f"{node.name} spans {span} lines; budget is {MAX_CLASS_LINES}",
                node.lineno,
            )
        )

    methods = [
        item for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    public_methods = [method for method in methods if not method.name.startswith("_")]
    if len(public_methods) > MAX_PUBLIC_METHODS:
        violations.append(
            Violation(
                path,
                "public-method-budget",
                (
                    f"{node.name} exposes {len(public_methods)} public methods; "
                    f"budget is {MAX_PUBLIC_METHODS}"
                ),
                node.lineno,
            )
        )

    for method in methods:
        if method.name == "__init__":
            parameter_count = _explicit_parameter_count(method)
            if parameter_count > MAX_CONSTRUCTOR_PARAMETERS:
                violations.append(
                    Violation(
                        path,
                        "constructor-parameter-budget",
                        (
                            f"{node.name}.__init__ has {parameter_count} explicit parameters; "
                            f"budget is {MAX_CONSTRUCTOR_PARAMETERS}"
                        ),
                        method.lineno,
                    )
                )
    return violations


def _scan_python_file(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [Violation(path, "unparseable-python", str(exc))]

    violations: list[Violation] = []
    module_lines = len(source.splitlines())
    if module_lines > MAX_MODULE_LINES:
        violations.append(
            Violation(
                path,
                "module-size-budget",
                f"module has {module_lines} lines; budget is {MAX_MODULE_LINES}",
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            violations.extend(_scan_class(path, node))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            span = _span(node)
            if span > MAX_FUNCTION_LINES:
                violations.append(
                    Violation(
                        path,
                        "function-size-budget",
                        f"{node.name} spans {span} lines; budget is {MAX_FUNCTION_LINES}",
                        node.lineno,
                    )
                )
    return violations


def scan_tree(repository_root: Path) -> list[Violation]:
    source_root = repository_root / "local_agent" / "host_ops"
    if not source_root.exists():
        return []

    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py")):
        violations.extend(_scan_python_file(path))
    return violations


def main() -> int:
    repository_root = Path(__file__).resolve().parents[2]
    violations = scan_tree(repository_root)
    if violations:
        print("Design contract violations:")
        for violation in violations:
            print(f"- {violation.render(repository_root)}")
        return 1

    print("Design contract: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
