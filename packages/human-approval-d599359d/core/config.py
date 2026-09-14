"""Settings for a generated workflow agent.

Every value comes from the environment (or `.env`).  The module-level
`os.environ.setdefault` calls at the bottom run *before* anything imports
`google.genai`, which is what makes the Vertex AI backend selection stick --
google-genai reads these variables at client construction time.

`.env.example` in this directory documents which node in the workflow needs
each key.
"""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Serving ───────────────────────────────────────────────────────────────
    PORT: int = 8080

    # ── Agent identity (advertised on the A2A agent card) ─────────────────────
    # AGENT_NAME must be a valid Python identifier: agent.py passes it to
    # `Workflow(name=...)`, which ADK validates as a node name and rejects if it
    # contains spaces or hyphens.
    AGENT_NAME: str = "workflow_agent"
    AGENT_DESCRIPTION: str = "A generated ADK graph workflow exposed over A2A."
    AGENT_VERSION: str = "1.0.0"

    # Public base URL of this service.  Becomes the agent card's `url`, which is
    # how other agents learn where to send JSON-RPC calls -- so it must be the
    # externally reachable address, not the in-container one.
    CLOUD_RUN_URL: str = ""

    # ── Google / Vertex AI ────────────────────────────────────────────────────
    GOOGLE_CLOUD_PROJECT: str = ""
    VERTEX_AI_LOCATION: str = "us-central1"
    LLM_MODEL: str = "gemini-2.5-flash"
    GOOGLE_GENAI_USE_VERTEXAI: str = "TRUE"

    # Service account key JSON, as a single line.  Leave empty to use
    # Application Default Credentials (the usual choice on Cloud Run).
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""

    # AI Studio API key -- an alternative to Vertex AI for local development.
    GOOGLE_API_KEY: str = ""

    # ── A2A task persistence ──────────────────────────────────────────────────
    # Empty means an in-memory task store: `tasks/get` then only works within
    # one process, and in-flight tasks are lost on restart.  Set a SQLAlchemy
    # async DSN (e.g. postgresql+asyncpg://...) to persist tasks so polling
    # survives restarts and more than one instance.
    TASK_STORE_DSN: str = ""

    # ── Inbound auth ──────────────────────────────────────────────────────────
    # When set, callers must present `Authorization: Bearer <token>`.
    A2A_AUTH_TOKEN: str = ""

    # ── MCP defaults ──────────────────────────────────────────────────────────
    MCP_TRANSPORT: str = "streamable_http"

    # `SettingsConfigDict` rather than a nested `class Config`: identical
    # behaviour, but the class-based form is deprecated in pydantic 2 and
    # generated packages should not emit deprecation warnings on import.
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="allow",  # generated per-node keys (MCP_*_URL, A2A_*_URL, ...)
    )

    def env_value(self, key: str, default: str = "") -> str:
        """Read one of the compiler-generated per-tool variables by name.

        Every key the compiler generates -- `MCP_<TOOL>_URL`,
        `A2A_<AGENT>_URL` and their tokens -- is undeclared on this class,
        because which ones exist depends on the canvas. pydantic-settings
        handles undeclared keys in two ways that both defeat a plain
        `getattr(settings, key)`, each failing silently:

        * From `.env` they are kept, but **lowercased**, because the model is
          case-insensitive by default. An uppercase lookup misses.
        * From the real environment they are not picked up at all: only
          declared fields are read from `os.environ`. That is the one that
          matters in deployment, where `.env` is gitignored and excluded from
          the image, so every tool URL arrives as a real environment variable.

        Either way the tool would be built with an empty URL and the only trace
        would be a "has no URL; skipping" line while the run reported success.
        The real environment wins over `.env`, which is the usual precedence.
        """
        environ_value = os.environ.get(key) or os.environ.get(key.upper())
        if environ_value:
            return environ_value

        for name in (key, key.lower()):
            value = getattr(self, name, None)
            if value is None and self.model_extra:
                value = self.model_extra.get(name)
            if value not in (None, ""):
                return str(value)
        return default


settings = Settings()


def _bootstrap_google_env() -> None:
    """Publish Google settings into os.environ before google-genai is imported."""
    if settings.GOOGLE_CLOUD_PROJECT:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", settings.GOOGLE_CLOUD_PROJECT)
    if settings.VERTEX_AI_LOCATION:
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", settings.VERTEX_AI_LOCATION)
    if settings.GOOGLE_GENAI_USE_VERTEXAI:
        # ADK 2.8 deprecated GOOGLE_GENAI_USE_VERTEXAI in favour of
        # GOOGLE_GENAI_USE_ENTERPRISE but still honours the old name. Set both:
        # the new one silences the deprecation warning, the old one keeps this
        # package working against older ADK builds.
        os.environ.setdefault(
            "GOOGLE_GENAI_USE_VERTEXAI", settings.GOOGLE_GENAI_USE_VERTEXAI
        )
        os.environ.setdefault(
            "GOOGLE_GENAI_USE_ENTERPRISE", settings.GOOGLE_GENAI_USE_VERTEXAI
        )

    # A service account key has to reach the client as a file path.
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON and not os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        handle.write(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        handle.flush()
        handle.close()
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = handle.name

    # Vertex AI and an AI Studio key are mutually exclusive backends; if both are
    # configured, Vertex wins and the key is removed so google-genai cannot
    # silently prefer it.
    if settings.GOOGLE_CLOUD_PROJECT:
        os.environ.pop("GOOGLE_API_KEY", None)
    elif settings.GOOGLE_API_KEY:
        os.environ["GOOGLE_API_KEY"] = settings.GOOGLE_API_KEY
        os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)


_bootstrap_google_env()


def public_url() -> str:
    """The externally reachable base URL, falling back to localhost for dev."""
    return settings.CLOUD_RUN_URL or f"http://localhost:{settings.PORT}"
