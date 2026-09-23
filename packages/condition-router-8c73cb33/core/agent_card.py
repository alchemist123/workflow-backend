"""The A2A agent card this workflow advertises.

Generated from the canvas. Pinned to a2a-sdk 0.3.x on purpose: in 1.x AgentCard
became a protobuf message that dropped `url`, `preferred_transport` and
`protocol_version` in favour of a repeated `supported_interfaces`, so this
construction would not compile there. Keeping card construction in this one
module means a future move to 1.x is a single-file change.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)

from core.config import public_url, settings

# The name the card gives its bearer scheme. Callers read this to learn that a
# token is required before they get a 401.
_BEARER_SCHEME = "bearerAuth"


def _security() -> tuple[dict[str, SecurityScheme] | None, list[dict[str, list[str]]] | None]:
    """Advertise the auth this agent enforces.

    core/a2a_app.py rejects an unauthenticated call when A2A_AUTH_TOKEN is set,
    but enforcing without advertising leaves a caller to discover it by failing.
    Declaring the scheme here is what makes the requirement discoverable from
    the agent card, which is the whole point of having one.
    """
    if not settings.A2A_AUTH_TOKEN:
        return None, None

    scheme = SecurityScheme(
        root=HTTPAuthSecurityScheme(
            type="http",
            scheme="bearer",
            description="A bearer token issued by whoever operates this agent.",
        )
    )
    return {_BEARER_SCHEME: scheme}, [{_BEARER_SCHEME: []}]


# ── Skills ────────────────────────────────────────────────────────────────────
SKILLS: list[AgentSkill] = [
    AgentSkill(
        id="run_workflow",
        name="Run Condition Router",
        description="Routes by score threshold \u2014 high (>50) gets premium tier, low gets standard.",
        tags=['workflow', 'a2a', 'adk'],
    ),
]


def build_agent_card() -> AgentCard:
    """The card served at /.well-known/agent-card.json."""
    security_schemes, security = _security()
    return AgentCard(
        capabilities=AgentCapabilities(
            # Streaming is off: the workflow answers with a single result
            # artifact. Callers poll `tasks/get` for long runs instead.
            streaming=False,
            push_notifications=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        description=settings.AGENT_DESCRIPTION,
        name=settings.AGENT_NAME,
        url=public_url(),
        version=settings.AGENT_VERSION,
        preferred_transport="JSONRPC",
        protocol_version="0.3.0",
        skills=SKILLS,
        security_schemes=security_schemes,
        security=security,
    )


agent_card = build_agent_card()
