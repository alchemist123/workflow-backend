"""Packaging layer: graph plan → standalone project directory on disk.

The output directory is a fully independent Python project: an ADK graph
workflow served over A2A, with one module per canvas node. It has no imports
from this backend and needs no database or shared volume.

    cp .env.example .env
    docker compose up --build

`render.py` does the writing; this module owns the surrounding concerns — where
the package goes, which host port it claims, the optional lint pass, and the git
repo that makes the directory ready to push.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from app.compiler.graph_plan import GraphPlan
from app.packaging.render import RenderError, render_package

logger = logging.getLogger(__name__)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "no-code-platform",
    "GIT_AUTHOR_EMAIL": "bot@nocode",
    "GIT_COMMITTER_NAME": "no-code-platform",
    "GIT_COMMITTER_EMAIL": "bot@nocode",
}


def package_slug(workflow_name: str) -> str:
    """A filesystem- and Docker-friendly directory name."""
    slug = (workflow_name or "").lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "workflow"


def build_runner_package(
    plan: GraphPlan,
    *,
    workflow_name: str | None = None,
    lint: bool = True,
) -> dict[str, Any]:
    """Write a self-contained package for `plan` and describe where it landed.

    Returns package_dir (as the user sees it), local_package_dir (where this
    process actually wrote), compose_command, service_url, service_port,
    files and any warnings the compiler or renderer raised.
    """
    name = workflow_name or plan.workflow_name
    slug = package_slug(name)

    # PACKAGES_DIR is where this process writes; HOST_PACKAGES_DIR is the same
    # directory as the user sees it, which differs when the backend runs in a
    # container with the packages directory bind-mounted.
    packages_root = Path(
        os.environ.get("PACKAGES_DIR", str(Path(__file__).parent.parent.parent / "packages"))
    )
    host_root = Path(os.environ.get("HOST_PACKAGES_DIR", str(packages_root)))

    dir_name = f"{slug}-{plan.version_id[:8]}"
    destination = packages_root / dir_name
    port = _free_port()

    rendered = render_package(plan, destination, port=port)
    warnings = list(rendered.warnings)

    if lint:
        warnings.extend(_lint(destination))
    warnings.extend(_git_init(destination, name))

    host_dir = str(host_root / dir_name)
    return {
        # The path as the *user* sees it, for the copyable path and the compose
        # command. Inside a container this is a host path that does not exist
        # here, so anything running the package in this process must use
        # `local_package_dir` instead.
        "package_dir": host_dir,
        "local_package_dir": str(destination),
        "compose_command": f"cd {host_dir} && docker compose up --build -d",
        "service_url": f"http://localhost:{port}",
        "service_port": port,
        "files": rendered.files,
        "warnings": warnings,
    }


def _lint(root: Path) -> list[str]:
    """Run ruff over the generated tree if it is available.

    Restricted to pyflakes (`F`) rules: an unused import or an undefined name
    is a generator bug worth reporting, whereas ruff's style rules are opinions
    about code nobody hand-writes.

    Advisory: the package is already known to compile and import (render.py
    gates on both), so a finding is a quality signal about the generator, not a
    reason to withhold the package. Reported as a warning so it surfaces
    without blocking.
    """
    ruff = shutil.which("ruff")
    if not ruff:
        return []

    try:
        result = subprocess.run(
            [ruff, "check", "--no-cache", "--select", "F", "--output-format=concise", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ruff could not be run over %s: %s", root, exc)
        return []

    if result.returncode == 0:
        return []

    # Concise format is one finding per line; the trailing summary lines are not.
    findings = [
        line
        for line in (result.stdout or "").splitlines()
        if line.strip() and not line.startswith(("Found ", "[*]"))
    ]
    if not findings:
        return []

    shown = findings[:10]
    warnings = [f"lint: {line}" for line in shown]
    if len(findings) > len(shown):
        warnings.append(f"lint: … and {len(findings) - len(shown)} more finding(s)")
    return warnings


def _git_init(root: Path, workflow_name: str) -> list[str]:
    """Initialise a repo so the directory is ready to push.

    Best effort: a package without a git repo is still perfectly usable, so a
    failure here is reported rather than raised.
    """
    if not shutil.which("git"):
        return []

    commands = [
        ["git", "init", "--quiet"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", f"chore: generated package for {workflow_name}"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, **_GIT_ENV},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [f"git: {' '.join(command[:2])} failed ({exc})"]
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            return [f"git: {' '.join(command[:2])} failed ({detail[-1] if detail else 'unknown'})"]
    return []


def _free_port(start: int = 8080, end: int = 8200) -> int:
    """A host port nothing is listening on, for docker-compose to publish.

    Racy by nature — the port could be taken between here and `docker compose
    up`. It only picks a sensible default for the generated compose file, which
    the user can override with HOST_PORT.
    """
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("localhost", port)) != 0:
                return port
    return start


__all__ = ["build_runner_package", "package_slug", "RenderError"]
