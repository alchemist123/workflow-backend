"""Run a workflow by driving its generated package.

This is the platform's Test path, and it deliberately has no execution logic of
its own: it renders the package, then shells out to the package's own
`run_once.py`. What you test is therefore the artefact that ships, byte for
byte, rather than a parallel implementation that can drift from it — which is
the divergence this whole migration exists to remove.

A subprocess rather than an in-process import: a package's modules are named
`agent`, `core.*`, `nodes.*`, which would collide with this backend's own
imports and be cached across runs. Running with the package as the working
directory is exactly how the container imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.compiler.graph_plan import GraphPlan
from app.packaging.builder import build_runner_package

logger = logging.getLogger(__name__)

# A test run has to finish while someone is watching it.
DEFAULT_TIMEOUT = 180.0


@dataclass
class NodeStep:
    """One node event from the run, mapped back to its canvas node."""

    node: str
    canvas_id: str | None
    node_type: str
    iteration: int
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "canvas_id": self.canvas_id,
            "node_type": self.node_type,
            "iteration": self.iteration,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class RunResult:
    ok: bool
    state: str
    mode: str
    task_id: str | None = None
    context_id: str | None = None
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
    polls: int = 0
    package_dir: str | None = None
    steps: list[NodeStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set when the workflow parked on a HUMAN_APPROVAL node: what it is asking,
    # and the interrupt id to quote when answering. A run in this state is
    # waiting, not broken, so `ok` stays False but `error` stays empty.
    input_required: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "mode": self.mode,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "polls": self.polls,
            "package_dir": self.package_dir,
            "steps": [s.to_dict() for s in self.steps],
            "warnings": self.warnings,
            "input_required": self.input_required,
        }


def ensure_package(plan: GraphPlan, *, rebuild: bool = False) -> tuple[Path, list[str]]:
    """The package directory for this workflow version, rendering it if needed.

    Package directories are keyed on the version id, so a version is rendered
    once and reused across test runs. `rebuild` forces a fresh render, which is
    what you want after editing the template or the renderer.
    """
    packages_root = Path(
        os.environ.get("PACKAGES_DIR", str(Path(__file__).parent.parent.parent / "packages"))
    )
    from app.packaging.builder import package_slug

    candidate = packages_root / f"{package_slug(plan.workflow_name)}-{plan.version_id[:8]}"

    if not rebuild and (candidate / "run_once.py").is_file() and (candidate / "graph.json").is_file():
        # Reuse only if the rendered graph still matches the plan; a canvas edit
        # that kept the same version id would otherwise test stale code.
        try:
            existing = json.loads((candidate / "graph.json").read_text())
            if existing == plan.to_dict():
                logger.info("reusing rendered package at %s", candidate)
                return candidate, []
        except (OSError, ValueError):
            pass

    # Lint and git are for a package someone will read or push; a test run wants
    # to start as soon as possible.
    built = build_runner_package(plan, lint=False)
    # `package_dir` is the path as the user sees it, which inside a container is
    # a host path that does not exist here -- launching against it failed with
    # ENOENT on every fresh render while a reused package worked, because reuse
    # went through `candidate` above.
    return Path(built["local_package_dir"]), built["warnings"]


async def run_workflow_package(
    plan: GraphPlan,
    payload: dict[str, Any],
    *,
    mode: str = "message",
    timeout: float = DEFAULT_TIMEOUT,
    rebuild: bool = False,
    answer: dict[str, Any] | None = None,
) -> RunResult:
    """Render (or reuse) the package and drive it once. Never raises.

    `answer` resolves a workflow that parks on a HUMAN_APPROVAL node. The task
    lives inside the `run_once.py` process, which has already exited, so there
    is nothing to resume: the workflow is **run again from the start** and the
    answer is given to the first thing it asks for. For a deterministic
    workflow that reaches the same gate, the outcome is the same — but it is a
    re-run, not a resume, and any side effect before the gate happens twice.
    """
    loop = asyncio.get_running_loop()
    try:
        package_dir, warnings = await loop.run_in_executor(
            None, lambda: ensure_package(plan, rebuild=rebuild)
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.exception("could not build the package for %s", plan.workflow_name)
        return RunResult(
            ok=False,
            state="failed",
            mode=mode,
            error=f"The workflow package could not be built: {exc}",
        )

    envelope = await _invoke(package_dir, payload, mode, timeout, answer)
    return _to_result(envelope, plan, package_dir, mode, warnings)


async def resume_workflow_package(
    plan: GraphPlan,
    *,
    task_id: str,
    context_id: str | None,
    interrupt_id: str,
    response: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> RunResult:
    """Answer a parked task and let the workflow carry on. Never raises.

    A true resume rather than a replay: `run_once.py` keeps the A2A task *and*
    the ADK session in a SQLite file beside the package, so the run continues
    from the approval node and nothing before it happens twice. The package
    holding that state must already exist, so this never renders a new one.
    """
    loop = asyncio.get_running_loop()
    try:
        package_dir, warnings = await loop.run_in_executor(
            None, lambda: ensure_package(plan)
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        logger.exception("could not locate the package for %s", plan.workflow_name)
        return RunResult(
            ok=False,
            state="failed",
            mode="message",
            error=f"The workflow package could not be built: {exc}",
        )

    command = [
        sys.executable,
        "run_once.py",
        "--resume",
        task_id,
        "--interrupt",
        interrupt_id,
        "--answer",
        json.dumps(response or {}),
        "--json",
    ]
    if context_id:
        command += ["--context", context_id]

    envelope = await _run_command(command, package_dir, timeout)
    return _to_result(envelope, plan, package_dir, "message", warnings)


def _to_result(
    envelope: dict[str, Any],
    plan: GraphPlan,
    package_dir: Path,
    mode: str,
    warnings: list[str],
) -> RunResult:
    """One envelope from run_once.py, as a RunResult."""
    if "_runner_error" in envelope:
        return RunResult(
            ok=False,
            state="failed",
            mode=mode,
            error=envelope["_runner_error"],
            package_dir=str(package_dir),
            warnings=warnings,
        )

    node_types = {n.name: n.node_type for n in plan.nodes}
    steps = [
        NodeStep(
            node=step.get("node", ""),
            canvas_id=step.get("canvas_id"),
            node_type=node_types.get(step.get("node", ""), ""),
            iteration=int(step.get("iteration") or 1),
            output=step.get("output"),
            error=step.get("error"),
        )
        for step in envelope.get("trace") or []
    ]

    return RunResult(
        ok=bool(envelope.get("ok")),
        state=str(envelope.get("state") or "unknown"),
        mode=str(envelope.get("mode") or mode),
        task_id=envelope.get("task_id"),
        context_id=envelope.get("context_id"),
        result=envelope.get("result"),
        error=envelope.get("error"),
        duration_ms=int(envelope.get("duration_ms") or 0),
        polls=int(envelope.get("polls") or 0),
        package_dir=str(package_dir),
        input_required=envelope.get("input_required"),
        steps=steps,
        warnings=warnings,
    )


async def _invoke(
    package_dir: Path,
    payload: dict[str, Any],
    mode: str,
    timeout: float,
    answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the package's run_once.py and parse its JSON envelope."""
    command = [
        sys.executable,
        "run_once.py",
        json.dumps(payload or {}),
        "--mode",
        mode,
        "--timeout",
        str(timeout),
        "--json",
    ]
    if answer is not None:
        command += ["--answer", json.dumps(answer)]

    return await _run_command(command, package_dir, timeout)


async def _run_command(
    command: list[str], package_dir: Path, timeout: float
) -> dict[str, Any]:
    """Run one run_once.py invocation and parse its JSON envelope."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(package_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONPATH": str(package_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
                # The package logs per node; keep it out of the platform's log
                # unless something goes wrong.
                "LOG_LEVEL": "WARNING",
            },
        )
    except OSError as exc:
        return {"_runner_error": f"Could not start the workflow package: {exc}"}

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {
            "_runner_error": (
                f"The workflow did not finish within {int(timeout)}s and was stopped."
            )
        }

    text = (stdout or b"").decode("utf-8", "replace").strip()
    errors = (stderr or b"").decode("utf-8", "replace").strip()

    if not text:
        detail = errors.splitlines()[-1] if errors else f"exit code {process.returncode}"
        logger.warning("run_once.py produced no output in %s: %s", package_dir, errors[-2000:])
        return {"_runner_error": f"The workflow package produced no result: {detail}"}

    try:
        # run_once.py prints exactly one JSON object on stdout with --json, but
        # take the last line so a stray print cannot break parsing.
        return json.loads(text.splitlines()[-1])
    except ValueError:
        logger.warning("run_once.py output was not JSON: %s", text[:2000])
        return {
            "_runner_error": "The workflow package returned output the platform could not read."
        }