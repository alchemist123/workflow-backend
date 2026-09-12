"""Deployment concerns: auth, task persistence, and the Cloud Run contract.

These cover files copied verbatim into every generated package (`core/config.py`,
`core/agent_card.py`, `core/a2a_app.py`), so proving them once here proves them
for every package — `tests/test_template_integrity.py` in the platform repo
asserts those files are copied unchanged.

Each test builds its own app because `core.config` reads the environment at
import time, which is what makes the Vertex AI backend selection stick.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Re-importing the config chain picks up a changed environment.
_CONFIG_MODULES = ("core.config", "core.agent_card", "core.a2a_app", "agent", "main")


def _fresh_app(monkeypatch, **env):
    """Build the app with these environment variables in force."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for name in _CONFIG_MODULES:
        if name in sys.modules:
            del sys.modules[name]
    return importlib.import_module("main").a2a_app


def _rpc_body(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _send(payload: dict, blocking: bool = True) -> dict:
    return _rpc_body(
        "message/send",
        {
            "message": {
                "role": "user",
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": json.dumps(payload)}],
            },
            "configuration": {"blocking": blocking},
        },
    )


# ── Inbound auth ─────────────────────────────────────────────────────────────


def test_without_a_token_the_agent_is_open(monkeypatch):
    """The default is open, and core/a2a_app.py logs a warning saying so."""
    app = _fresh_app(monkeypatch, A2A_AUTH_TOKEN="")
    with TestClient(app) as client:
        response = client.post("/", json=_send({"text": "hi"}))
        assert response.status_code == 200

        card = client.get("/.well-known/agent-card.json").json()
        assert card.get("security") is None
        assert card.get("securitySchemes") is None


def test_a_token_is_both_advertised_and_enforced(monkeypatch):
    """Enforcing without advertising leaves a caller to discover auth by failing."""
    app = _fresh_app(monkeypatch, A2A_AUTH_TOKEN="s3cr3t")
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        assert card["security"] == [{"bearerAuth": []}]
        scheme = card["securitySchemes"]["bearerAuth"]
        assert scheme["type"] == "http"
        assert scheme["scheme"] == "bearer"

        # Enforced.
        assert client.post("/", json=_send({"text": "hi"})).status_code == 401
        assert client.post(
            "/", json=_send({"text": "hi"}), headers={"Authorization": "Bearer wrong"}
        ).status_code == 401

        ok = client.post(
            "/", json=_send({"text": "hi"}), headers={"Authorization": "Bearer s3cr3t"}
        )
        assert ok.status_code == 200
        assert ok.json()["result"]["status"]["state"] == "completed"


def test_rejection_is_a_jsonrpc_error_not_bare_html(monkeypatch):
    """An A2A client parses JSON-RPC; an HTML 401 would be unreadable to it."""
    app = _fresh_app(monkeypatch, A2A_AUTH_TOKEN="s3cr3t")
    with TestClient(app) as client:
        body = client.post("/", json=_send({"text": "hi"})).json()

    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32001
    assert "token" in body["error"]["message"].lower()


def test_discovery_and_health_stay_public(monkeypatch):
    """A card is how a caller learns auth is required, so it cannot need auth.
    Cloud Run's probe cannot send a token either."""
    app = _fresh_app(monkeypatch, A2A_AUTH_TOKEN="s3cr3t")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/.well-known/agent-card.json").status_code == 200
        assert client.get("/.well-known/agent.json").status_code == 200


# ── Task persistence ─────────────────────────────────────────────────────────


def test_without_a_dsn_tasks_live_only_in_this_process(monkeypatch):
    app = _fresh_app(monkeypatch, TASK_STORE_DSN="", A2A_AUTH_TOKEN="")
    with TestClient(app) as client:
        task = client.post("/", json=_send({"text": "hi"})).json()["result"]
        # Visible here...
        polled = client.post("/", json=_rpc_body("tasks/get", {"id": task["id"]})).json()
        assert polled["result"]["id"] == task["id"]
    return task["id"]


def test_a_dsn_persists_tasks_to_the_database(monkeypatch, tmp_path):
    database = tmp_path / "tasks.db"
    app = _fresh_app(
        monkeypatch,
        TASK_STORE_DSN=f"sqlite+aiosqlite:///{database}",
        A2A_AUTH_TOKEN="",
    )
    with TestClient(app) as client:
        task = client.post("/", json=_send({"text": "persist me"})).json()["result"]
        assert task["status"]["state"] == "completed"

    assert database.exists(), "no database file was created"
    connection = sqlite3.connect(database)
    tables = [row[0] for row in connection.execute(
        "select name from sqlite_master where type='table'"
    )]
    assert "tasks" in tables
    stored = connection.execute("select count(*) from tasks").fetchone()[0]
    assert stored >= 1


def test_a_persisted_task_survives_a_restart(monkeypatch, tmp_path):
    """The failure this exists to prevent: on Cloud Run a poll routed to another
    instance, or made after a restart, must not return not-found."""
    database = tmp_path / "tasks.db"
    dsn = f"sqlite+aiosqlite:///{database}"

    app = _fresh_app(monkeypatch, TASK_STORE_DSN=dsn, A2A_AUTH_TOKEN="")
    with TestClient(app) as client:
        task_id = client.post("/", json=_send({"text": "hi"})).json()["result"]["id"]

    # A genuinely separate process, standing in for another instance.
    probe = (
        "import json,sys;"
        "from starlette.testclient import TestClient;"
        "from main import a2a_app;"
        "c=TestClient(a2a_app);"
        "c.__enter__();"
        "r=c.post('/', json={'jsonrpc':'2.0','id':'1','method':'tasks/get',"
        f"'params':{{'id':{task_id!r}}}}});"
        "print(json.dumps(r.json()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PACKAGE_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(PACKAGE_ROOT),
            "PYTHONWARNINGS": "ignore",
            "TASK_STORE_DSN": dsn,
            "A2A_AUTH_TOKEN": "",
            "AGENT_NAME": "text_triage",
        },
    )
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert "error" not in payload, payload
    assert payload["result"]["id"] == task_id
    assert payload["result"]["status"]["state"] == "completed"


def test_an_unusable_dsn_fails_at_startup_with_a_clear_message(monkeypatch):
    """A typo must not degrade silently.

    Falling back to in-memory would hide a real misconfiguration: the operator
    asked for persistence, and the symptom would surface much later as a
    `tasks/get` returning not-found with nothing pointing at the cause. Failing
    at start-up, naming the variable, is the kinder outcome.
    """
    with pytest.raises(RuntimeError, match="TASK_STORE_DSN"):
        _fresh_app(monkeypatch, TASK_STORE_DSN="not-a-real-dsn://nowhere", A2A_AUTH_TOKEN="")


# ── Cloud Run contract ───────────────────────────────────────────────────────


def test_cloud_run_url_becomes_the_advertised_url(monkeypatch):
    """The card's url is how other agents reach this one, so it must be the
    externally reachable address, not the in-container one."""
    _fresh_app(monkeypatch, CLOUD_RUN_URL="https://wf-abc123-uc.a.run.app", PORT="8080")
    from core.agent_card import build_agent_card

    assert build_agent_card().url == "https://wf-abc123-uc.a.run.app"


def test_without_cloud_run_url_it_falls_back_to_the_local_port(monkeypatch):
    _fresh_app(monkeypatch, CLOUD_RUN_URL="", PORT="9123")
    from core.config import public_url

    assert public_url() == "http://localhost:9123"


def test_agent_name_must_be_a_valid_identifier(monkeypatch):
    """agent.py passes AGENT_NAME to Workflow(name=...), which ADK validates as
    a node name. A hyphen or a space in .env would break the graph at import."""
    with pytest.raises(Exception):
        _fresh_app(monkeypatch, AGENT_NAME="not an identifier")


def test_dockerfile_serves_on_the_injected_port():
    """Cloud Run injects PORT at start-up, so it has to be expanded then."""
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text()

    assert "${PORT}" in dockerfile
    # JSON form so signals reach the process, wrapped in sh -c for expansion.
    assert 'CMD ["sh", "-c"' in dockerfile
    # Not running as root.
    assert "USER agent" in dockerfile


def test_secrets_are_kept_out_of_the_image():
    """.env holds credentials, so it must not be copied into the image."""
    dockerignore = (PACKAGE_ROOT / ".dockerignore").read_text()
    gitignore = (PACKAGE_ROOT / ".gitignore").read_text()

    assert ".env" in dockerignore.split()
    assert ".env" in gitignore.split()


# ── Generated per-tool environment variables ─────────────────────────────────


def test_env_value_reads_a_generated_key_despite_case_folding(tmp_path, monkeypatch):
    """`getattr(settings, "MCP_X_URL")` does not work, and fails silently.

    pydantic-settings is case-insensitive by default, so a key the Settings
    class does not declare -- every key the compiler generates -- is kept in
    `model_extra` lowercased. The uppercase lookup then returns nothing, the
    tool is built with an empty URL, and the only trace is a
    "has no URL; skipping" line while the run still reports success.
    """
    env = tmp_path / ".env"
    env.write_text(
        "AGENT_NAME=probe\n"
        "MCP_WEATHER_URL=http://mcp.example/mcp\n"
        "A2A_CALC_URL=http://agent.example\n"
    )
    monkeypatch.chdir(tmp_path)

    from core.config import Settings

    settings = Settings()

    # The bug, pinned: the uppercase attribute is simply absent.
    assert getattr(settings, "MCP_WEATHER_URL", None) is None
    # ...and the accessor finds it anyway.
    assert settings.env_value("MCP_WEATHER_URL") == "http://mcp.example/mcp"
    assert settings.env_value("A2A_CALC_URL") == "http://agent.example"


def test_env_value_still_reads_declared_fields(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AGENT_NAME=probe\nMCP_TRANSPORT=sse\n")
    monkeypatch.chdir(tmp_path)

    from core.config import Settings

    assert Settings().env_value("MCP_TRANSPORT") == "sse"


def test_env_value_falls_back_when_a_key_is_absent_or_blank(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AGENT_NAME=probe\nMCP_EMPTY_TOKEN=\n")
    monkeypatch.chdir(tmp_path)

    from core.config import Settings

    settings = Settings()
    assert settings.env_value("NOT_SET") == ""
    assert settings.env_value("NOT_SET", "fallback") == "fallback"
    # A blank value is the same as absent: an empty token means "no token".
    assert settings.env_value("MCP_EMPTY_TOKEN") == ""


def test_a_real_environment_variable_is_read_too(tmp_path, monkeypatch):
    """Deployment sets these as real env vars, not via .env."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_NAME=probe\n")
    monkeypatch.setenv("MCP_DEPLOYED_URL", "http://from-env/mcp")

    from core.config import Settings

    assert Settings().env_value("MCP_DEPLOYED_URL") == "http://from-env/mcp"
