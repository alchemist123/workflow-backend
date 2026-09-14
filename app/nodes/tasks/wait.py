"""WAIT — hold the run for a fixed time, then carry on.

ADK has no delay node. Searching the package for sleep / delay / schedule /
timer turns up only retry backoff, polling loops and fixed internal delays;
`workflow/_trigger.py` sounds relevant but `Trigger` is just the data model for
a downstream node's input, with no timing in it. So this is ours to build.

It is `asyncio.sleep` in the node. That is cooperative rather than blocking:
a wait on one branch does not hold up a sibling branch, measured at 2.0s total
for a 2s wait running beside a fast branch.

The alternative — parking the task the way HUMAN_APPROVAL does, and resuming it
when the time comes — was rejected. It would survive a restart, but *nothing in
the package would ever resume it*: a wait that needs an external scheduler to
fire is a scheduling integration, not a node, and every WAIT would become a
workflow that stops forever unless something pokes it.

What that choice costs, and what this node does about it:

* **The wait lives in the process.** A restart loses the run. Nothing can be
  done about that here; it is stated in the palette description and the docs.
* **A blocking caller holds its connection open** for the whole wait. Past a
  minute that is the wrong way to invoke the workflow, so the validator says so
  and points at task mode.
* **`@node(timeout=N)` kills a node that overruns** — verified:
  `NodeTimeoutError: Node 'slow' timed out after 1.0 seconds`. Nodes inherit
  `policies.timeout_seconds` (120s in the seeds), so a five-minute wait would
  die at two minutes with a timeout error that named the wrong problem. The
  renderer derives this node's timeout from its wait instead.
"""

from dataclasses import dataclass

from app.nodes.base import NodeDefinition, PaletteMetadata

# Seconds per unit offered on the canvas.
UNITS: dict[str, int] = {"seconds": 1, "minutes": 60}

# Past this a wait is a scheduling problem — something outside the workflow
# should start it later — rather than a pause inside one run.
MAX_WAIT_SECONDS = 3600

# Beyond this a blocking `message/send` caller is holding a connection open for
# an uncomfortable time, and should be using task mode.
BLOCKING_COMFORT_SECONDS = 60


@dataclass
class WaitNode(NodeDefinition):
    node_type: str = "WAIT"
    version: str = "1"
    palette: PaletteMetadata = None

    config_schema: dict = None
    input_schema: dict = None
    output_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="Wait",
            category="flow",
            color="#64748b",
            icon="Timer",
            description="Pause for a fixed time, then continue — held in the running process",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "duration": {
                    "type": "number",
                    "minimum": 0,
                    "default": 5,
                    "description": "How long to wait.",
                },
                "unit": {
                    "type": "string",
                    "enum": sorted(UNITS),
                    "default": "seconds",
                    "description": "Whether `duration` counts seconds or minutes.",
                },
            },
            "required": [],
        }
        self.input_schema = {"type": "object"}
        self.output_schema = {
            "type": "object",
            "properties": {
                # The payload passes through untouched; this is added so a later
                # node (or a reader of the trace) can see what happened here.
                "waited_seconds": {"type": "number"},
            },
        }
        self.output_handles = ["output"]


def wait_seconds(config: dict) -> float:
    """How long this node should wait, in seconds."""
    duration = (config or {}).get("duration")
    if duration is None:
        duration = 5
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return 0.0
    unit = (config or {}).get("unit") or "seconds"
    return max(0.0, duration * UNITS.get(unit, 1))


def node_timeout_for(config: dict) -> int:
    """The ADK node timeout this wait needs to survive.

    The wait plus a margin, rather than the canvas policy — a node whose whole
    job is to take a long time must not be killed for taking it.
    """
    return int(wait_seconds(config)) + 30
