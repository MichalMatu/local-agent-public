#!/usr/bin/env python3
"""Read-only local Python architecture graph. No imports are executed."""
from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def module_name(root: Path, source: Path) -> str:
    path = source.relative_to(root).with_suffix("")
    parts = path.parts
    # The filesystem product folder is hyphenated; Python package names are not.
    if parts and parts[0] == "local-agent":
        parts = ("local_agent", *parts[1:])
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_graph(root: Path) -> dict[str, set[str]]:
    """Resolve local import edges without loading application code."""
    base = root / "platforms/macos/local-agent"
    if not base.is_dir():
        raise ValueError("local_agent package not found")
    files = sorted(p for p in base.rglob("*.py") if not p.is_symlink())
    names = {module_name(base.parent, p) for p in files}
    graph: dict[str, set[str]] = {name: set() for name in sorted(names)}
    for path in files:
        name = module_name(base.parent, path)
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    components = package.split(".")
                    if node.level > len(components):
                        continue
                    prefix = ".".join(components[:len(components) - node.level + 1])
                    base_name = ".".join(filter(None, (prefix, node.module or "")))
                else:
                    base_name = node.module or ""
                candidates = [base_name]
                candidates.extend(
                    f"{base_name}.{alias.name}" for alias in node.names if alias.name != "*"
                )
            for target in candidates:
                if target == name:
                    continue
                if target in names:
                    graph[name].add(target)
                else:
                    # 'import local_agent.x' also imports its parent package.
                    parts = target.split(".")
                    for size in range(len(parts) - 1, 1, -1):
                        ancestor = ".".join(parts[:size])
                        if ancestor in names and ancestor != name:
                            graph[name].add(ancestor)
                            break
    return graph


def cyclic_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan strongly connected components; only genuine cycles returned."""
    stack: list[str] = []
    active: set[str] = set()
    discovery: dict[str, int] = {}
    low: dict[str, int] = {}
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        discovery[node] = low[node] = len(discovery)
        stack.append(node)
        active.add(node)
        for child in sorted(graph[node]):
            if child not in discovery:
                visit(child)
                low[node] = min(low[node], low[child])
            elif child in active:
                low[node] = min(low[node], discovery[child])
        if low[node] == discovery[node]:
            members: list[str] = []
            while stack:
                member = stack.pop()
                active.remove(member)
                members.append(member)
                if member == node:
                    break
            if len(members) > 1:
                cycles.append(sorted(members))

    for name in sorted(graph):
        if name not in discovery:
            visit(name)
    return sorted(cycles, key=lambda c: (-len(c), c))


def build_report(root: Path) -> dict[str, object]:
    graph = import_graph(root)
    inbound: dict[str, int] = defaultdict(int)
    for dependencies in graph.values():
        for dependency in dependencies:
            inbound[dependency] += 1
    hotspots = sorted(
        (
            {"module": name, "inbound": inbound[name], "outbound": len(deps)}
            for name, deps in graph.items()
        ),
        key=lambda item: (-item["inbound"], -item["outbound"], item["module"]),
    )
    cycles = cyclic_components(graph)
    return {
        "status": "observed",
        "source": "offline_ast",
        "module_count": len(graph),
        "import_edges": sum(map(len, graph.values())),
        "cycle_groups": len(cycles),
        "cycles": cycles[:12],
        "cycle_groups_truncated": max(0, len(cycles) - 12),
        "top_inbound": hotspots[:20],
        "mutated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--format", choices=("json", "dot"), default="json")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.format == "json":
        print(json.dumps(build_report(root), indent=2, sort_keys=True))
    else:
        print("digraph local_agent {")
        for name, imports in sorted(import_graph(root).items()):
            print(f'  "{name}";')
            for target in sorted(imports):
                print(f'  "{name}" -> "{target}";')
        print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
