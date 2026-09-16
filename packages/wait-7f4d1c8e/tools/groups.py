"""Sequential and parallel tool groups, exposed to an agent as a single tool.

A SEQUENTIAL_AGENT or PARALLEL_AGENT node on the canvas collects several tools
and hands the consuming agent **one** tool that runs them together — in a fixed
order, or concurrently. That replaces the old `tool_execution_mode` toggle on
ORCHESTRATOR_AGENT: the ordering is now something you draw rather than a setting
buried in a config panel.

## Why a composite tool rather than an ADK workflow agent

ADK's `SequentialAgent` / `ParallelAgent` are the obvious candidates, and they
are the wrong tool here for two independent reasons:

1. Their docstrings say they are *"deprecated in favor of Workflow"*, and the
   ADK docs say template workflows have been *"superseded by graph-based
   workflows"*. Building on them now would mean building on the way out.
2. `Workflow` — the replacement — is a `BaseNode` but **not** a `BaseAgent`, so
   it cannot go in an `LlmAgent`'s `sub_agents` and cannot be wrapped by
   `AgentTool`. ADK currently offers no way to attach a graph to an agent's
   tools. (`SequentialAgent.sub_agents` also takes `BaseAgent` instances, and a
   group's children here are *tools*, not agents.)

So the group is a `FunctionTool` whose implementation drives its children. That
is deterministic, costs no extra model turns, and is exactly the semantics the
canvas now draws.

## What the model sees

One tool, whose parameters are the **union** of its children's:

- **parallel** — every child runs on the same input, so every child's required
  parameters are required on the group.
- **sequential** — only the *first* child's required parameters are required.
  Later children's parameters are optional because an earlier child's output is
  merged into the payload, so they are often already filled in.

That rule is what makes a pipeline callable in one turn: the model supplies the
pipeline's input, not every intermediate value.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from tools.signature import safe_tool_name

logger = logging.getLogger("workflow.tools.groups")

SEQUENTIAL = "sequential"
PARALLEL = "parallel"


def _tool_schema(tool: Any) -> dict[str, Any]:
    """The parameter schema a child tool declares, or an empty one."""
    try:
        declaration = tool._get_declaration()
    except Exception:  # noqa: BLE001 - a child without a declaration is usable
        return {}
    if declaration is None:
        return {}
    dumped = declaration.model_dump(exclude_none=True)
    schema = dumped.get("parameters_json_schema") or dumped.get("parameters") or {}
    return schema if isinstance(schema, dict) else {}


def merged_schema(children: list[Any], mode: str) -> dict[str, Any]:
    """The union of the children's parameters, as one JSON Schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for index, tool in enumerate(children):
        schema = _tool_schema(tool)
        child_properties = schema.get("properties") or {}
        child_required = schema.get("required") or []

        for name, spec in child_properties.items():
            if name in properties:
                # Two children want the same name. Keep the first definition and
                # say so, because silently reshaping one child's input would be
                # worse than a slightly wrong type hint.
                logger.debug(
                    "group parameter %r is declared by more than one tool; "
                    "keeping the first definition",
                    name,
                )
                continue
            properties[name] = spec

        # Parallel: everything runs on the caller's input, so all of it is
        # required. Sequential: only the head of the pipeline is.
        if mode == PARALLEL or index == 0:
            for name in child_required:
                if name not in required:
                    required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _accepted_args(tool: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """The subset of `payload` this child's signature actually accepts."""
    function = getattr(tool, "func", None)
    if function is None:
        return dict(payload)
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return dict(payload)

    # A child taking **kwargs gets everything.
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return dict(payload)
    return {name: payload[name] for name in parameters if name in payload}


async def _invoke(tool: Any, payload: dict[str, Any]) -> Any:
    """Call one child tool. Never raises: a failure comes back as `{"error": …}`.

    Calls the wrapped function rather than `tool.run_async`, which would need a
    `ToolContext` this composite has no access to.
    """
    function = getattr(tool, "func", None)
    if function is None:
        return {"error": f"tool {tool!r} has no callable to run"}

    args = _accepted_args(tool, payload)
    try:
        result = function(**args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 - reported to the model as data
        logger.warning("group child %s failed: %s", getattr(tool, "name", "?"), exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    return result


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _as_dict(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {"result": result}


async def run_sequential(
    children: list[Any], payload: dict[str, Any], *, stop_on_error: bool = True
) -> dict[str, Any]:
    """Run the children in order, threading each result into the next call."""
    state = dict(payload)
    steps: list[dict[str, Any]] = []

    for tool in children:
        name = getattr(tool, "name", "tool")
        result = await _invoke(tool, state)
        ok = not _failed(result)
        steps.append({"tool": name, "ok": ok, "result": result})

        if not ok and stop_on_error:
            return {
                "mode": SEQUENTIAL,
                "ok": False,
                "steps": steps,
                "error": f"{name} failed: {_as_dict(result).get('error')}",
            }

        if ok:
            # Merging is what lets a later tool consume an earlier one's output
            # by parameter name.
            state.update(_as_dict(result))

    return {"mode": SEQUENTIAL, "ok": True, "steps": steps, "result": state}


async def run_parallel(
    children: list[Any],
    payload: dict[str, Any],
    *,
    max_concurrency: int = 0,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Run every child on the same input, concurrently."""
    semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None

    async def guarded(tool: Any) -> Any:
        if semaphore is None:
            return await _invoke(tool, payload)
        async with semaphore:
            return await _invoke(tool, payload)

    gathered = await asyncio.gather(*(guarded(tool) for tool in children))

    results: dict[str, Any] = {}
    failures: list[str] = []
    for tool, result in zip(children, gathered):
        name = getattr(tool, "name", "tool")
        results[name] = result
        if _failed(result):
            failures.append(name)

    ok = not failures if stop_on_error else True
    out: dict[str, Any] = {"mode": PARALLEL, "ok": ok, "results": results}
    if failures:
        out["failed"] = failures
        if stop_on_error:
            out["error"] = f"these tools failed: {', '.join(failures)}"
    return out


def build_group_tool(group: dict[str, Any], children: list[Any]) -> Any:
    """Wrap an ordered list of child tools as one FunctionTool.

    `group` carries the canvas config: `name`, `mode`, `description`,
    `stop_on_error` and, for parallel, `max_concurrency`. `children` are already
    built FunctionTools, in the order the group should run them.
    """
    from google.adk.tools import FunctionTool

    mode = group.get("mode") or SEQUENTIAL
    name = safe_tool_name(group.get("name") or f"{mode}_group")
    stop_on_error = bool(group.get("stop_on_error", mode == SEQUENTIAL))
    max_concurrency = int(group.get("max_concurrency") or 0)

    child_names = [getattr(tool, "name", "tool") for tool in children]
    if mode == SEQUENTIAL:
        summary = " then ".join(child_names) or "nothing"
        default_description = (
            f"Runs these tools in order and returns every step: {summary}. "
            "Each tool's output is available to the ones after it."
        )
    else:
        summary = ", ".join(child_names) or "nothing"
        default_description = (
            f"Runs these tools at the same time on the same input and returns "
            f"all their results: {summary}."
        )
    description = group.get("description") or default_description

    async def call_group(**kwargs) -> dict:
        payload = dict(kwargs)
        if mode == PARALLEL:
            return await run_parallel(
                children,
                payload,
                max_concurrency=max_concurrency,
                stop_on_error=stop_on_error,
            )
        return await run_sequential(children, payload, stop_on_error=stop_on_error)

    call_group.__name__ = name
    call_group.__doc__ = description

    # The group declares the union of its children's parameters, so the model
    # can drive the whole pipeline in one call.
    schema = merged_schema(children, mode)
    from tools.signature import apply_schema_signature

    apply_schema_signature(call_group, schema, fallback_param="request")

    logger.info(
        "group %s (%s) exposes %d tool(s): %s",
        name,
        mode,
        len(children),
        ", ".join(child_names) or "none",
    )
    return FunctionTool(call_group)
