"""Structural guards on the package template.

The template is the source every generated package is copied from, so a break
here breaks every future package.  Its own suite
(`app/packaging/template/tests/`) covers behaviour; this file covers the things
that only matter because it is a *template*:

  * every file compiles, so a generated package can never ship a syntax error
  * the files the generator promises to copy or render all exist
  * the static half imports nothing from the platform backend (`app.*`)
  * `graph.json` matches the registry and the graph, which is the contract the
    Phase 3 compiler has to emit
"""

from __future__ import annotations

import ast
import json
import py_compile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "packaging" / "template"

# Files copied verbatim into every generated package.
STATIC_FILES = [
    "core/__init__.py",
    "core/config.py",
    "core/agent_card.py",
    "core/a2a_app.py",
    "core/state.py",
    "core/logging.py",
    "nodes/__init__.py",
    "nodes/base.py",
    "tools/__init__.py",
    "tools/signature.py",
    "tools/schema.py",
    "tools/mcp.py",
    "tools/a2a.py",
    "tools/functions.py",
    "tools/groups.py",
    "tools/agents.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/test_graph.py",
    "tests/test_nodes.py",
    "Dockerfile",
    "docker-compose.yml",
    "pytest.ini",
    "ruff.toml",
    "run_once.py",
    ".gitignore",
    ".dockerignore",
]

# Files the generator renders per workflow.
RENDERED_FILES = [
    "main.py",
    "agent.py",
    "nodes/registry.py",
    "requirements.txt",
    "requirements-dev.txt",
    "README.md",
    ".env.example",
    "graph.json",
]


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in TEMPLATE.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_template_directory_exists():
    assert TEMPLATE.is_dir(), f"template missing at {TEMPLATE}"


@pytest.mark.parametrize("relative", STATIC_FILES + RENDERED_FILES)
def test_expected_file_exists(relative):
    assert (TEMPLATE / relative).is_file(), f"template is missing {relative}"


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda p: str(p.relative_to(TEMPLATE))
)
def test_every_python_file_compiles(path, tmp_path):
    """A generated package must never ship a syntax error."""
    py_compile.compile(
        str(path), cfile=str(tmp_path / "out.pyc"), doraise=True
    )


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda p: str(p.relative_to(TEMPLATE))
)
def test_no_imports_from_the_platform_backend(path):
    """A generated package is standalone: nothing may import `app.*`."""
    tree = ast.parse(path.read_text())
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                assert not alias.name.startswith("app."), alias.name
                assert alias.name != "app", alias.name
        elif isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            assert not module.startswith("app."), module
            assert module != "app", module


@pytest.mark.parametrize(
    "path",
    [p for p in _python_files() if p.parent.name == "nodes"],
    ids=lambda p: p.name,
)
def test_node_modules_carry_no_return_annotation(path):
    """A `-> Event` return hint fails ADK's edge schema validation.

    See tests/test_adk_contract.py::test_return_annotation_breaks_edge_validation.
    """
    tree = ast.parse(path.read_text())
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        decorated = any(
            (isinstance(d, ast.Name) and d.id == "node")
            or (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "node")
            for d in function.decorator_list
        )
        if decorated:
            assert function.returns is None, (
                f"{path.name}:{function.name} has a return annotation; "
                "ADK would infer it as the node's output_schema"
            )


def test_node_modules_take_ctx_and_node_input():
    """ADK's default 'state' binding requires this exact signature shape."""
    for path in _python_files():
        if path.parent.name != "nodes" or path.name in {"__init__.py", "base.py", "registry.py"}:
            continue
        tree = ast.parse(path.read_text())
        found = False
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            names = [a.arg for a in function.args.args]
            if names[:2] == ["ctx", "node_input"]:
                found = True
        assert found, f"{path.name} has no (ctx, node_input) node function"


# ── graph.json is the compiler's output contract ─────────────────────────────


@pytest.fixture
def graph() -> dict:
    return json.loads((TEMPLATE / "graph.json").read_text())


def test_graph_json_matches_the_registry(graph):
    import sys

    if str(TEMPLATE) not in sys.path:
        sys.path.insert(0, str(TEMPLATE))
    from nodes.registry import CANVAS_IDS, ENTRY_NODE, NODES, TERMINAL_NODE

    names = {node["name"] for node in graph["nodes"]}
    assert names == set(NODES)
    assert graph["entry_node"] == ENTRY_NODE
    assert graph["terminal_node"] == TERMINAL_NODE

    for node in graph["nodes"]:
        assert node["canvas_id"] == CANVAS_IDS[node["name"]]


def test_graph_json_edges_are_well_formed(graph):
    names = {node["name"] for node in graph["nodes"]} | {"START"}

    seen: set[tuple[str, str]] = set()
    for edge in graph["edges"]:
        assert edge["from"] in names, edge
        assert edge["to"] in names, edge
        pair = (edge["from"], edge["to"])
        # ADK rejects two edges sharing a from/to pair, so the compiler must
        # merge them into one entry with a route list.
        assert pair not in seen, f"duplicate edge in graph.json: {pair}"
        seen.add(pair)

        route = edge.get("route")
        assert route is None or isinstance(route, (str, int, bool, list)), edge


def test_graph_json_declares_a_single_terminal(graph):
    with_outgoing = {edge["from"] for edge in graph["edges"]}
    names = {node["name"] for node in graph["nodes"]}
    terminals = names - with_outgoing
    assert terminals == {graph["terminal_node"]}, terminals


def test_graph_json_routes_are_declared_on_their_node(graph):
    by_name = {node["name"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        route = edge.get("route")
        if not route or edge["from"] == "START":
            continue
        declared = by_name[edge["from"]].get("routes") or []
        values = route if isinstance(route, list) else [route]
        for value in values:
            assert value in declared, (
                f"{edge['from']} routes to {edge['to']} via {value!r}, "
                f"which is not in its declared routes {declared}"
            )
