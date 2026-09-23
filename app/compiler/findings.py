"""A validation result that knows what it is about.

The validator used to produce two lists of prose. Good enough for a compile
log, useless for a canvas: `Node 'node_1731…' (TOOL) cannot be used as a tool`
names an id nobody has ever seen on screen, and there is no way to work out
which box to mark. So each result now carries the node or edge it concerns, and
the canvas can put the mark where the mistake is.

**The messages themselves are untouched.** Eleven test files assert on
substrings of them and the compile log shows them verbatim, so the migration
added an anchor at each call site and changed no wording at all;
`tests/test_validator_messages_golden.py` pins that.

For the canvas a message beginning `Node '<id>' (<TYPE>)` reads badly — the id
is meaningless to whoever drew the node. `detail()` removes exactly that
prefix, by reconstructing it from the anchor rather than by matching a pattern:
if the message does not start with the string we would have generated, it is
returned whole. So the canvas can head the sentence with the node's *title*
instead, and a reworded message degrades to showing the full text rather than
to showing nonsense.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Finding:
    """One thing the validator has to say, and what it is about."""

    message: str
    severity: Severity = "error"

    node_id: str | None = None
    edge_id: str | None = None
    handle: str | None = None

    # Several rules concern a set rather than one thing: two ENDs, a cycle, a
    # pair of duplicate edges. One sentence, several places to mark.
    related_node_ids: tuple[str, ...] = ()
    related_edge_ids: tuple[str, ...] = ()

    # A stable id for the rule, where one is useful to the canvas. Most rules
    # do not need one: marking the right node and showing the sentence is the
    # whole job, and inventing sixty codes nothing reads would be ceremony.
    code: str = ""

    @property
    def subject(self) -> str:
        """What the canvas should attach this to."""
        if self.edge_id:
            return "edge"
        if self.node_id:
            return "node"
        return "workflow"

    def detail(self, node_types: Mapping[str, str]) -> str:
        """The message without its `Node '<id>' (<TYPE>)` opening, if it has one."""
        if not self.node_id:
            return self.message
        prefix = f"Node '{self.node_id}'"
        node_type = node_types.get(self.node_id)
        if node_type:
            prefix = f"{prefix} ({node_type})"
        if not self.message.startswith(prefix):
            return self.message
        return self.message[len(prefix):].lstrip(": ").strip() or self.message

    def to_dict(self, node_types: Mapping[str, str]) -> dict[str, Any]:
        """For the wire.

        `text` is the message exactly as the compile log shows it; `message` is
        the same sentence with the node prefix removed, for a canvas that has
        somewhere better to put the subject.
        """
        return {
            "code": self.code,
            "severity": self.severity,
            "text": self.message,
            "message": self.detail(node_types),
            "subject": self.subject,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "handle": self.handle,
            "related_node_ids": list(self.related_node_ids),
            "related_edge_ids": list(self.related_edge_ids),
        }


@dataclass
class Findings:
    """An ordered collection, with the two appenders the validator uses.

    One ordered list, partitioned into errors and warnings only at the end: the
    two legacy lists were each append-ordered and independent, so a stable
    partition reproduces both exactly and in the same order.
    """

    items: list[Finding] = field(default_factory=list)

    def error(self, message: str, **anchors: Any) -> None:
        self.items.append(Finding(message, "error", **_clean(anchors)))

    def warn(self, message: str, **anchors: Any) -> None:
        self.items.append(Finding(message, "warning", **_clean(anchors)))

    def extend_errors(self, messages: Iterable[str], **anchors: Any) -> None:
        """For a rule whose text comes from elsewhere, one finding per string."""
        for message in messages:
            self.error(message, **anchors)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.items)

    def as_strings(self, node_types: Mapping[str, str]) -> tuple[list[str], list[str]]:
        """The `(errors, warnings)` pair every existing caller expects."""
        return (
            [f.message for f in self.items if f.severity == "error"],
            [f.message for f in self.items if f.severity == "warning"],
        )


def _clean(anchors: dict[str, Any]) -> dict[str, Any]:
    """Accept `node=`/`edge=` as shorthand, and tuple-ify the related lists."""
    out = dict(anchors)
    if "node" in out:
        node = out.pop("node")
        out["node_id"] = getattr(node, "id", node)
    if "edge" in out:
        edge = out.pop("edge")
        out["edge_id"] = getattr(edge, "id", edge)
    for key in ("related_node_ids", "related_edge_ids"):
        if key in out and out[key] is not None:
            out[key] = tuple(out[key])
    return out
