"""Packaging layer: compile IR → standalone self-contained project directory.

The output directory is a fully independent Python project.  It has:
  main.py           — standalone FastAPI app (zero imports from this backend)
  requirements.txt  — minimal runtime deps
  Dockerfile
  docker-compose.yml
  .env.example      — lists required env vars
  ir.json           — IR snapshot for debugging
  README.md         — quick-start instructions

Running `docker compose up --build` inside the directory starts the workflow as a
service.  No database, no main backend, no shared volumes required.
"""
import json
import shutil
import socket
from pathlib import Path
from app.config import get_settings
from app.packaging.codegen import generate_standalone_app

settings = get_settings()

DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim
WORKDIR /app
RUN pip install uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_BASE_REQUIREMENTS = """\
fastapi==0.111.0
uvicorn[standard]==0.29.0
pydantic>=2.7.1
jmespath==1.0.1
jinja2==3.1.4
"""

# google-adk requires fastapi>=0.115.0 — use a looser pin when ADK is present
_BASE_REQUIREMENTS_ADK = """\
fastapi>=0.115.0,<1.0.0
uvicorn[standard]>=0.29.0
pydantic>=2.7.1
jmespath==1.0.1
jinja2==3.1.4
"""

_COMPOSE_TEMPLATE = """\
services:
  workflow:
    build: .
    ports:
      - "{port}:8000"
    env_file:
      - .env
    volumes:
      - ~/.config/gcloud:/root/.config/gcloud:ro
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json
    restart: unless-stopped
"""

_ENV_EXAMPLE_BASE = """\
# Copy this file to .env and fill in real values before running.
"""


def _build_requirements(ir_dict: dict) -> str:
    node_list = list(ir_dict.get("nodes", {}).values())
    types = {n.get("node_type") for n in node_list}
    has_agentic = bool(types & {"AGENT", "ORCHESTRATOR_AGENT"})
    has_vertex_model = any(
        n.get("node_type") == "MODEL" and n.get("config", {}).get("provider") == "vertex_ai"
        for n in node_list
    )
    # google-adk and google-genai both require fastapi>=0.115.0
    needs_adk_fastapi = has_agentic or has_vertex_model
    reqs = _BASE_REQUIREMENTS_ADK if needs_adk_fastapi else _BASE_REQUIREMENTS
    if types & {"AGENT", "MODEL", "DATASOURCE", "TOOL", "ORCHESTRATOR_AGENT", "REMOTE_AGENT"}:
        reqs += "httpx>=0.28.1,<1.0.0\n"
    if has_agentic:
        reqs += "google-adk>=1.0.0\n"
    elif has_vertex_model:
        # google-adk isn't needed but google-genai is required for Vertex AI MODEL nodes
        reqs += "google-genai>=1.14.0\n"
    return reqs


def _build_env_example(ir_dict: dict) -> str:
    node_list = list(ir_dict.get("nodes", {}).values())
    types = {n.get("node_type") for n in node_list}
    has_vertex = any(
        n.get("config", {}).get("provider") == "vertex_ai" or
        n.get("config", {}).get("vertex_project")
        for n in node_list
    )
    lines = ["# Copy this file to .env and fill in your real values before running."]
    if types & {"MODEL", "AGENT", "ORCHESTRATOR_AGENT"}:
        if has_vertex:
            lines.append("GOOGLE_CLOUD_PROJECT=my-gcp-project-id")
            lines.append("VERTEX_LOCATION=us-central1")
            lines.append("")
            lines.append("# Auth — gcloud ADC is mounted automatically via docker-compose.yml volumes.")
            lines.append("# Run once on your host: gcloud auth application-default login")
            lines.append("# To use a service account key instead, set GOOGLE_SERVICE_ACCOUNT_JSON (single-line JSON):")
            lines.append("# GOOGLE_SERVICE_ACCOUNT_JSON={...}")
        else:
            lines.append("GOOGLE_API_KEY=AIzaSy...")
    return "\n".join(lines) + "\n"


def _build_env(ir_dict: dict) -> str:
    """Write a .env with blank keys so docker-compose doesn't error."""
    node_list = list(ir_dict.get("nodes", {}).values())
    types = {n.get("node_type") for n in node_list}
    has_vertex = any(
        n.get("config", {}).get("provider") == "vertex_ai" or
        n.get("config", {}).get("vertex_project")
        for n in node_list
    )
    lines = ["# Fill in real values — see .env.example for descriptions."]
    if types & {"MODEL", "AGENT", "ORCHESTRATOR_AGENT"}:
        if has_vertex:
            lines.append("GOOGLE_CLOUD_PROJECT=")
            lines.append("VERTEX_LOCATION=us-central1")
            lines.append("# GOOGLE_SERVICE_ACCOUNT_JSON=")
        else:
            lines.append("GOOGLE_API_KEY=")
    return "\n".join(lines) + "\n"


def _build_readme(workflow_name: str, port: int, ir_dict: dict) -> str:
    nodes = ir_dict.get("nodes", {}).values()
    node_types = sorted({n.get("node_type") for n in nodes})
    trigger = next((n for n in nodes if n.get("node_type", "").endswith("TRIGGER")), None)
    trigger_path = (trigger or {}).get("config", {}).get("path", "/run") if trigger else "/run"

    return f"""\
# {workflow_name} — Standalone Workflow

Auto-generated by the No-Code Platform.  This directory is a complete,
self-contained project that does **not** depend on the main backend.

## Quick start

```bash
# 1. Copy env template and fill in your keys
cp .env.example .env

# 2. Build and start
docker compose up --build -d

# 3. Trigger the workflow
curl -X POST http://localhost:{port}{trigger_path} \\
     -H "Content-Type: application/json" \\
     -d '{{"message": "hello"}}'

# 4. Browse the auto-generated API docs
open http://localhost:{port}/docs
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | Standalone FastAPI app — all workflow logic inlined |
| `requirements.txt` | Runtime Python dependencies |
| `Dockerfile` | Container build recipe |
| `docker-compose.yml` | Single-command start, maps port {port} |
| `.env.example` | Environment variable template |
| `ir.json` | Compiled workflow IR (useful for debugging) |

## Node types in this workflow

{chr(10).join(f"- `{t}`" for t in node_types)}

## Notes

- The `main.py` is generated code — **do not edit it by hand**.
  Re-package from the platform after making canvas changes.
- All secrets are read from environment variables (see `.env.example`).
  Never commit a populated `.env` file.
"""


def build_runner_package(ir_dict: dict, version_id: str, workflow_name: str = "workflow") -> dict:
    """Write a fully self-contained project directory and return metadata about it.

    Returns a dict with:
      package_dir    — absolute path to the generated directory
      compose_command — copy-paste command to start the container
      service_url    — expected URL once the container is running
      service_port   — port number
      files          — list of generated file names
    """
    import re
    slug = workflow_name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-") or "workflow"

    import os
    packages_root = Path(os.environ.get("PACKAGES_DIR", str(Path(__file__).parent.parent.parent / "packages")))
    host_root = Path(os.environ.get("HOST_PACKAGES_DIR", str(packages_root)))

    dir_name = f"{slug}-{version_id[:8]}"
    p = packages_root / dir_name
    host_p = host_root / dir_name

    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)

    port = _free_port()

    # Write all project files
    (p / "main.py").write_text(generate_standalone_app(ir_dict, workflow_name))
    (p / "requirements.txt").write_text(_build_requirements(ir_dict))
    (p / "Dockerfile").write_text(DOCKERFILE_TEMPLATE)
    (p / "docker-compose.yml").write_text(_COMPOSE_TEMPLATE.format(port=port))
    (p / ".env.example").write_text(_build_env_example(ir_dict))
    (p / ".env").write_text(_build_env(ir_dict))
    (p / "ir.json").write_text(json.dumps(ir_dict, indent=2))
    (p / "README.md").write_text(_build_readme(workflow_name, port, ir_dict))

    # Initialise a git repo so the directory is ready to push
    try:
        import subprocess
        subprocess.run(["git", "init"], cwd=str(p), capture_output=True, timeout=10)
        (p / ".gitignore").write_text(".env\n__pycache__/\n*.pyc\n")
        subprocess.run(["git", "add", "-A"], cwd=str(p), capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", f"chore: generated package for {workflow_name}"],
            cwd=str(p), capture_output=True, timeout=10,
            env={**os.environ, "GIT_AUTHOR_NAME": "no-code-platform", "GIT_AUTHOR_EMAIL": "bot@nocode",
                 "GIT_COMMITTER_NAME": "no-code-platform", "GIT_COMMITTER_EMAIL": "bot@nocode"},
        )
    except Exception:
        pass  # git is best-effort; package is still usable without it

    host_dir = str(host_p)
    compose_command = f"cd {host_dir} && docker compose up --build -d"

    return {
        "package_dir": host_dir,
        "compose_command": compose_command,
        "service_url": f"http://localhost:{port}",
        "service_port": port,
        "files": ["main.py", "requirements.txt", "Dockerfile", "docker-compose.yml",
                  ".env.example", ".gitignore", "ir.json", "README.md"],
    }


def _free_port(start: int = 8080, end: int = 8200) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    return start
