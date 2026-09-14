"""MERGE: where the branches of a parallel fork come back together.

ADK's `JoinNode` waits for every predecessor and then passes the aggregated
inputs straight through — a dict keyed by predecessor node name, like
`{"n_transform_1": {...}, "n_transform_2": {...}}`. That is rarely what the
next node wants to read: a transform after a merge asks for `data["tax"]`, not
`data["n_transform_7"]["tax"]`.

So a MERGE is a JoinNode subclass that flattens before yielding. Subclassing
rather than adding a plain node afterwards keeps ADK's join semantics — which
is the part that cannot be reimplemented, since waiting for every predecessor
comes from `_requires_all_predecessors` on the node class itself.

`SOURCES` is the predecessor order taken from the canvas. The aggregated dict's
own order is arrival order, a race between branches, so anything that picks or
lists results uses this instead and gives the same answer every run.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import JoinNode


class MergeNode(JoinNode):
    """A JoinNode that combines its branches' outputs into one payload."""

    merge_mode: str = "merge"
    sources: list[str] = []

    def _ordered(self, node_input: Any) -> list[Any]:
        """Branch outputs in canvas order, with any stragglers on the end."""
        if not isinstance(node_input, dict):
            return [node_input] if node_input is not None else []
        ordered = [node_input[name] for name in self.sources if name in node_input]
        ordered += [v for k, v in node_input.items() if k not in self.sources]
        return ordered

    def _combine(self, node_input: Any) -> Any:
        values = self._ordered(node_input)
        if self.merge_mode == "array":
            return {"results": values}
        if self.merge_mode == "first":
            first = values[0] if values else {}
            return first if isinstance(first, dict) else {"result": first}
        combined: dict = {}
        for value in values:
            if isinstance(value, dict):
                combined.update(value)
        return combined

    async def _run_impl(
        self, *, ctx: Context, node_input: Any
    ) -> AsyncGenerator[Any, None]:
        yield Event(
            output=self._combine(node_input),
            branch=ctx._invocation_context.branch,
        )
