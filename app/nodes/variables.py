"""Variable-name rules, shared by the compiler and the generated package.

The runtime copy lives in `app/packaging/template/core/variables.py`; this is
the compile-time half, so a bad name is a canvas error rather than a variable
that silently never appears.

Variables are flat, top-level session-state keys, which is how ADK does it:
`Event(state={"customer": "ACME"})` writes one and a node parameter named
`customer` is bound from it automatically. That flat namespace is shared, so
the reserved list below keeps canvas variables clear of the workflow's own
`wf` payload, a loop's `_loop_<node>_*` counters, and ADK's `app:` / `user:` /
`temp:` scopes.
"""

from __future__ import annotations

import re

RESERVED_NAMES = frozenset({"wf"})
RESERVED_PREFIXES = ("_loop_", "app:", "user:", "temp:", "_")

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def name_error(name: str) -> str | None:
    """Why `name` cannot be a variable, or None if it can."""
    cleaned = (name or "").strip()
    if not cleaned:
        return None  # not naming a variable is fine
    if not _NAME.match(cleaned):
        return (
            f"'{cleaned}' is not a usable variable name. Use a letter followed "
            "by letters, digits or underscores — ADK binds node parameters by "
            "name, and a parameter cannot be called that."
        )
    if cleaned in RESERVED_NAMES or cleaned.startswith(RESERVED_PREFIXES):
        return (
            f"'{cleaned}' is reserved. Workflow state uses 'wf', loops use "
            "'_loop_*', and ADK reserves 'app:', 'user:' and 'temp:'."
        )
    return None
