"""Credentials configured on the canvas must not reach anything shareable.

A generated package is committed to git by the builder and serves its own
`graph.json` from `GET /graph`, so a credential embedded in either is exposed
twice over. The compiler moves every declared secret to an environment variable,
writes the canvas value as that variable's default in the gitignored `.env`, and
strips it from the config the plan carries — so the generated modules and
`graph.json` are clean by construction rather than by remembering to redact.
"""

from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path

import pytest

from app.compiler import build_graph_plan, run_compiler
from app.nodes.registry import NODE_REGISTRY
from app.packaging.builder import build_runner_package
from app.packaging.render import render_package
from app.schemas.canvas import CanvasPayload

warnings.filterwarnings("ignore", category=UserWarning)

# Distinctive strings so a match cannot be a coincidence.
API_KEY = "AIzaSy-CANARY-API-KEY"
SA_JSON = '{"type":"service_account","private_key":"CANARY-PRIVATE-KEY"}'
A2A_TOKEN = "CANARY-BEARER-TOKEN"
MCP_URL = "https://canary-user:canary-pw@mcp.example.com"

CANARIES = [API_KEY, "CANARY-PRIVATE-KEY", A2A_TOKEN, "canary-user:canary-pw"]


def _node(node_id: str, node_type: str, config: dict | None = None, **meta) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "version": "1",
        "position": {"x": 0, "y": 0},
        "metadata": {"title": meta.get("title", ""), "description": ""},
        "config": config if config is not None else {},
        "io": {"input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
        "policies": {"timeout_seconds": 60, "retry": {"max_attempts": 1}, "on_error": "fail"},
    }


def _edge(source: str, target: str, handle: str = "output", target_handle: str = "input") -> dict:
    return {
        "id": f"e_{source}_{target}_{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
        "target_handle": target_handle,
        "condition": None,
    }


_START = {
    "payload_schema": {
        "fields": [{"name": "q", "type": "string", "description": "", "required": True}]
    }
}


def _secret_plan(version_id: str = "secrets-0001"):
    """A canvas carrying every credential the config schemas allow."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START),
                _node(
                    "orch",
                    "ORCHESTRATOR_AGENT",
                    {
                        "model": "gemini-2.5-flash",
                        "system_prompt": "help",
                        "api_key": API_KEY,
                        "service_account_json": SA_JSON,
                        "vertex_project": "my-proj",
                    },
                    title="Brain",
                ),
                _node(
                    "remote",
                    "REMOTE_AGENT",
                    {"endpoint": "http://agent:8001", "name": "Helper", "auth_token": A2A_TOKEN},
                    title="Helper",
                ),
                _node("tool", "TOOL", {"mcp_url": MCP_URL, "tool_name": "search"}, title="Search"),
                _node(
                    "tool2",
                    "TOOL",
                    {"mcp_url": "https://second.example.com", "tool_name": "lookup"},
                    title="Lookup",
                ),
                _node("end", "END", {}),
            ],
            "edges": [
                _edge("start", "orch"),
                _edge("remote", "orch", "output", "tools"),
                _edge("tool", "orch", "output", "tools"),
                _edge("tool2", "orch", "output", "tools"),
                _edge("orch", "end"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, version_id)
    assert errors == [], errors
    return build_graph_plan(ir, workflow_name="Secret Audit")


# ── Declaration ──────────────────────────────────────────────────────────────


def test_credential_bearing_node_types_declare_their_secret_keys():
    """A node type that accepts a credential but does not declare it would leak."""
    expected = {
        "ORCHESTRATOR_AGENT": {"api_key", "service_account_json"},
        "AGENT": {"api_key", "service_account_json"},
        "LLM_AGENT": {"api_key", "service_account_json"},
        "REMOTE_AGENT": {"auth_token", "endpoint"},
        "TOOL": {"auth_token", "mcp_url"},
        "DATASOURCE": {"auth_token", "mcp_url"},
    }
    for node_type, keys in expected.items():
        assert NODE_REGISTRY[node_type].secret_config_keys == keys, node_type


def test_every_credentialish_config_key_is_declared_secret():
    """Catch a new config key that looks like a credential but was not declared.

    Suffix matching rather than substring, so `max_tokens` is not mistaken for
    one.
    """
    suffixes = ("_key", "_token", "_secret", "_password", "_credentials")
    contains = ("private_key", "credential")

    # Names that match the shape but hold a field name, not a credential. Listed
    # explicitly so a genuinely new credential still trips the check.
    benign = {"state_key", "output_key", "message_key", "items_key"}

    for node_type, definition in NODE_REGISTRY.items():
        properties = (definition.config_schema or {}).get("properties") or {}
        for key in properties:
            lowered = key.lower()
            if lowered in benign:
                continue
            looks_secret = lowered.endswith(suffixes) or any(
                word in lowered for word in contains
            )
            if looks_secret:
                assert key in definition.secret_config_keys, (
                    f"{node_type}.{key} looks like a credential but is not in "
                    "secret_config_keys, so it would be written into graph.json"
                )


# ── The plan ─────────────────────────────────────────────────────────────────


def test_secrets_are_stripped_from_the_plan():
    plan = _secret_plan()
    blob = json.dumps(plan.to_dict())

    for canary in CANARIES:
        assert canary not in blob, f"{canary} survived into the graph plan"


def test_secret_env_keys_hold_the_value_but_do_not_serialise_it():
    """The value has to travel somewhere -- as an env default, not in graph.json."""
    plan = _secret_plan()
    by_key = {k.key: k for k in plan.env_keys}

    # It reached the env key...
    assert by_key["GOOGLE_API_KEY"].default == API_KEY
    assert by_key["GOOGLE_API_KEY"].secret is True
    # ...but to_dict() withholds a secret's default.
    assert "default" not in by_key["GOOGLE_API_KEY"].to_dict()
    assert by_key["GOOGLE_API_KEY"].to_dict()["secret"] is True


def test_each_tool_gets_its_own_env_var():
    """An agent with two MCP servers needs two variables.

    Regression: the first implementation kept only the first binding per node,
    so a second server silently reused the first one's URL.
    """
    plan = _secret_plan()
    orch = next(n for n in plan.nodes if n.node_type == "ORCHESTRATOR_AGENT")
    resolved = orch.config["resolved_tools"]

    servers = {s["name"]: s["url_env"] for s in resolved["mcp_servers"]}
    assert servers == {"Search": "MCP_SEARCH_URL", "Lookup": "MCP_LOOKUP_URL"}

    agents = {a["name"]: (a["endpoint_env"], a["auth_token_env"]) for a in resolved["a2a_agents"]}
    assert agents == {"Helper": ("A2A_HELPER_URL", "A2A_HELPER_TOKEN")}

    # And the values are gone from the tool entries themselves.
    for server in resolved["mcp_servers"]:
        assert "url" not in server
    for agent in resolved["a2a_agents"]:
        assert "endpoint" not in agent
        assert "auth_token" not in agent

    keys = {k.key for k in plan.env_keys}
    assert {"MCP_SEARCH_URL", "MCP_LOOKUP_URL", "A2A_HELPER_URL", "A2A_HELPER_TOKEN"} <= keys


def test_env_var_names_stay_unique_for_duplicate_tool_titles():
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START),
                _node("t1", "TOOL", {"mcp_url": "http://a", "tool_name": "x"}, title="Search"),
                _node("t2", "TOOL", {"mcp_url": "http://b", "tool_name": "y"}, title="Search"),
                _node("end", "END", {}),
            ],
            "edges": [_edge("start", "t1"), _edge("t1", "t2"), _edge("t2", "end")],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "dupe-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Dupe")

    keys = [k.key for k in plan.env_keys]
    assert len(keys) == len(set(keys))
    assert "MCP_SEARCH_URL" in keys
    assert "MCP_SEARCH_2_URL" in keys


# ── The rendered package ─────────────────────────────────────────────────────


def _files_containing(root: Path, needles: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() or ".git" in path.parts:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        hits = [needle for needle in needles if needle in text]
        if hits:
            found[str(path.relative_to(root))] = hits
    return found


def test_only_dotenv_holds_credentials(tmp_path):
    plan = _secret_plan()
    root = tmp_path / "pkg"
    render_package(plan, root)

    found = _files_containing(root, CANARIES)
    assert set(found) == {".env"}, found
    # And .env holds all of them, so nothing was lost on the way.
    assert sorted(found[".env"]) == sorted(CANARIES)


def test_graph_json_is_safe_to_serve(tmp_path):
    """GET /graph returns this file verbatim."""
    plan = _secret_plan()
    root = tmp_path / "pkg"
    render_package(plan, root)

    blob = (root / "graph.json").read_text()
    for canary in CANARIES:
        assert canary not in blob


def test_env_example_blanks_secrets_but_documents_them(tmp_path):
    """.env.example is committed, so it names each credential without a value."""
    plan = _secret_plan()
    root = tmp_path / "pkg"
    render_package(plan, root)

    example = (root / ".env.example").read_text()

    for canary in CANARIES:
        assert canary not in example
    for key in ("GOOGLE_API_KEY", "MCP_SEARCH_URL", "A2A_HELPER_TOKEN"):
        # Named, and left blank.
        assert f"{key}=\n" in example, key
        assert "— credential" in example

    # The header explains why they are blank here.
    assert "committed" in example and ".gitignore" in example


def test_node_modules_read_credentials_from_settings_only(tmp_path):
    """A TOOL or REMOTE_AGENT *in the flow* gets its own module.

    (Wired to an agent's "tools" handle instead, it becomes an ADK tool and has
    no module -- covered by test_each_tool_gets_its_own_env_var.)
    """
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START),
                _node("tool", "TOOL", {"mcp_url": MCP_URL, "tool_name": "search"}, title="Search"),
                _node(
                    "remote",
                    "REMOTE_AGENT",
                    {"endpoint": "http://agent:8001", "name": "Helper", "auth_token": A2A_TOKEN},
                    title="Helper",
                ),
                _node("end", "END", {}),
            ],
            "edges": [
                _edge("start", "tool"),
                _edge("tool", "remote"),
                _edge("remote", "end"),
            ],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "inflow-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="In Flow")

    root = tmp_path / "pkg"
    render_package(plan, root)

    # Credentials reached .env only.
    assert set(_files_containing(root, [A2A_TOKEN, "canary-user:canary-pw"])) == {".env"}

    tool = next(n for n in plan.nodes if n.node_type == "TOOL")
    source = (root / f"nodes/{tool.name}.py").read_text()
    assert 'URL_ENV_KEY = "MCP_SEARCH_URL"' in source
    # No literal fallback: an MCP URL can carry basic-auth credentials.
    assert "URL_FALLBACK" not in source
    assert "settings.env_value(URL_ENV_KEY)" in source

    remote = next(n for n in plan.nodes if n.node_type == "REMOTE_AGENT")
    source = (root / f"nodes/{remote.name}.py").read_text()
    assert 'ENDPOINT_ENV_KEY = "A2A_HELPER_URL"' in source
    assert "ENDPOINT_FALLBACK" not in source


def test_git_never_tracks_a_credential(tmp_path, monkeypatch):
    """The builder commits the package, so this is what would be pushed."""
    monkeypatch.setenv("PACKAGES_DIR", str(tmp_path))
    monkeypatch.setenv("HOST_PACKAGES_DIR", str(tmp_path))

    built = build_runner_package(_secret_plan(), lint=False)
    root = Path(built["package_dir"])

    if not (root / ".git").exists():
        pytest.skip("git is unavailable")

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(root), capture_output=True, text=True, timeout=60
    ).stdout.split()

    assert ".env" not in tracked, ".env is tracked; credentials would be pushed"
    leaking = set(_files_containing(root, CANARIES)) & set(tracked)
    assert leaking == set(), f"tracked files contain credentials: {sorted(leaking)}"


def test_a_package_with_no_credentials_still_renders(tmp_path):
    """The sanitising pass must not require secrets to be present."""
    canvas = CanvasPayload.model_validate(
        {
            "nodes": [
                _node("start", "A2A_START", _START),
                _node("mid", "TRANSFORM", {"mode": "jmespath", "expression": "q"}),
                _node("end", "END", {}),
            ],
            "edges": [_edge("start", "mid"), _edge("mid", "end")],
        }
    )
    errors, _warnings, ir = run_compiler(canvas, "plain-0001")
    assert errors == [], errors
    plan = build_graph_plan(ir, workflow_name="Plain")

    render_package(plan, tmp_path / "pkg")
    keys = {k.key for k in plan.env_keys}
    # No model node, so no Google credentials are asked for.
    assert "GOOGLE_API_KEY" not in keys
    assert "MCP_TRANSPORT" not in keys
