"""The example workflows in test-agents/workflows/ must stay compilable.

They are the closest thing the project has to real canvases, so they double as a
regression suite for the compiler: a validation rule that is too strict shows up
here as a seed that no longer compiles.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.compiler import run_compiler
from app.schemas.canvas import CanvasPayload

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "test-agents" / "workflows"


def _seed_files() -> list[Path]:
    if not SEED_DIR.is_dir():
        return []
    # seed_all.py only orchestrates the others; it has no CANVAS of its own.
    return sorted(p for p in SEED_DIR.glob("seed_*.py") if p.name != "seed_all.py")


def _load_canvas(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "CANVAS")


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_workflow_compiles(path):
    canvas = CanvasPayload.model_validate(_load_canvas(path))
    errors, _warnings, ir = run_compiler(canvas, "test")

    assert errors == [], f"{path.name} no longer compiles: {errors}"
    assert ir is not None
    assert len(ir.entrypoints) == 1


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_workflow_uses_a2a_start(path):
    """No seed may still reference a retired trigger type."""
    canvas = _load_canvas(path)
    types = [node["type"] for node in canvas["nodes"]]

    assert types.count("A2A_START") == 1, types
    for retired in ("HTTP_TRIGGER", "SCHEDULE_TRIGGER", "WEBHOOK_TRIGGER", "QUEUE_TRIGGER"):
        assert retired not in types


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_workflow_has_one_terminal(path):
    """ADK permits at most one terminal output; see test_adk_contract.py."""
    canvas = _load_canvas(path)
    ends = [n for n in canvas["nodes"] if n["type"] == "END"]

    assert len(ends) == 1, f"{path.name} has {len(ends)} END nodes"


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_condition_handles_match_branch_names(path):
    """A CONDITION's edge handles must be its declared branch names.

    A mismatch compiles but routes nowhere: the router returns a route value
    that no edge is keyed on.
    """
    canvas = _load_canvas(path)
    by_id = {n["id"]: n for n in canvas["nodes"]}

    for node in canvas["nodes"]:
        if node["type"] != "CONDITION":
            continue
        declared = {b["name"] for b in node["config"]["branches"]} | {"default"}
        handles = {
            e.get("source_handle", "output")
            for e in canvas["edges"]
            if e["source"] == node["id"]
        }
        assert handles <= declared, (
            f"{path.name}: CONDITION '{node['id']}' has edge handles {handles - declared} "
            f"that are not declared branches {declared}"
        )
        assert by_id  # keep the lookup meaningful if the assertion above changes


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_seed_a2a_start_declares_a_payload(path):
    """The entry node's payload_schema is the agent's documented input."""
    from app.nodes.triggers import payload_json_schema

    canvas = _load_canvas(path)
    start = next(n for n in canvas["nodes"] if n["type"] == "A2A_START")
    fields = (start["config"].get("payload_schema") or {}).get("fields") or []

    assert fields, f"{path.name}: A2A_START declares no payload fields"

    schema = payload_json_schema(start["config"])
    assert schema["properties"]
    assert schema.get("required"), "at least one field should be required"


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_no_seed_pins_itself_to_a_schema_version(path):
    """A seed that names a version gets migrated on read, and Run goes grey.

    `CanvasPayload.schema_version` defaults to the current one, so omitting it
    is both simpler and self-maintaining. Seeds that hardcoded a number drifted
    the moment the schema moved: four were still on 6 and one on 4 after the
    v7 migration landed, so they seeded, compiled, and then opened in the UI as
    "migrated — Save & Compile to apply", with Run and Package disabled.
    """
    source = path.read_text()
    assert '"schema_version"' not in source, (
        f"{path.name} pins a schema version; let the backend stamp it"
    )


@pytest.mark.skipif(not _seed_files(), reason="test-agents/workflows not present")
@pytest.mark.parametrize("path", _seed_files(), ids=lambda p: p.stem)
def test_a_seed_opens_without_needing_migration(path):
    """What the UI actually does on load, which is where the drift showed."""
    from app.compiler.canvas_migrations import needs_migration
    from app.schemas.canvas import CanvasPayload

    canvas = CanvasPayload.model_validate(_load_canvas(path))
    assert not needs_migration(canvas.model_dump())
