"""Keep architectural boundaries and import cycles reviewable without another dependency."""

import ast
import importlib.util
import sys

import pytest

from tests.support.paths import PROJECT_ROOT

PACKAGE = "telegram_mcp"


def imports(module: str, source: str, *, package: bool = False) -> set[str]:
    found: set[str] = set()
    parent = module if package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, parent)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def permitted(source: str, target: str) -> bool:
    source_parts = source.split(".")
    target_parts = target.split(".")
    if target_parts[0] != PACKAGE:
        if len(source_parts) > 1 and source_parts[1] in {"domain", "application"}:
            return target_parts[0] in sys.stdlib_module_names or (
                source_parts[1] == "domain" and target_parts[0] == "pydantic"
            )
        return True
    if source in {PACKAGE, f"{PACKAGE}.bootstrap"}:
        return True
    if source == f"{PACKAGE}.cli":
        return target.startswith(f"{PACKAGE}.bootstrap")
    if len(source_parts) < 2 or len(target_parts) < 2:
        return False
    layer, dependency = source_parts[1], target_parts[1]
    if layer == "domain":
        return dependency == "domain"
    if layer == "application":
        return dependency in {"domain", "application"}
    if layer == "adapters":
        if dependency not in {"domain", "application", "adapters"}:
            return False
        ingress = {f"{PACKAGE}.adapters.{name}" for name in ("cli", "mcp", "transport", "bridge")}
        if source not in ingress and any(
            target == name or target.startswith(name + ".") for name in ingress
        ):
            return False
        return True
    return False


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    done: set[str] = set()
    active: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in active:
            return active[active.index(node) :] + [node]
        if node in done:
            return None
        active.append(node)
        for neighbor in sorted(graph[node]):
            cycle = visit(neighbor)
            if cycle:
                return cycle
        active.pop()
        done.add(node)
        return None

    for node in sorted(graph):
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def test_production_imports_follow_layers_without_cycles() -> None:
    root = PROJECT_ROOT / "src"
    modules = {}
    for path in (root / PACKAGE).rglob("*.py"):
        parts = list(path.relative_to(root).with_suffix("").parts)
        package = parts[-1] == "__init__"
        if package:
            parts.pop()
        name = ".".join(parts)
        modules[name] = imports(name, path.read_text(), package=package)
    violations = sorted(
        (source, target)
        for source, dependencies in modules.items()
        for target in dependencies
        if not permitted(source, target)
    )
    assert violations == []
    assert {name for name in modules if name.count(".") <= 1} == {
        PACKAGE,
        f"{PACKAGE}.cli",
        f"{PACKAGE}.bootstrap",
        *(f"{PACKAGE}.{layer}" for layer in ("domain", "application", "adapters")),
    }
    graph = {
        name: {target for target in dependencies if target in modules and target != name}
        for name, dependencies in modules.items()
    }
    assert find_cycle(graph) is None


def test_test_modules_share_support_without_importing_other_tests() -> None:
    root = PROJECT_ROOT / "tests"
    modules = {
        ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts): path
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
    }
    test_modules = {name for name, path in modules.items() if path.name.startswith("test_")}
    violations = [
        (source, target)
        for source, path in modules.items()
        for target in imports(source, path.read_text(encoding="utf-8"))
        if any(target == test or target.startswith(test + ".") for test in test_modules)
    ]
    assert sorted(violations) == []


@pytest.mark.parametrize(
    ("source", "code", "forbidden"),
    [
        (
            "telegram_mcp.domain.rules",
            "from ..adapters.config import Settings",
            "telegram_mcp.adapters.config",
        ),
        ("telegram_mcp.application.reads", "import telethon", "telethon"),
        ("telegram_mcp.application.reads", "from mcp.server import MCPServer", "mcp.server"),
        (
            "telegram_mcp.application.reads",
            "from ..adapters import config",
            "telegram_mcp.adapters.config",
        ),
        (
            "telegram_mcp.adapters.telegram.runtime",
            "from ..mcp import create_server",
            "telegram_mcp.adapters.mcp",
        ),
    ],
)
def test_import_guard_detects_absolute_and_relative_violations(
    source: str, code: str, forbidden: str
) -> None:
    assert forbidden in imports(source, code)
    assert not permitted(source, forbidden)


def test_cycle_guard_distinguishes_cycles_from_shared_dependencies() -> None:
    assert find_cycle({"a": {"b", "c"}, "b": {"c"}, "c": set()}) is None
    assert find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == ["a", "b", "c", "a"]
