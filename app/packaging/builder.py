"""Packaging layer: compile IR → standalone FastAPI app → Docker image → running container."""
import json
import shutil
import socket
import subprocess
from pathlib import Path
from app.config import get_settings
from app.packaging.codegen import generate_standalone_app

settings = get_settings()

DOCKERFILE_TEMPLATE = """\
FROM {base_image}
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_BASE_REQUIREMENTS = """\
fastapi==0.111.0
uvicorn[standard]==0.29.0
pydantic==2.7.1
jmespath==1.0.1
jinja2==3.1.4
"""

_COMPOSE_BASE = """\
services:
  workflow:
    build: .
    ports:
      - "{port}:8000"
    restart: unless-stopped
"""

_COMPOSE_ENV_BLOCK = """\
    environment:
      - ANTHROPIC_API_KEY=${{ANTHROPIC_API_KEY}}
"""


def _build_requirements(ir_dict: dict) -> str:
    nodes = ir_dict.get("nodes", {}).values()
    types = {n.get("node_type") for n in nodes}
    reqs = _BASE_REQUIREMENTS
    if types & {"AGENT", "MODEL", "DATASOURCE", "TOOL"}:
        reqs += "httpx==0.27.0\n"
    if types & {"AGENT", "MODEL"}:
        reqs += "anthropic==0.28.0\n"
    return reqs


def _build_compose(port: int, ir_dict: dict) -> str:
    nodes = ir_dict.get("nodes", {}).values()
    types = {n.get("node_type") for n in nodes}
    compose = _COMPOSE_BASE.format(port=port)
    if types & {"AGENT", "MODEL"}:
        compose += _COMPOSE_ENV_BLOCK
    return compose


def build_runner_package(ir_dict: dict, version_id: str, workflow_name: str = "workflow") -> str:
    """Write standalone FastAPI app into <backend>/packages/<slug>/.  Returns the dir path.

    The backend dir is volume-mounted from the host (.:/app in docker-compose), so the
    packages/ folder is visible on the host at ./backend/packages/.
    """
    import re
    slug = workflow_name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-") or "workflow"

    import os
    # PACKAGES_DIR inside Docker = /packages (volume-mounted from host ./backend/packages/).
    # HOST_PACKAGES_DIR tells us the host-visible path for the same directory so the
    # compose_command and runner_dir shown in the UI are copy-paste-ready on the host.
    packages_root = Path(os.environ.get("PACKAGES_DIR", str(Path(__file__).parent.parent.parent / "packages")))
    host_root = Path(os.environ.get("HOST_PACKAGES_DIR", str(packages_root)))

    p = packages_root / f"{slug}-{version_id[:8]}"
    host_p = host_root / f"{slug}-{version_id[:8]}"

    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)

    port = _free_port()

    (p / "main.py").write_text(generate_standalone_app(ir_dict, workflow_name))
    (p / "requirements.txt").write_text(_build_requirements(ir_dict))
    (p / "Dockerfile").write_text(DOCKERFILE_TEMPLATE.format(base_image=settings.runner_base_image))
    (p / "docker-compose.yml").write_text(_build_compose(port, ir_dict))
    (p / "ir.json").write_text(json.dumps(ir_dict, indent=2))

    # Return the HOST path so the UI shows a usable path
    return str(host_p)


def build_docker_image(runner_dir: str, version_id: str) -> dict:
    """Build a Docker image, then start the container.

    Strategy:
      1. Python Docker SDK
      2. docker CLI subprocess
      3. Manual fallback (no daemon)
    """
    image_tag = f"{settings.docker_registry}/workflow:{version_id[:12]}"

    # ── Python SDK ────────────────────────────────────────────────────────────
    try:
        import docker as _sdk
        client = _sdk.from_env()
        image, logs = client.images.build(path=runner_dir, tag=image_tag, rm=True)
        log_lines = [l.get("stream", "").rstrip() for l in logs if l.get("stream", "").strip()]
        shutil.rmtree(runner_dir, ignore_errors=True)
        run_result = _docker_run_sdk(client, image_tag)
        return {
            "success": True, "image_tag": image_tag,
            "image_id": image.id, "logs": log_lines, "method": "sdk",
            **run_result,
        }
    except Exception:
        pass

    # ── CLI subprocess ────────────────────────────────────────────────────────
    _DAEMON_MSGS = (
        "Cannot connect to the Docker daemon",
        "docker daemon is not running",
        "Is the docker daemon running",
    )
    try:
        result = subprocess.run(
            ["docker", "build", "-t", image_tag, runner_dir],
            capture_output=True, text=True, timeout=300,
        )
        logs = (result.stdout + result.stderr).splitlines()
        if result.returncode == 0:
            shutil.rmtree(runner_dir, ignore_errors=True)
            run_result = _docker_run_cli(image_tag)
            return {
                "success": True, "image_tag": image_tag,
                "logs": logs, "method": "cli",
                **run_result,
            }
        if any(m in result.stderr for m in _DAEMON_MSGS):
            pass  # fall through to manual
        else:
            shutil.rmtree(runner_dir, ignore_errors=True)
            return {"success": False, "error": result.stderr[-2000:], "logs": logs}
    except FileNotFoundError:
        pass
    except Exception as exc:
        shutil.rmtree(runner_dir, ignore_errors=True)
        return {"success": False, "error": str(exc)}

    # ── Manual (no Docker daemon) ─────────────────────────────────────────────
    # docker-compose.yml is already written inside runner_dir with a free port
    compose_path = Path(runner_dir) / "docker-compose.yml"
    port = _free_port()
    if compose_path.exists():
        # Read the port that was baked into docker-compose.yml
        import re
        m = re.search(r'"(\d+):8000"', compose_path.read_text())
        if m:
            port = int(m.group(1))

    return {
        "success": True,
        "image_tag": image_tag,
        "runner_dir": runner_dir,
        "compose_command": f"cd {runner_dir} && docker compose up --build -d",
        "service_url": f"http://localhost:{port}",
        "docker_available": False,
        "logs": [
            "Docker daemon not reachable from the API process.",
            f"Package ready at: {runner_dir}",
            f"Run: cd {runner_dir} && docker compose up --build -d",
            f"Then open: http://localhost:{port}/docs",
        ],
        "method": "manual",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port(start: int = 8080, end: int = 8200) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    return start


def _docker_run_cli(image_tag: str) -> dict:
    port = _free_port()
    r = subprocess.run(
        ["docker", "run", "-d", "-p", f"{port}:8000",
         "-e", "ANTHROPIC_API_KEY=" + __import__("os").environ.get("ANTHROPIC_API_KEY", ""),
         image_tag],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        container_id = r.stdout.strip()[:12]
        return {
            "container_id": container_id,
            "service_url": f"http://localhost:{port}",
            "service_port": port,
        }
    return {"container_error": r.stderr.strip()}


def _docker_run_sdk(client, image_tag: str) -> dict:
    port = _free_port()
    try:
        import os
        container = client.containers.run(
            image_tag,
            detach=True,
            ports={"8000/tcp": port},
            environment={"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")},
        )
        return {
            "container_id": container.short_id,
            "service_url": f"http://localhost:{port}",
            "service_port": port,
        }
    except Exception as exc:
        return {"container_error": str(exc)}
