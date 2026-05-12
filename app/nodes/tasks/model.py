"""Model node — direct LLM call supporting Anthropic, Google AI Studio, and Vertex AI."""
from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata

_GOOGLE_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-pro-preview-03-25",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

_VERTEX_MODELS = [
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite-001",
    "gemini-2.5-pro-preview-03-25",
    "gemini-1.5-pro-001",
    "gemini-1.5-flash-001",
]


@dataclass
class ModelNode(NodeDefinition):
    node_type: str = "MODEL"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Model",
            category="ai",
            color="#10b981",
            icon="Brain",
            description="Direct LLM call — Anthropic or Google Gemini",
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["google", "vertex_ai"],
                    "default": "google",
                    "description": "LLM provider",
                },
                "model": {
                    "type": "string",
                    "default": "gemini-2.0-flash",
                    "description": "Model name",
                },
                "api_key": {
                    "type": "string",
                    "description": "Google AI Studio API key — overrides GOOGLE_API_KEY env var",
                },
                "vertex_project": {
                    "type": "string",
                    "description": "Google Cloud project ID (Vertex AI only)",
                },
                "vertex_location": {
                    "type": "string",
                    "default": "us-central1",
                    "description": "Vertex AI region",
                },
                "service_account_json": {
                    "type": "string",
                    "description": "Service account key JSON (Vertex AI) — leave empty to use ADC",
                },
                "system_prompt": {"type": "string"},
                "prompt_template": {
                    "type": "string",
                    "description": "Jinja2 template rendered with input_data fields",
                },
                "max_tokens": {"type": "integer", "minimum": 1, "default": 1024},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2, "default": 0.7},
                "response_format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "default": "text",
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "parsed": {},
                "usage": {"type": "object"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        provider = node_config.get("provider", "google")
        if provider == "vertex_ai":
            return await _call_vertex_ai(node_config, input_data)
        return await _call_gemini(node_config, input_data)


# ── Jinja2 prompt rendering ────────────────────────────────────────────────────

def _render_prompt(template: str, input_data: dict) -> str:
    if not template:
        import json
        return json.dumps(input_data)
    try:
        from jinja2.sandbox import SandboxedEnvironment
        env = SandboxedEnvironment()
        return env.from_string(template).render(**input_data, input=input_data)
    except Exception:
        return template


# ── Anthropic ─────────────────────────────────────────────────────────────────

async def _call_anthropic(config: dict, input_data: dict) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic package not installed", "_error": True}

    from app.config import get_settings
    import json

    settings = get_settings()
    api_key = config.get("api_key") or settings.anthropic_api_key
    model = config.get("model", "claude-sonnet-4-6")
    prompt = _render_prompt(config.get("prompt_template", ""), input_data)

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=config.get("max_tokens", 1024),
            system=config.get("system_prompt", ""),
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.content[0].text if response.content else ""
        parsed = None
        if config.get("response_format") == "json":
            try:
                parsed = json.loads(content)
            except Exception:
                pass
        return {
            "content": content,
            "parsed": parsed,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    except Exception as exc:
        return {"error": str(exc), "_error": True}


# ── Google Gemini ─────────────────────────────────────────────────────────────

async def _call_gemini(config: dict, input_data: dict) -> dict:
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed", "_error": True}

    import json as _json
    from app.config import get_settings

    settings = get_settings()
    api_key = config.get("api_key") or settings.google_api_key
    if not api_key:
        return {"error": "Google API key not configured", "_error": True}

    model = config.get("model", "gemini-2.0-flash")
    prompt = _render_prompt(config.get("prompt_template", ""), input_data)
    system_prompt = config.get("system_prompt", "")

    contents = []
    if system_prompt:
        contents.append({"role": "user", "parts": [{"text": f"System: {system_prompt}\n\n{prompt}"}]})
    else:
        contents.append({"role": "user", "parts": [{"text": prompt}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": config.get("max_tokens", 1024),
            "temperature": config.get("temperature", 0.7),
        },
    }

    import asyncio
    data: dict = {}
    last_exc: Exception | None = None
    for attempt in range(4):  # up to 3 retries with exponential backoff on 429
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post(url, params={"key": api_key}, json=payload)
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)
    else:
        return {"error": str(last_exc or "Google API rate limit — max retries exceeded"), "_error": True}

    candidates = data.get("candidates", [])
    content = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        content = "".join(p.get("text", "") for p in parts)

    token_meta = data.get("usageMetadata", {})
    parsed = None
    if config.get("response_format") == "json":
        try:
            parsed = _json.loads(content)
        except Exception:
            pass

    return {
        "content": content,
        "parsed": parsed,
        "usage": {
            "input_tokens": token_meta.get("promptTokenCount", 0),
            "output_tokens": token_meta.get("candidatesTokenCount", 0),
        },
    }


# ── Vertex AI (via google-genai unified SDK) ───────────────────────────────────

async def _call_vertex_ai(config: dict, input_data: dict) -> dict:
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return {"error": "google-genai package not installed (pip install google-genai)", "_error": True}

    import asyncio
    import json as _json
    from app.config import get_settings

    settings = get_settings()
    project = config.get("vertex_project") or settings.effective_vertex_project
    location = config.get("vertex_location") or settings.effective_vertex_location
    sa_json = config.get("service_account_json") or settings.google_service_account_json

    if not project:
        return {"error": "Vertex AI project ID not configured — enter your GCP Project ID in the node config", "_error": True}

    credentials = None
    if sa_json:
        try:
            from google.oauth2 import service_account
            info = _json.loads(sa_json)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except Exception as exc:
            return {"error": f"Invalid service account JSON: {exc}", "_error": True}

    try:
        client_kwargs: dict = {"vertexai": True, "project": project, "location": location}
        if credentials:
            client_kwargs["credentials"] = credentials
        client = genai.Client(**client_kwargs)

        model_name = config.get("model", "gemini-2.0-flash-001")
        prompt = _render_prompt(config.get("prompt_template", ""), input_data)
        system_prompt = config.get("system_prompt", "")

        gen_config = genai_types.GenerateContentConfig(
            max_output_tokens=config.get("max_tokens", 1024),
            temperature=config.get("temperature", 0.7),
            system_instruction=system_prompt or None,
        )

        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=gen_config,
        )

        content = response.text or ""
        parsed = None
        if config.get("response_format") == "json":
            try:
                parsed = _json.loads(content)
            except Exception:
                pass

        usage = response.usage_metadata
        return {
            "content": content,
            "parsed": parsed,
            "usage": {
                "input_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
                "output_tokens": getattr(usage, "candidates_token_count", 0) if usage else 0,
            },
        }
    except Exception as exc:
        return {"error": str(exc), "_error": True}
