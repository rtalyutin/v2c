"""Enforce runtime import directions and reject module cycles.

TYPE_CHECKING imports are annotations, not runtime coupling. Compatibility
facades are ordinary modules and receive no exemption from the cycle check.
"""

from __future__ import annotations

import argparse
import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "v2c"


def within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def runtime_nodes(node: ast.AST):
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and (
            isinstance(child.test, ast.Name)
            and child.test.id == "TYPE_CHECKING"
            or isinstance(child.test, ast.Attribute)
            and child.test.attr == "TYPE_CHECKING"
        ):
            for alternative in child.orelse:
                yield from runtime_nodes(alternative)
        else:
            yield from runtime_nodes(child)


def check(root: Path) -> list[str]:
    modules = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(["v2c", *parts])] = path
    graph: dict[str, set[str]] = {name: set() for name in modules}
    failures: list[str] = []
    control = (
        "v2c.api",
        "v2c.engine",
        "v2c.pipeline",
        "v2c.orchestration",
        "v2c.application",
        "v2c.config",
    )
    for name, path in modules.items():
        package = name if path.stem == "__init__" else name.rpartition(".")[0]
        for node in runtime_nodes(ast.parse(path.read_text())):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (
                    resolve_name("." * node.level + (node.module or ""), package)
                    if node.level
                    else node.module or ""
                )
                targets = [base, *(base + "." + alias.name for alias in node.names)]
            for target in targets:
                if not target.startswith("v2c"):
                    continue
                forbidden = False
                if within(name, "v2c.contracts"):
                    forbidden = not within(target, "v2c.contracts")
                elif within(name, "v2c.workflow"):
                    forbidden = not any(
                        within(target, p) for p in ("v2c.workflow", "v2c.contracts")
                    )
                elif within(name, "v2c.persistence"):
                    forbidden = target.startswith(
                        (*control, "v2c.adapters", "v2c.runner", "v2c.providers")
                    )
                elif within(name, "v2c.runner") or within(name, "v2c.providers.timeweb"):
                    forbidden = target.startswith((*control, "v2c.models", "v2c.persistence"))
                if forbidden:
                    failures.append(f"Forbidden dependency: {name} -> {target}")
                if target in modules and target != name:
                    graph[name].add(target)
    visited: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in active:
            failures.append("Import cycle: " + " -> ".join([*active[active.index(name) :], name]))
            return
        if name in visited:
            return
        active.append(name)
        for target in sorted(graph[name]):
            visit(target)
        active.pop()
        visited.add(name)

    for name in sorted(modules):
        visit(name)
    return sorted(set(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    failures = check(parser.parse_args().root)
    if failures:
        raise SystemExit("\n".join(failures))
    print("Architecture import boundaries and cycles: PASS")


if __name__ == "__main__":
    main()
