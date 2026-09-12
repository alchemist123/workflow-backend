"""Golden-file coverage of the graph plan for the example workflows.

The seed canvases in test-agents/workflows/ are the closest thing to real user
input, so their compiled plans are checked in.  A diff here is the whole point:
it makes any change to naming, edge shape or env-key derivation visible in
review rather than silently reshaping every generated package.

Regenerate after an intended change:

    UPDATE_GOLDEN=1 pytest tests/test_graph_plan_golden.py

Then read the diff before committing it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import warnings
from pathlib import Path

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.compiler.graph_check import verify_plan_builds
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "test-agents" / "workflows"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "graph_plans"


def _seed_files() -> list[Path]:
    if not SEED_DIR.is_dir():
        return []
    return sorted(p for p in SEED_DIR.glob("seed_*.py") if p.name != "seed_all.py")


def _plan_for(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    canvas = CanvasPayload.model_validate(module.CANVAS)
    errors, _warnings, ir = run_compiler(canvas, "golden-version-id")
    assert errors == [], f"{path.name} does not validate: {errors}"

    name = path.stem.removeprefix("seed_")
    return build_graph_plan(
        ir,
        workflow_name=name,
        workflow_description=f"Seed workflow: {name}",
    )


pytestmark = pytest.mark.skipif(
    not _seed_files(), reason="test-agents/workflows not present"
)


@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_graph_plan_matches_golden(path):
    plan = _plan_for(path)
    actual = {
        "plan": plan.to_dict(),
        "warnings": plan.warnings,
    }
    golden_path = GOLDEN_DIR / f"{path.stem}.json"

    if os.environ.get("UPDATE_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"rewrote {golden_path.name}")

    assert golden_path.exists(), (
        f"no golden file for {path.stem}; run UPDATE_GOLDEN=1 pytest {__file__}"
    )
    expected = json.loads(golden_path.read_text())

    assert actual == expected, (
        f"{path.stem} graph plan changed. If that is intended, regenerate with "
        f"UPDATE_GOLDEN=1 pytest {Path(__file__).name} and review the diff."
    )


@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_graph_plan_is_accepted_by_adk(path):
    """The authoritative check: ADK's own validator builds the plan.

    Anything ADK rejects should have been caught by the semantic validator
    first, so a failure here means our rules have drifted from ADK's.
    """
    plan = _plan_for(path)
    assert verify_plan_builds(plan) == []


@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_graph_plan_invariants(path):
    """The structural promises the renderer relies on."""
    plan = _plan_for(path)
    names = [n.name for n in plan.nodes]

    # Unique, valid Python identifiers -- they become module and node names.
    assert len(set(names)) == len(names)
    for name in names:
        assert name.isidentifier(), name

    # Exactly one entry, wired from START; exactly one terminal.
    assert [n.name for n in plan.nodes if n.is_entry] == [plan.entry_node]
    start_edges = [e for e in plan.edges if e.from_node == "START"]
    assert len(start_edges) == 1
    assert start_edges[0].to_node == plan.entry_node

    with_outgoing = {e.from_node for e in plan.edges}
    assert set(names) - with_outgoing == {plan.terminal_node}

    # No duplicate (from, to) pairs -- ADK rejects those outright.
    pairs = [(e.from_node, e.to_node) for e in plan.edges]
    assert len(pairs) == len(set(pairs))

    # Every edge endpoint exists.
    known = set(names) | {"START"}
    for edge in plan.edges:
        assert edge.from_node in known, edge
        assert edge.to_node in known, edge

    # Every route value is declared on its source node.
    by_name = plan.by_name
    for edge in plan.edges:
        if edge.route is None or edge.from_node == "START":
            continue
        declared = by_name[edge.from_node].routes
        values = edge.route if isinstance(edge.route, list) else [edge.route]
        for value in values:
            assert value in declared, (
                f"{edge.from_node} -> {edge.to_node} routes via {value!r}, "
                f"not in declared routes {declared}"
            )

    # Everything the package ships is generated.
    assert plan.unsupported == []


@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_env_keys_are_well_formed(path):
    plan = _plan_for(path)
    keys = [k.key for k in plan.env_keys]

    assert len(keys) == len(set(keys)), "duplicate env keys"
    for key in keys:
        assert key.isupper()
        assert key.replace("_", "").isalnum(), key

    # Every required key names at least one thing that needs it, so `.env.example`
    # can say why it is there.
    for entry in plan.env_keys:
        assert entry.needed_by, entry.key


def test_golden_directory_has_no_orphans():
    """A renamed or deleted seed should not leave a stale golden file behind."""
    if not GOLDEN_DIR.is_dir():
        pytest.skip("no golden files yet")
    expected = {f"{p.stem}.json" for p in _seed_files()}
    actual = {p.name for p in GOLDEN_DIR.glob("*.json")}
    assert actual - expected == set(), f"stale golden files: {sorted(actual - expected)}"
