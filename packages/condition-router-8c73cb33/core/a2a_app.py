"""Assembles the served application around the workflow graph.

`to_a2a()` returns a bare Starlette app carrying only the JSON-RPC route and the
two well-known agent-card routes -- no health check and no auth.  This module
adds what a deployed service needs:

  * GET  /health            liveness for Cloud Run / compose health checks
  * GET  /graph             the compiled graph, for debugging a running package
  * bearer-token auth       when A2A_AUTH_TOKEN is set
  * a persistent task store when TASK_STORE_DSN is set, so `tasks/get` keeps
    working across restarts and across more than one instance

The A2A routes are attached during lifespan startup, so anything appended to
`app.routes` here must be appended before the app starts serving.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.agent_card import agent_card
from core.config import settings

logger = logging.getLogger("workflow.a2a")

# Paths that stay reachable without a bearer token: liveness probes and agent
# discovery.  An agent card is public by design -- it is how other agents learn
# that authentication is required at all.
_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/.well-known/agent-card.json",
        "/.well-known/agent.json",
    }
)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Rejects calls that do not carry the configured bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        if request.headers.get("authorization") != self._expected:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": (
                            "Unauthorized. Send this agent's bearer token in the "
                            "Authorization header."
                        ),
                    },
                },
                status_code=401,
            )
        return await call_next(request)


async def _health(request: Request) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        {
            "status": "ok",
            "agent": settings.AGENT_NAME,
            "version": settings.AGENT_VERSION,
        }
    )


async def _graph(request: Request) -> JSONResponse:  # noqa: ARG001
    """The compiled graph plan, so a running package can be inspected."""
    path = Path(__file__).resolve().parent.parent / "graph.json"
    if not path.exists():
        return JSONResponse({"error": "graph.json not found"}, status_code=404)
    return JSONResponse(json.loads(path.read_text()))


def _build_task_store():
    """A persistent task store when a DSN is configured, else None.

    Returns `(store, dispose)`.  With no DSN, `to_a2a` falls back to its
    in-memory store: fine for a single local process, but `tasks/get` then fails
    for a poll routed to another instance, and restarts drop in-flight tasks.
    """
    dsn = settings.TASK_STORE_DSN.strip()
    if not dsn:
        return None, None

    try:
        from a2a.server.tasks import DatabaseTaskStore
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError as exc:
        logger.warning(
            "TASK_STORE_DSN is set but the database task store is unavailable "
            "(%s); falling back to in-memory tasks.",
            exc,
        )
        return None, None

    try:
        engine = create_async_engine(dsn)
    except Exception as exc:  # noqa: BLE001 - a config error, reported as one
        # Fail at start-up with a message naming the variable. Falling back to
        # in-memory would be worse: the operator asked for persistence, and
        # discovering it silently did not happen means a `tasks/get` returning
        # not-found much later, with nothing pointing at the cause.
        raise RuntimeError(
            f"TASK_STORE_DSN is not a usable SQLAlchemy URL ({exc}). "
            "Expected something like postgresql+asyncpg://user:pass@host:5432/db. "
            "Leave it blank to use in-memory tasks instead."
        ) from exc

    return DatabaseTaskStore(engine=engine), engine.dispose


def build_app(
    root_agent,
    *,
    extra_routes: list[Route] | None = None,
    runner=None,
    task_store=None,
) -> Starlette:
    """Wrap a Workflow (or Agent) as the application this package serves.

    `runner` overrides the in-memory Runner `to_a2a` would build. Passing one
    is how `run_once.py` observes each node as the graph steps: it hands in a
    Runner whose `run_async` tees every ADK event. Production leaves it None.

    `task_store` overrides the one built from `TASK_STORE_DSN`. `run_once.py`
    passes a store on a local SQLite file so a workflow that parks on a human
    approval can be answered by a *later* process -- the task has to outlive
    the one that created it. The caller owns anything it passes in, so this
    app does not dispose of it. Production leaves it None.
    """
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    if task_store is not None:
        dispose = None
    else:
        task_store, dispose = _build_task_store()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:  # noqa: ARG001
        # Not "serving at": this lifespan also runs under `run_once.py`, which
        # drives the app in-process through a TestClient and binds no port at
        # all. Claiming a URL there sends people to a dead localhost:8080.
        logger.info(
            "%s v%s ready — agent card advertises %s",
            settings.AGENT_NAME,
            settings.AGENT_VERSION,
            agent_card.url,
        )
        try:
            yield
        finally:
            if dispose is not None:
                await dispose()

    app = to_a2a(
        root_agent,
        host="0.0.0.0",
        port=settings.PORT,
        agent_card=agent_card,
        task_store=task_store,
        lifespan=lifespan,
        runner=runner,
    )

    # Appended before startup so they coexist with the A2A routes that
    # `to_a2a`'s lifespan attaches.
    app.routes.append(Route("/health", _health, methods=["GET"]))
    app.routes.append(Route("/graph", _graph, methods=["GET"]))
    for route in extra_routes or []:
        app.routes.append(route)

    if settings.A2A_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware, token=settings.A2A_AUTH_TOKEN)
    else:
        logger.warning(
            "A2A_AUTH_TOKEN is not set - this agent accepts unauthenticated calls."
        )

    return app
