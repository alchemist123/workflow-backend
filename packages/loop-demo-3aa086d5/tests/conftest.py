"""Test fixtures for a generated workflow package.

The package's modules import each other by top-level name (`core.config`,
`nodes.registry`), because that is how they resolve inside the container where
the package root *is* the working directory. These tests run from the repo, so
the package root goes on sys.path here.
"""

from __future__ import annotations

import json
import sys
import uuid
import warnings
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

# ADK's A2A support is flagged experimental and warns on every call.
warnings.filterwarnings("ignore", category=UserWarning)


@pytest.fixture
def run_graph():
    """Run the workflow graph once and return (final_output, event_paths)."""

    async def _run(payload: dict):
        from google.adk import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        from agent import root_agent

        sessions = InMemorySessionService()
        runner = Runner(
            app_name="test", node=root_agent, session_service=sessions
        )
        await sessions.create_session(
            app_name="test", user_id="u", session_id="s"
        )

        message = types.Content(
            role="user", parts=[types.Part(text=json.dumps(payload))]
        )

        outputs: list = []
        paths: list[str] = []
        async for event in runner.run_async(
            user_id="u", session_id="s", new_message=message
        ):
            if event.node_info and event.node_info.path:
                paths.append(event.node_info.path)
            if event.output is not None:
                outputs.append(event.output)

        return (outputs[-1] if outputs else None), paths

    return _run


@pytest.fixture
def a2a_client():
    """A TestClient over the real served app, with a JSON-RPC helper attached."""
    from starlette.testclient import TestClient

    from main import a2a_app

    with TestClient(a2a_app) as client:

        def rpc(method: str, params: dict) -> dict:
            response = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": method,
                    "params": params,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        def message(payload: dict) -> dict:
            return {
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(payload)}],
                "messageId": str(uuid.uuid4()),
                "kind": "message",
            }

        client.rpc = rpc  # type: ignore[attr-defined]
        client.message = message  # type: ignore[attr-defined]
        yield client
