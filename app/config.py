from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "No-Code Platform"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nocode"
    database_url_sync: str = "postgresql://postgres:postgres@localhost:5432/nocode"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Security
    secret_key: str = "change-me-in-production"

    # Runtime
    default_execution_timeout: int = 300
    max_loop_iterations: int = 1000

    # Docker packaging
    docker_registry: str = "localhost:5000"
    runner_base_image: str = "python:3.11-slim"

    # LLM provider keys (for AGENT/MODEL/ORCHESTRATOR_AGENT nodes)
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # Vertex AI (alternative to Google AI Studio — higher quotas, enterprise auth)
    vertex_project: str = ""
    vertex_location: str = "us-central1"
    google_service_account_json: str = ""  # full service account key JSON string

    # Aliases matching common GCP env var conventions
    google_cloud_project: str = ""    # GOOGLE_CLOUD_PROJECT
    vertex_ai_location: str = ""      # VERTEX_AI_LOCATION

    @property
    def effective_vertex_project(self) -> str:
        return self.vertex_project or self.google_cloud_project

    @property
    def effective_vertex_location(self) -> str:
        return self.vertex_location or self.vertex_ai_location or "us-central1"

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
