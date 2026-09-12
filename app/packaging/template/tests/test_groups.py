"""Sequential and parallel tool groups.

Part of the template's own suite: `tools/groups.py` and `tools/signature.py` are
copied verbatim into every package, so proving them once here proves them
everywhere.

Everything uses inline functions as children, so the whole file runs offline.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.tools import FunctionTool

from tools.functions import build_group, collect_tools
from tools.groups import build_group_tool, merged_schema, run_parallel, run_sequential
from tools.signature import apply_schema_signature, safe_tool_name


def _schema_of(tool) -> dict:
    declaration = tool._get_declaration()
    dumped = declaration.model_dump(exclude_none=True)
    return dumped.get("parameters_json_schema") or {}


def _tool(name: str, params: dict, required: list[str], body):
    """A FunctionTool with an explicit declared signature."""

    async def impl(**kwargs):
        return body(kwargs)

    impl.__name__ = name
    impl.__doc__ = f"Tool {name}."
    apply_schema_signature(
        impl, {"type": "object", "properties": params, "required": required}
    )
    return FunctionTool(impl)


# ── Declaring parameters at all ──────────────────────────────────────────────


def test_a_kwargs_wrapper_declares_nothing_without_help():
    """The bug this module exists to prevent.

    ADK builds a declaration from the signature, so `**kwargs` alone gives the
    model a tool it cannot pass anything to — silently.
    """

    async def bare(**kwargs) -> dict:
        """A tool."""
        return {}

    assert _schema_of(FunctionTool(bare)) == {}


def test_a_synthesized_signature_declares_the_schema():
    async def fixed(**kwargs) -> dict:
        """A tool."""
        return {}

    apply_schema_signature(
        fixed,
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    )
    schema = _schema_of(FunctionTool(fixed))

    assert set(schema["properties"]) == {"query", "limit"}
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["required"] == ["query"]


def test_a_schemaless_tool_falls_back_to_a_free_text_parameter():
    """Better to accept a request than to accept nothing."""

    async def unknown(**kwargs) -> dict:
        """A tool whose input schema we could not read."""
        return {}

    apply_schema_signature(unknown, None, fallback_param="request")
    schema = _schema_of(FunctionTool(unknown))

    assert list(schema["properties"]) == ["request"]
    assert schema["required"] == ["request"]


def test_required_parameters_are_ordered_first():
    """A parameter with a default cannot precede one without."""

    async def mixed(**kwargs) -> dict:
        """A tool."""
        return {}

    apply_schema_signature(
        mixed,
        {
            "properties": {"optional": {"type": "string"}, "needed": {"type": "string"}},
            "required": ["needed"],
        },
    )
    assert _schema_of(FunctionTool(mixed))["required"] == ["needed"]


def test_safe_tool_name():
    assert safe_tool_name("kb", "search docs") == "kb__search_docs"
    assert safe_tool_name("a2a", "Diet Advisor") == "a2a__Diet_Advisor"
    assert safe_tool_name("") == "tool"
    assert safe_tool_name("2fast")[0].isalpha()


# ── The group's declared parameters ──────────────────────────────────────────


def test_sequential_requires_only_the_first_tool_s_input():
    """A pipeline is callable in one turn: the model supplies the input, not
    every intermediate value."""
    first = _tool("fetch", {"url": {"type": "string"}}, ["url"], lambda kw: {"body": "x"})
    second = _tool("summarise", {"body": {"type": "string"}}, ["body"], lambda kw: {})

    schema = merged_schema([first, second], "sequential")

    assert set(schema["properties"]) == {"url", "body"}
    assert schema["required"] == ["url"]


def test_parallel_requires_every_tool_s_input():
    """Every child runs on the same input, so all of it must be supplied."""
    left = _tool("a", {"text": {"type": "string"}}, ["text"], lambda kw: {})
    right = _tool("b", {"lang": {"type": "string"}}, ["lang"], lambda kw: {})

    schema = merged_schema([left, right], "parallel")

    assert set(schema["properties"]) == {"text", "lang"}
    assert sorted(schema["required"]) == ["lang", "text"]


def test_group_exposes_one_tool_with_a_generated_description():
    first = _tool("fetch", {"url": {"type": "string"}}, ["url"], lambda kw: {})
    second = _tool("summarise", {"url": {"type": "string"}}, [], lambda kw: {})

    group = build_group_tool({"name": "pipeline", "mode": "sequential"}, [first, second])

    assert group.name == "pipeline"
    description = group._get_declaration().description
    assert "fetch then summarise" in description
    assert list(_schema_of(group)["properties"]) == ["url"]


# ── Sequential execution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sequential_runs_in_order_and_threads_output():
    order: list[str] = []

    first = _tool(
        "extract", {"text": {"type": "string"}}, ["text"],
        lambda kw: (order.append("extract"), {"words": len(kw["text"].split())})[1],
    )
    second = _tool(
        "grade", {"words": {"type": "integer"}}, ["words"],
        lambda kw: (order.append("grade"), {"grade": "long" if kw["words"] > 3 else "short"})[1],
    )

    out = await run_sequential([first, second], {"text": "one two three four five"})

    assert order == ["extract", "grade"]
    assert out["ok"] is True
    assert [step["tool"] for step in out["steps"]] == ["extract", "grade"]
    # `grade` only worked because `extract`'s output was threaded in.
    assert out["result"]["grade"] == "long"


@pytest.mark.asyncio
async def test_sequential_stops_at_the_first_failure_by_default():
    ran: list[str] = []

    def boom(_kw):
        raise ValueError("nope")

    failing = _tool("boom", {"x": {"type": "string"}}, [], boom)
    after = _tool("after", {"x": {"type": "string"}}, [], lambda kw: (ran.append("after"), {})[1])

    out = await run_sequential([failing, after], {"x": "1"}, stop_on_error=True)

    assert out["ok"] is False
    assert "boom failed" in out["error"]
    assert ran == [], "the pipeline should have stopped"


@pytest.mark.asyncio
async def test_sequential_can_be_told_to_carry_on():
    ran: list[str] = []

    def boom(_kw):
        raise ValueError("nope")

    failing = _tool("boom", {"x": {"type": "string"}}, [], boom)
    after = _tool("after", {"x": {"type": "string"}}, [], lambda kw: (ran.append("after"), {"done": True})[1])

    out = await run_sequential([failing, after], {"x": "1"}, stop_on_error=False)

    assert out["ok"] is True
    assert ran == ["after"]
    assert [step["ok"] for step in out["steps"]] == [False, True]


@pytest.mark.asyncio
async def test_a_child_only_receives_parameters_it_declares():
    """A child must not be handed keys its signature does not accept."""
    seen: dict = {}

    narrow = _tool(
        "narrow", {"wanted": {"type": "string"}}, ["wanted"],
        lambda kw: (seen.update(kw), {})[1],
    )

    await run_sequential([narrow], {"wanted": "yes", "unwanted": "no"})

    assert seen == {"wanted": "yes"}


# ── Parallel execution ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_runs_every_child_on_the_same_input():
    left = _tool("chars", {"text": {"type": "string"}}, ["text"], lambda kw: {"n": len(kw["text"])})
    right = _tool("upper", {"text": {"type": "string"}}, ["text"], lambda kw: {"up": kw["text"].upper()})

    out = await run_parallel([left, right], {"text": "abc"})

    assert out["ok"] is True
    assert out["results"] == {"chars": {"n": 3}, "upper": {"up": "ABC"}}


@pytest.mark.asyncio
async def test_parallel_actually_overlaps():
    """Serial execution of three 100ms tools would take 300ms."""

    async def make(name):
        async def impl(**kwargs):
            await asyncio.sleep(0.1)
            return {"who": name}

        impl.__name__ = name
        impl.__doc__ = name
        apply_schema_signature(impl, {"properties": {"x": {"type": "string"}}})
        return FunctionTool(impl)

    children = [await make("a"), await make("b"), await make("c")]

    started = asyncio.get_running_loop().time()
    out = await run_parallel(children, {"x": "1"})
    elapsed = asyncio.get_running_loop().time() - started

    assert len(out["results"]) == 3
    assert elapsed < 0.25, f"three 100ms tools took {elapsed:.2f}s — not concurrent"


@pytest.mark.asyncio
async def test_parallel_keeps_the_survivors_by_default():
    """One dead source should not lose the others."""

    def boom(_kw):
        raise ValueError("down")

    failing = _tool("dead", {"x": {"type": "string"}}, [], boom)
    working = _tool("alive", {"x": {"type": "string"}}, [], lambda kw: {"ok": 1})

    out = await run_parallel([failing, working], {"x": "1"}, stop_on_error=False)

    assert out["ok"] is True
    assert out["failed"] == ["dead"]
    assert out["results"]["alive"] == {"ok": 1}


@pytest.mark.asyncio
async def test_parallel_can_report_the_whole_group_as_failed():
    def boom(_kw):
        raise ValueError("down")

    failing = _tool("dead", {"x": {"type": "string"}}, [], boom)
    working = _tool("alive", {"x": {"type": "string"}}, [], lambda kw: {"ok": 1})

    out = await run_parallel([failing, working], {"x": "1"}, stop_on_error=True)

    assert out["ok"] is False
    assert "dead" in out["error"]


@pytest.mark.asyncio
async def test_max_concurrency_is_respected():
    live = 0
    peak = 0

    async def make(name):
        async def impl(**kwargs):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return {"who": name}

        impl.__name__ = name
        impl.__doc__ = name
        apply_schema_signature(impl, {"properties": {"x": {"type": "string"}}})
        return FunctionTool(impl)

    children = [await make(f"t{i}") for i in range(6)]
    await run_parallel(children, {"x": "1"}, max_concurrency=2)

    assert peak <= 2, f"ran {peak} at once with max_concurrency=2"


# ── Building from the generated config ───────────────────────────────────────


def _function_child(name: str, params: dict, required: list[str], code: str) -> dict:
    return {
        "kind": "function",
        "name": name,
        "parameters": {"type": "object", "properties": params, "required": required},
        "code": code,
    }


@pytest.mark.asyncio
async def test_a_generated_group_config_becomes_one_tool():
    group = {
        "kind": "group",
        "name": "pipeline",
        "mode": "sequential",
        "stop_on_error": True,
        "children": [
            _function_child("extract", {"text": {"type": "string"}}, ["text"],
                            "result = {'words': len(data['text'].split())}"),
            _function_child("grade", {"words": {"type": "integer"}}, ["words"],
                            "result = {'grade': 'long' if data['words'] > 2 else 'short'}"),
        ],
    }

    tool = await build_group(group)

    assert tool.name == "pipeline"
    assert _schema_of(tool)["required"] == ["text"]

    out = await tool.func(text="one two three")
    assert out["ok"] is True
    assert out["result"]["grade"] == "long"


@pytest.mark.asyncio
async def test_collect_tools_shows_one_entry_per_group():
    groups = [
        {
            "kind": "group", "name": "first", "mode": "sequential",
            "children": [_function_child("a", {"x": {"type": "string"}}, [], "result = {}")],
        },
        {
            "kind": "group", "name": "second", "mode": "parallel",
            "children": [_function_child("b", {"x": {"type": "string"}}, [], "result = {}")],
        },
    ]

    tools = await collect_tools(groups=groups)

    # Two groups with one child each -> two tools, not four.
    assert [t.name for t in tools] == ["first", "second"]


@pytest.mark.asyncio
async def test_a_group_can_contain_a_group():
    inner = {
        "kind": "group", "name": "inner", "mode": "parallel",
        "children": [
            _function_child("x", {"text": {"type": "string"}}, ["text"], "result = {'x': 1}"),
            _function_child("y", {"text": {"type": "string"}}, ["text"], "result = {'y': 2}"),
        ],
    }
    outer = {
        "kind": "group", "name": "outer", "mode": "sequential",
        "children": [
            _function_child("prepare", {"text": {"type": "string"}}, ["text"],
                            "result = {'text': data['text'].strip()}"),
            inner,
        ],
    }

    tool = await build_group(outer)
    out = await tool.func(text="  hi  ")

    assert out["ok"] is True
    assert [step["tool"] for step in out["steps"]] == ["prepare", "inner"]
    # The nested group's own result shape is preserved.
    nested = out["steps"][1]["result"]
    assert nested["mode"] == "parallel"
    assert set(nested["results"]) == {"x", "y"}
