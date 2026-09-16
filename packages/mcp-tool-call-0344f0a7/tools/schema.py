"""JSON Schema -> pydantic model, for an agent's declared input structure.

ADK's two schema fields are not symmetric, and this module exists because of
the asymmetry:

* `LlmAgent.output_schema` accepts a **plain dict**, so a canvas output
  structure is passed straight through with no conversion at all.
* `LlmAgent.input_schema` is typed `Optional[type[BaseModel]]` — a dict is
  rejected. So an input structure has to be turned into a real pydantic class,
  which is what `model_from_schema` does.

ADK derives the tool declaration a calling model sees from that class, via
`model_json_schema()`. Two things worth knowing about that path, both verified
against ADK 2.8.0 rather than assumed:

* The declaration lands in `parameters_json_schema`, not `parameters`. Reading
  the wrong one makes a correctly-declared tool look like it takes no arguments.
* An `Optional[...]` field serialises to an `anyOf`, and that is fine — ADK
  passes it through and the required/optional split survives.
"""

from __future__ import annotations

import keyword
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger("workflow.tools.schema")

# JSON Schema type -> Python annotation.
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}

# The parameter an agent takes when it declares no input structure of its own.
FALLBACK_FIELD = "request"


def safe_model_name(name: str) -> str:
    """A class name derived from a canvas name, always a valid identifier."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", name or "").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"M{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return "".join(part[:1].upper() + part[1:] for part in cleaned.split("_") if part) or "Input"


def model_from_schema(name: str, schema: dict[str, Any] | None) -> type[BaseModel] | None:
    """Build a pydantic model from a JSON Schema object.

    Returns None when there is nothing to build, so a caller can leave ADK's
    `input_schema` unset rather than handing it an empty model — an empty schema
    would tell the calling model this agent takes no arguments at all.
    """
    properties = (schema or {}).get("properties") or {}
    if not properties:
        return None

    required = set((schema or {}).get("required") or [])
    fields: dict[str, Any] = {}

    for key, spec in properties.items():
        if not isinstance(key, str) or not key.isidentifier():
            logger.warning("skipping schema field %r: not a valid parameter name", key)
            continue
        spec = spec if isinstance(spec, dict) else {}
        annotation = _PY_TYPES.get(spec.get("type"), str)
        description = spec.get("description") or None

        if key in required:
            fields[key] = (annotation, Field(description=description))
        else:
            # Optional so a caller may omit it; the default keeps it out of the
            # declaration's `required` list.
            fields[key] = (
                Optional[annotation],
                Field(default=spec.get("default"), description=description),
            )

    if not fields:
        return None
    return create_model(safe_model_name(name), **fields)


def fallback_input_model(name: str, description: str = "") -> type[BaseModel]:
    """The one-parameter model used when an agent declares no input structure.

    Without any declared parameters a calling model has no way to pass anything,
    so an agent used as a tool always needs at least this.
    """
    return create_model(
        safe_model_name(name),
        **{
            FALLBACK_FIELD: (
                str,
                Field(description=description or "What you want this agent to do."),
            )
        },
    )
