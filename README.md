# No-Code A2A Workflow Platform — Engineering KT

This document is a code-level knowledge transfer for engineers new to this codebase. It explains every layer of the system, how data flows from a canvas click all the way to a running container, and how to add or change things without breaking anything.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Database Models](#3-database-models)
4. [The Node System](#4-the-node-system)
5. [The Compiler Pipeline](#5-the-compiler-pipeline)
6. [IR and the Graph Plan](#6-ir-and-the-graph-plan)
7. [Running a Workflow](#7-running-a-workflow)
8. [A2A and MCP Tool Protocols](#8-a2a-and-mcp-tool-protocols)
9. [The Packaging / Deploy Layer](#9-the-packaging--deploy-layer)
10. [Frontend Architecture](#10-frontend-architecture)
11. [End-to-End Walkthrough](#11-end-to-end-walkthrough)
12. [How to Add a New Node Type](#12-how-to-add-a-new-node-type)

---

## 1. System Overview

The platform lets users build AI workflows visually on a canvas, test them, and
package them for deployment. A workflow is a directed graph of **nodes** (an
entry point, AI agents, transforms, conditions, …) connected by **edges**.

A packaged workflow is a [Google ADK graph](https://adk.dev/graphs/) exposed as
an [A2A](https://a2a-protocol.org/) agent: other agents discover it from its
agent card and call it over JSON-RPC.

```
Canvas (browser)
    │  drag-and-drop nodes, draw edges
    ▼
Save & Compile (API)
    │  canvas JSON → validated IR JSON (stored in DB)
    ▼
Compile the graph plan
    │  IR → ADK node/edge plan, checked by ADK's own validator
    ▼
Render the package
    │  graph plan → a standalone project: one module per node,
    │  an ADK Workflow, an A2A server
    ▼
Test  ──or──  Deploy
    │  the platform drives the package it just rendered,
    │  over that package's own A2A surface
    ▼
Running container
    │  self-contained A2A agent, no dependency on this backend
```

**There is exactly one execution path.** The platform has no engine of its own:
testing a workflow renders its package and drives it, so a test run and a
deployment execute identical code. An earlier design ran a second traversal
in-process, and the two had already diverged — `LOOP` ran real iterations in
the platform and a single pass in a generated package. `tests/test_single_engine.py`
fails if that second path grows back.

**Stack**

| Part | Technology |
|------|-----------|
| Backend | Python 3.11, FastAPI, SQLAlchemy (async), PostgreSQL |
| Workflow runtime | Google ADK 2.x graph workflows (`google-adk`) |
| Agent protocol | A2A over JSON-RPC (`a2a-sdk`, pinned `<1.0` — see §9) |
| AI agents | ADK `LlmAgent`, `google-genai` (Vertex AI) |
| Tool protocols | MCP (Streamable HTTP), A2A JSON-RPC |
| Frontend | React 18, TypeScript, ReactFlow, Zustand, Tailwind CSS |
| Packaging | Jinja2 templates, Docker, docker-compose |

## 2. Repository Layout

```
app/
├── main.py                     ← FastAPI app bootstrap, CORS, router registration, DB init
├── config.py                   ← All settings read from environment variables
├── database.py                 ← AsyncEngine + AsyncSessionLocal (session factory)
│
├── models/
│   ├── workflow.py             ← DB tables: Workflow, WorkflowVersion, WorkflowExecution, NodeExecutionLog
│   └── resources.py            ← DB tables: AgentResource, ModelResource, ToolResource, DataSourceResource
│
├── schemas/
│   ├── canvas.py               ← Pydantic canvas shapes + CURRENT_SCHEMA_VERSION
│   └── workflow.py             ← Pydantic: API request/response shapes
│
├── api/
│   ├── workflows.py            ← REST: /workflows, /versions, /test, /package, /node_logs
│   └── resources.py            ← REST: /agents, /models, /tools, /datasources
│
├── nodes/                      ← Node *declarations* only — no behaviour (§4)
│   ├── base.py                 ← NodeDefinition + PaletteMetadata
│   ├── registry.py             ← NODE_REGISTRY, get_palette(), RETIRED_TRIGGER_TYPES
│   ├── triggers/
│   │   └── a2a_start.py        ← A2A_START — the only entry node
│   └── tasks/
│       ├── orchestrator_agent.py   ← agent with MCP / A2A / function tools
│       ├── agent.py, model.py      ← LLM nodes
│       ├── tool.py, datasource.py  ← MCP nodes
│       ├── remote_agent.py         ← A2A remote agent
│       ├── function.py, transform.py
│       ├── condition.py, loop.py, parallel_fork.py, merge.py, end.py
│       └── human_approval.py, subworkflow.py   ← declared, not yet generated
│
├── compiler/
│   ├── schema_validator.py     ← Layer 1: per-node config checks
│   ├── semantic_validator.py   ← Layer 2: graph topology rules (NetworkX)
│   ├── ir.py                   ← Layer 3: canvas → IR
│   ├── graph_plan.py           ← Layer 4: IR → ADK graph plan, naming, env keys (§6)
│   ├── graph_check.py          ← Builds the plan under ADK to prove it is legal
│   └── canvas_migrations.py    ← Forward migrations for stored canvas JSON
│
├── runtime/
│   └── package_runner.py       ← Renders the package and drives it (§7)
│
└── packaging/
    ├── render.py               ← graph plan → project files (§9)
    ├── builder.py              ← Where it lands, port, lint, git init
    ├── template/               ← The reference project, copied verbatim
    │   ├── core/               ← config, agent_card, a2a_app, state, logging
    │   ├── nodes/base.py       ← The node authoring contract
    │   ├── tools/              ← MCP / A2A / inline functions as ADK tools
    │   ├── run_once.py         ← Run the workflow once and report per node
    │   └── tests/              ← The template's own suite (runs in CI)
    └── render_templates/       ← Jinja2: agent.py, nodes/<type>.py, .env, README

scripts/
└── migrate_canvases.py         ← Backfill stored canvases to the current schema

tests/
├── test_adk_contract.py        ← Pins the google-adk / a2a-sdk surface we rely on
├── test_single_engine.py       ← Fails if a second execution path grows back
├── test_secrets.py             ← Credentials must not reach graph.json or git
├── test_graph_plan*.py         ← Layer 4, incl. golden files for the seed workflows
├── test_render.py              ← Rendering, incl. seed packages end to end
├── test_package_runner.py      ← Driving a package (the Test button's path)
└── golden/graph_plans/         ← Compiled plans for the four seed workflows
```

**Two directories are easy to confuse.** `packaging/template/` is a *runnable
project* whose files are copied verbatim into every package — it has its own
test suite, so a bug there is caught in CI rather than in a customer's
container. `packaging/render_templates/` holds the Jinja2 templates for the
parts that differ per workflow.

### Running the tests

```bash
python -m pytest                                  # platform (361 tests)
cd app/packaging/template && python -m pytest      # the template itself (57 tests)
```

## 3. Database Models

All models live in `app/models/workflow.py`.

### `workflows`
One row per named workflow. Holds `name`, `description`, `status` (`draft` / `active` / `archived`).

### `workflow_versions`
Every Save & Compile creates a new version row. Key columns:

| Column | What it stores |
|--------|---------------|
| `canvas_json` | Raw canvas as the frontend sent it (nodes with x/y positions, raw config) |
| `ir_json` | The compiled Intermediate Representation — **the engine reads THIS, not canvas_json** |
| `is_valid` | `True` only if both compiler passes completed without errors |
| `validation_errors` | List of error strings if compilation failed |

> **Important:** `canvas_json` and `ir_json` are written together at save time. If you change a node's config on the canvas but don't click Save & Compile, the engine still uses the old `ir_json`. Always re-save after edits.

### `workflow_executions`
One row per run. `trigger_payload` holds `{payload, mode}`; `output` holds the
result plus the A2A task lifecycle (§7); `status` moves `pending → running →
success/failed`.

### `node_execution_logs`
One row per node event, written from the per-node trace the package reports.
Keyed by **canvas** node id, so the frontend can paint status badges onto the
canvas. A looping node produces one row per iteration; a node on an untaken
branch produces none.

Both cascade from their parent: deleting a workflow removes its versions,
executions and node logs. (Without that cascade SQLAlchemy nulls the FK, which
the NOT NULL constraint refuses — deleting any workflow with run history used to
fail.)

```
workflows (1)
  └── workflow_versions (many)
        └── workflow_executions (many)
              └── node_execution_logs (many — one per node)
```

---

## 4. The Node System

### `NodeDefinition` — `app/nodes/base.py`

A `NodeDefinition` is a **declaration, not an implementation**. It carries the
palette entry, the config contract the UI edits and the compiler validates, and
the handles the canvas can wire. It has no `execute` method.

```python
@dataclass
class NodeDefinition:
    node_type: str                  # "TRANSFORM"
    version: str
    palette: PaletteMetadata        # label, category, colour, icon, description, wave
    config_schema: dict             # JSON Schema — validated by compiler layer 1
    input_schema: dict
    output_schema: dict
    output_handles: list[str]       # e.g. ["true", "false", "default"]
    allows_inbound: bool = True
    allows_outbound: bool = True
    is_trigger: bool = False        # only A2A_START
    is_terminal: bool = False       # only END
    allows_cycle: bool = False      # only LOOP
    accepts_tools: bool = False     # has a "tools" handle
    is_tool_group: bool = False     # SEQUENTIAL_AGENT / PARALLEL_AGENT
    supports_on_error_continue: bool = False
    secret_config_keys: frozenset[str] = frozenset()
```

Node **behaviour** lives once, in `packaging/render_templates/nodes/*.j2` and
`packaging/template/nodes/`. Adding an `execute` method here would create a
second implementation that never runs — `tests/test_single_engine.py` fails if
one appears.

Two flags are worth knowing about:

- **`supports_on_error_continue`** — whether `on_error: continue` means anything
  for this type. True for nodes that fail for reasons outside the workflow
  (`LLM_AGENT`, `TOOL`, `REMOTE_AGENT`, `FUNCTION`, `TRANSFORM`, the agents): their
  generated module returns an error payload so a downstream router can branch on
  `_error`. False for structural nodes, where continuing is meaningless rather
  than lenient — a `CONDITION` that "continues" has no route to take. The
  validator warns rather than silently ignoring the setting.
- **`secret_config_keys`** — config keys holding credentials. The compiler moves
  each to an environment variable and strips it from the plan, so it never
  reaches the generated code or `graph.json`. See §9.

### Registry — `app/nodes/registry.py`

```python
NODE_REGISTRY: dict[str, NodeDefinition]   # node_type → definition
get_node_definition(node_type)             # None if unknown
get_palette()                              # list of dicts for GET /workflows/palette
RETIRED_TRIGGER_TYPES                      # HTTP/SCHEDULE/WEBHOOK/QUEUE_TRIGGER
```

`RETIRED_TRIGGER_TYPES` exists so a canvas saved before the A2A move gets
*"HTTP_TRIGGER no longer exists — re-save this workflow to migrate it"* rather
than *"unknown node type"*.

### `A2A_START` — the only entry node

A packaged workflow is invoked by a message, not by a path, a cron expression or
a queue subscription, so there is one entry type. Its config is just:

- `payload_schema` — a list of named, typed, described fields. This is the
  contract the agent advertises, and the Run panel builds its form from it.
- `input_mode` — `json` (a JSON object) or `text` (prose, arriving as
  `{"text": ...}`).
- `state_key` — the top-level key holding workflow state.

`payload_json_schema()` in the same module converts the field list into the JSON
Schema the generated package embeds as `PAYLOAD_SCHEMA`. The canvas stores the
authoring shape; the compiler emits the schema.

### Tool-provider nodes

`TOOL`, `DATASOURCE`, `REMOTE_AGENT` and `FUNCTION` can be wired two ways:

- **into the flow** — they become graph nodes with their own module, or
- **into an `ORCHESTRATOR_AGENT`'s `tools` handle** (`target_handle: "tools"`) —
  they become ADK tools the agent may call, and are *not* graph nodes.

The IR compiler resolves the second case into `resolved_tools` on the
consumer's config, and the graph plan excludes those nodes from the graph.

### Tool groups — sequential and parallel

`SEQUENTIAL_AGENT` and `PARALLEL_AGENT` are tool *groups*. Wire tools into a
group's `tools` handle, then wire the group into an agent's:

```
[TOOL Fetch] ──tools──┐
                      ├──> [SEQUENTIAL_AGENT] ──tools──> [ORCHESTRATOR_AGENT]
[TOOL Summarise] ─────┘
```

The agent then sees **one** tool. Calling it runs the children in the configured
order (sequential, each result merged into the next call's arguments) or all at
once on the same input (parallel, results keyed by tool name). Groups nest: a
group can be a child of another group.

This replaces the old `tool_execution_mode` dropdown on `ORCHESTRATOR_AGENT`.
That flag could not say *which* tools to group or in what order, so it is
removed rather than migrated — canvas migration v2 → v3 drops it and, if it was
set to `parallel`, notes in the node's description how to restore the behaviour.

**Why a composite tool and not ADK's `SequentialAgent` / `ParallelAgent`.** Two
independent reasons, both checked against the installed ADK:

1. Those classes are on the way out. Their docstrings read *"deprecated in
   favor of Workflow"*, and the ADK docs say template workflows have been
   *"superseded by graph-based workflows"*.
2. `Workflow` — the replacement — is a `BaseNode` but **not** a `BaseAgent`, so
   it cannot go in an `LlmAgent`'s `sub_agents` and cannot be wrapped by
   `AgentTool`. ADK offers no way to attach a graph to an agent's tools. (Their
   `sub_agents` also take agents, and a group's children are tools.)

So a group is a `FunctionTool` that drives its children — deterministic, no
extra model turns. The implementation is `packaging/template/tools/groups.py`.

**What the model is asked for.** The group declares the union of its children's
parameters. For a parallel group every child's required parameters are required,
because they all run on the caller's input. For a sequential group only the
*first* child's are, because a later child's input is usually produced by an
earlier one. That is what makes a pipeline callable in a single turn.

### LLM Agent — in the flow, or as a sub-agent

`LLM_AGENT` (renamed from `MODEL`) is the composable agent unit. Where you wire
it decides what it is, and there is no setting to contradict the drawing:

* **In the flow** — a graph node. With nothing on its `tools` handle it is one
  direct `generate_content` call: cheap, one round trip, fully deterministic.
  Wire in a tool, remote agent, function or group and it becomes an ADK
  `LlmAgent` with those tools, because a model that can call tools needs a loop
  to call them in.
* **Into another agent's `tools` handle** — a sub-agent. This is ADK's own
  recommended mechanism: the agent is emitted with `mode='single_turn'` and
  attached via `sub_agents=[...]`, and ADK exposes it to the parent's model as a
  tool by itself, running it inline in the parent's session. ADK's `AgentTool`
  docstring says direct use of `AgentTool` is *discouraged* in favour of exactly
  this, so the package does not use it.
* **Into a Sequential or Parallel Tools group** — a group member. A group is a
  deterministic composite `FunctionTool`, not an agent, so there is no
  `sub_agents` list to attach to; the group drives the agent through its own
  `Runner`. That path leaves `mode` unset, because ADK refuses to run a
  `single_turn` agent as a Runner's root.

A sub-agent is a tool consumer in its own right, so it can have its own MCP
tools, remote agents and groups — those resolve recursively, and each nested
credential still gets its own environment variable.

### Input and output structure

Agent nodes can declare the shape of what they take and what they return. Both
are authored as a list of named fields and become ADK's two schema fields, which
are **not** symmetric:

| Canvas | ADK | Where it applies |
| --- | --- | --- |
| Input structure | `input_schema` (`type[BaseModel]`) | Only when the agent is *called as a tool*. The fields become the parameters the calling model can fill in. |
| Output structure | `output_schema` (plain dict) | Wherever the agent runs. ADK requests `response_mime_type=application/json` with the schema, so the reply is constrained to that shape. |
| Store reply as | `output_key` | Puts the *parsed* object into workflow state. |

Because `input_schema` only has an effect for an agent being called, only
`LLM_AGENT` offers it — `AGENT` and `ORCHESTRATOR_AGENT` cannot be wired into a
`tools` handle, so the setting could never do anything for them, and offering it
would be offering a dead control. Both of those still take an output structure.

A sub-agent that declares no input structure is still given one required
`request: str` parameter, because a tool declaring no parameters can never be
passed anything at all.

`output_schema` and tools can be combined. ADK 2.8 supports it explicitly —
"tools during the thought loop and enforcing structure only on the final
output" — natively where the model allows it (Vertex AI Gemini) and otherwise
through an injected `SetModelResponseTool`. So the platform imposes no rule
against the combination.

### Loops

ADK graph workflows have no loop construct. A loop is a **cycle on the canvas**,
and ADK's only structural rule is that the cycle must contain at least one
*routed* edge — `_detect_unconditional_cycles`: *"Cycles must include at least
one conditional (routed) edge to avoid infinite loops"*. The `LOOP` node supplies
that edge (`loop_body` to go round again, `done` to leave) and owns the
iteration counter, because on re-entry `node_input` is whatever the body
produced rather than what the loop last saw.

ADK imposes no step limit of its own, so `max_iterations` is the only thing
between a mis-written condition and a graph that spins forever. It is a hard
stop in both modes, and reaching it sets `truncated` on the result rather than
returning a partial answer that looks complete.

**for_each** walks the list at `items_path`. The list is resolved **once, on
entry, and pinned** for the whole loop. Re-resolving it each pass would read it
back out of the *body's* output, so a body that filters or consumes the list
would change what the loop is walking half way through. Each pass sets
`current_item` and `current_index`; `done` carries `results`, `iterations` and
`total_items`.

**while** checks `exit_condition` before each pass and leaves when it is
**true** — so `i >= 5` runs the body five times, and a condition that is already
true runs it zero times. `data` is the latest payload and `i` the iteration
count.

Iteration state is namespaced per node (`_loop_<node>_i`, `_results`, `_items`).
A shared key would be overwritten by an inner loop, leaving an outer loop
resuming against the inner one's list — measured as one iteration over nothing.

Both modes are validated on the canvas: `for_each` without an items path and
`while` without an exit condition are compile errors, as is a `LOOP` with either
output handle unwired. Each of those used to compile, package, run and iterate
zero times without a word.

### Human approval

A HUMAN_APPROVAL node parks the run on a person. It needs no machinery of its
own, because ADK graphs interrupt natively and A2A already has a state for it:

* the node returns a **`RequestInput`**, which `BaseNode.run` converts to an
  interrupt event (*"RequestInput -> convert to interrupt Event"*);
* `to_a2a` reports the task as **`input-required`**, carrying the question and
  a JSON Schema for the answer, so a client can build a form from the task;
* the caller resumes with `message/send` on the **same `taskId`** and a data
  part tagged `adk_type: function_response`;
* the node runs again and reads the answer from **`ctx.resume_inputs`**.

Two details decide whether any of that works, and the template handles both so
a canvas author cannot get them wrong:

| Detail | If you get it wrong |
| --- | --- |
| `rerun_on_resume=True` | the node is marked complete on resume and never sees the answer |
| interrupt id derived from `ctx.node_path` | the re-run cannot look the answer up, so the task sits at `input-required` however often it is answered |

**The decision is a route.** `approved` and `rejected` are named handles, so
only the matching branch runs. This is the one thing the node got wrong for a
while: it was missing from the compiler's hardcoded branch-node list, so both
edges were plain flow, both branches ran, and the losing branch's output
overwrote the winner's. That list is now derived from `uses_named_routes` on
the node declarations, the same way the tool-wiring sets are.

**Required extra fields are enforced on approval, not by the schema.** ADK
validates the answer against `response_schema` before the node sees it, so
marking a collected field required there would make *rejecting* impossible
without inventing a value for a field that does not apply. Only `approved` is
required in the advertised schema; the node checks the rest when approving and
asks again — reusing the same interrupt id, which ADK supports — if one is
missing.

**There is no timeout, and cannot be one in the package.** The node returns and
the graph parks; nothing is waiting in-process for a clock to fire. Expiring an
approval needs a scheduler outside the package, so the old `timeout_seconds` /
`on_timeout` settings were removed rather than left as controls that do
nothing.

**A parked run outlives the process that made it.** `run_once.py` keeps the
A2A task *and* the ADK session in one SQLite file under `.runs/` beside the
package — persisting only the task would resume into an empty workflow. So a
later invocation, or the platform's own Runs panel, answers the parked task and
the workflow **carries on from the approval node**; nothing before it runs
twice. Verified by the node log: a resumed run records only `gate → paid → end`.

Answer one from the generated package, which does the whole handshake:

```bash
python run_once.py '{"amount": 5000, "reason": "laptops"}'
#   WAITING  state=input-required — prints the question and what to answer

python run_once.py '{"amount": 5000, "reason": "laptops"}' \
    --answer '{"approved": true, "comment": "ok", "cost_centre": "ENG-1"}'
#   completed
```

In the platform, a parked run is recorded as `waiting` rather than `failed`, and
the Runs panel shows the question with **Approve** and **Reject** buttons, its
fields built from the schema the paused task advertised. Answering resumes that
task rather than replaying the workflow.

```bash
# answer a task an earlier invocation parked
python run_once.py --resume <task-id> --context <context-id> \
    --interrupt <interrupt-id> --answer '{"approved": true}'
```

### Asking a person for values

HUMAN_APPROVAL answers one question — yes or no. HUMAN_INPUT answers the other
one: values the workflow needs and cannot work out for itself. Same ADK
interrupt, same A2A `input-required`, same shared template; what differs is the
shape on the canvas.

| | HUMAN_APPROVAL | HUMAN_INPUT |
| --- | --- | --- |
| Handles | `approved`, `rejected` — routes you wire separately | one `output` |
| Answer | a decision, plus any extra values | values, merged into the payload |
| Run panel | **Approve** / **Reject** | one **Submit** |

Two node types rather than a mode, because handles are declared per type: one
node cannot be both without the canvas lying about where its edges go.

**Required fields are enforced by the node, never by the advertised schema.**
ADK validates `response_schema` before the node runs again, and a validation
failure fails the *whole run* — so one blank field would destroy a workflow
that had been waiting on a person, at the worst possible moment. Marking
`address` required in the schema produced exactly that: a run that died with
"address Field required". Both node types therefore advertise everything as
optional, note the requirement in each field's description, and carry the names
on the interrupt payload as `required_fields`. The node checks them and **asks
again**, naming what is missing; the Run panel marks them with an asterisk and
checks before sending, which only saves a round trip.

A workflow can pause more than once. `seed_human_approval.py` does: an approval
gate, then an input request on the approved path — approve, reject and supply
values, all in one run.

### Waiting

ADK has no delay node. Searching the package for sleep / delay / schedule /
timer turns up only retry backoff, polling loops and fixed internal delays;
`workflow/_trigger.py` sounds relevant but `Trigger` is the data model for a
downstream node's input, with no timing in it. So `WAIT` is `asyncio.sleep` in
a generated node.

**Why not park the task** the way HUMAN_APPROVAL does, which would survive a
restart? Because nothing in the package would ever resume it. A wait that needs
an external scheduler to fire is a scheduling integration, not a node — every
`WAIT` would become a workflow that stops forever until something pokes it.

**Why `asyncio.sleep` is acceptable:** it is cooperative, so a branch that is
waiting does not hold up the branch beside it. Measured from the trace of
`seed_wait.py` — a 1s and a 3s wait on parallel branches complete 2.01s apart
and the graph finishes 3.06s in, not 4s.

**The detail that makes it work.** `@node(timeout=N)` *kills* a node that
outlives its timeout:

    NodeTimeoutError: Node 'slow' timed out after 1.0 seconds.

Nodes normally inherit `policies.timeout_seconds` — 120s in the seeds — so a
five-minute wait would die at two minutes blaming a timeout rather than the
wait. The renderer derives this node's timeout from its wait instead. Verified
both ways: with the canvas policy an 8s wait died at 5s; with the derived
timeout it completes.

**What it costs, said out loud rather than hidden:**

| | |
| --- | --- |
| The wait is in-process | a restart loses the run |
| Blocking callers | hold a connection open for the whole wait — past 60s the validator says to use task mode |
| Over an hour | rejected: that is a scheduling problem, not a pause in a run |

The payload carries the **configured** wait, not the measured one. A measured
duration differs by a millisecond between runs, and the generated suite asserts
a blocking call and a polled call return the same result — so a timing value in
the payload failed `test_both_modes_agree_on_the_result` for every workflow with
a WAIT. The real elapsed time goes to the log, where a difference is
information rather than noise.

### Variables

Each node can name its result. Later nodes read it as `vars['<name>']` in a
Transform expression or a Condition branch — including across edges that no
longer carry it, which is the point.

**ADK has no separate variable concept: variables *are* session-state keys**,
bound by name. Verified against 2.8.0 — a node returning
`Event(state={"customer": "ACME"})` makes a later node's `customer` parameter
arrive as `"ACME"`, and the session holds a flat `{'customer': 'ACME'}`.
`node_input` is special-cased as the edge payload rather than a state key. So a
canvas variable is a **flat, top-level state key**, the same place
`LlmAgent.output_key` already writes, and readable by ADK's own binding.

**The saving is wired once, in the decorator.** The node modules have 27 return
sites between them — a router has three, a remote agent four — and a variable
saved on some paths but not others is worse than no variable at all. So
`flow_node` wraps ADK's `@node`, and records the result after the node returns
whichever way it got there.

**The namespace is shared, so names are checked.** The workflow's own payload
lives under `wf`, a loop keeps counters under `_loop_<node>_*`, and ADK reserves
anything containing `:` (`app:`, `user:`, `temp:`). A name that collides, or one
that is not identifier-shaped — ADK binds parameters by name, and a parameter
cannot be called `my var` — is a compile error rather than a variable that
quietly never appears. Two nodes writing the same name is a warning: whichever
runs last wins.

Only nodes that produce a result of their own offer it. A Sequential or Parallel
Tools group is resolved into its consumer's tool list, and a MERGE is a
`JoinNode` with no module, so neither has a result to name.

### Tool declarations need real signatures

ADK builds a tool's declaration from its Python signature, so a wrapper written
as `async def impl(**kwargs)` declares **no parameters** — the model sees the
name and description but cannot pass it anything, and nothing raises. MCP tools
and inline canvas functions are therefore given a synthesized
`inspect.Signature` built from the schema they actually accept (the MCP server's
`inputSchema`, or the FUNCTION node's declared `parameters`). See
`packaging/template/tools/signature.py`.


## 5. The Compiler Pipeline

Layers 1–3 run on `POST /api/v1/workflows/{id}/versions` (the Save & Compile
button). Layer 4 runs on `/test` and `/package`.

```
CanvasPayload (Pydantic)
        │
        ▼
schema_validator.validate_schemas()       ← Layer 1
        │   every node's config against its config_schema
        ▼
semantic_validator.validate_semantics()   ← Layer 2
        │   NetworkX DiGraph. Returns (errors, warnings).
        ▼
ir.compile_to_ir()                        ← Layer 3
        │   IRNode objects; resolves tool-provision edges into
        │   resolved_tools on ORCHESTRATOR_AGENT
        ▼
IR.to_dict() → stored as ir_json in DB
        │
        ▼
graph_plan.build_graph_plan()             ← Layer 4 (on test / package)
        │   ADK node + edge plan, node naming, env keys,
        │   secrets moved out of config
        ▼
graph_check.verify_plan_builds()          ← the authoritative check
            builds the plan as a real ADK Workflow
```

### Layer 2 exists mostly to pre-empt ADK

Several rules catch, at save time, something ADK would otherwise reject when the
graph is built or — worse — halfway through a run. Each is proven against real
ADK behaviour in `tests/test_adk_contract.py`:

| Rule | What it prevents |
|---|---|
| Exactly one `A2A_START` | An ADK graph takes one entry |
| Exactly one `END` | *"multiple terminal nodes produced output"*, raised during **finalisation** — after the work is done |
| No dangling flow node | Same failure: a node with inbound but no outbound edge ends a path of its own |
| Every `PARALLEL_FORK` reconverges | Same again: branches that each end somewhere different |
| Every node reachable from the entry | A node that would never run |
| Cycles only through `LOOP` | An unbounded graph |
| Duplicate `(source, target)` pairs → **warning** | *"Duplicate edge found: from=X, to=Y"* — the compiler merges them, so this is not an error |
| `on_error: continue` on a structural node → **warning** | A setting that would be silently ignored |

### Layer 4's check is authoritative, not a second opinion

`graph_check.verify_plan_builds()` materialises the plan as a real
`google.adk.Workflow` with stub node bodies and lets **ADK's own validator**
judge it. If ADK changes a rule, packaging fails on the next attempt instead of
shipping a broken container. Construction is pure Python, so it costs nothing.

It earns its place: it caught that `Workflow(name=...)` is itself validated as a
node name, so a workflow called "Phase 3 Router" was rejected outright. The plan
now carries `workflow_slug` (an identifier) alongside `workflow_name` (for the
agent card).

### Canvas migrations

`compiler/canvas_migrations.py` rewrites canvases saved against an older schema,
keyed on `canvas_json.schema_version` and idempotent.

- **v1 → v2** — the four trigger types become a single `A2A_START`, keeping the
  node's id, position and edges. An `HTTP_TRIGGER`'s `body_schema` carries over
  to `payload_schema`, so the existing Run form keeps working. Extra triggers
  and their edges are dropped, and retired settings (a cron expression, a queue
  name) are appended to the node's description rather than silently discarded.

Reading a version migrates it on the fly, so old workflows open correctly
without waiting for a backfill. That read deliberately builds a fresh response
object rather than assigning to the ORM instance — `get_db` commits at the end
of every request, so mutating it would turn a GET into a silent write. A
migrated version comes back with `is_valid: false` and the reason in
`validation_errors`, which keeps Run and Package disabled until the user saves.

To persist the migration and recompile:

```bash
cd backend
python -m scripts.migrate_canvases --dry-run   # report what would change
python -m scripts.migrate_canvases             # apply
```

## 6. IR and the Graph Plan

Two representations, deliberately separate: the **IR** describes the canvas, and
the **graph plan** describes the ADK `Workflow` generated from it. That way the
canvas model never learns ADK's constraints, and the packaging layer never
learns the canvas's.

### The IR — `workflow_versions.ir_json`

```json
{
  "workflow_version_id": "abc-123",
  "entrypoints": ["node_start_1"],
  "nodes": {
    "node_start_1": {
      "kind": "trigger.a2a_start",
      "node_type": "A2A_START",
      "config": {
        "input_mode": "json",
        "state_key": "wf",
        "payload_schema": {
          "fields": [{"name": "text", "type": "string", "description": "", "required": true}]
        }
      },
      "policies": { "timeout_seconds": 60, "retry_max_attempts": 1, "on_error": "fail" },
      "metadata": { "title": "A2A Start", "description": "" },
      "io": { "input_schema": {}, "output_schema": {} },
      "next": ["node_orch_2"],
      "branches": {}
    },
    "node_orch_2": {
      "kind": "task.orchestrator_agent",
      "node_type": "ORCHESTRATOR_AGENT",
      "config": {
        "model": "gemini-2.5-flash",
        "system_prompt": "You are a helpful agent.",
        "resolved_tools": {
          "mcp_servers": [],
          "a2a_agents": [
            {
              "name": "diet_advisor",
              "endpoint": "http://host.docker.internal:8005",
              "description": "Gives diet advice",
              "node_id": "node_remote_3",
              "node_type": "REMOTE_AGENT"
            }
          ],
          "functions": []
        }
      },
      "next": ["node_end_4"],
      "branches": {}
    },
    "node_end_4": { "kind": "task.end", "node_type": "END", "config": {}, "next": [], "branches": {} }
  }
}
```

- `entrypoints` — node ids to start from. Exactly one, the `A2A_START`.
- `next` — nodes to run after this one. Empty for branch nodes, which use
  `branches` instead.
- `branches` — `handle_name → [node_id, …]`. `CONDITION` uses its branch names,
  `LOOP` uses `loop_body` / `done`, `PARALLEL_FORK` uses its branch labels.
- `resolved_tools` — only on agent nodes. The dereferenced tool configs,
  including the canvas `node_id`.
- Tool-provider nodes **do** appear in `nodes` (every canvas node does). It is
  the *graph plan* that excludes the ones reachable only through a `tools`
  handle.

### The graph plan — `graph.json`

Serialised into every package and served by that package's `GET /graph`.

```json
{
  "schema_version": 1,
  "workflow": {
    "name": "Condition Router",
    "slug": "condition_router",
    "description": "Routes by score threshold",
    "version_id": "abc-123",
    "canvas_version": 2
  },
  "entry_node": "a2a_start",
  "terminal_node": "n_end_2",
  "nodes": [
    { "name": "a2a_start",     "canvas_id": "trigger",        "type": "A2A_START",
      "module": "nodes.a2a_start", "title": "A2A Start", "timeout": 30, "retries": 1,
      "on_error": "fail", "is_entry": true },
    { "name": "n_condition_1", "canvas_id": "condition",      "type": "CONDITION",
      "module": "nodes.n_condition_1", "routes": ["high_score", "low_score"], "…": "…" },
    { "name": "n_end_2",       "canvas_id": "end",            "type": "END",
      "module": "nodes.n_end_2", "is_terminal": true, "…": "…" }
  ],
  "edges": [
    { "from": "START",         "to": "a2a_start",     "route": null },
    { "from": "a2a_start",     "to": "n_condition_1", "route": null },
    { "from": "n_condition_1", "to": "n_transform_3", "route": "high_score" },
    { "from": "n_condition_1", "to": "n_transform_4", "route": "low_score" },
    { "from": "n_transform_3", "to": "n_end_2",       "route": null },
    { "from": "n_transform_4", "to": "n_end_2",       "route": null }
  ],
  "join_nodes": [],
  "env_keys": [
    { "key": "AGENT_NAME", "required": true, "needed_by": ["agent_card"], "default": "condition_router" },
    { "key": "A2A_AUTH_TOKEN", "required": false, "needed_by": ["inbound_auth"], "secret": true }
  ]
}
```

### How canvas topology maps onto ADK

| Canvas | ADK | Notes |
|---|---|---|
| Linear chain | plain edges | `route: null` |
| `CONDITION` | router node + dict edge | the node returns `Event(route="…")`; the edge maps each value to a target |
| Two branches → one target | **one** edge, `route: ["a", "b"]` | ADK rejects two edges sharing a `(from, to)` pair, so the compiler merges them |
| `PARALLEL_FORK` | several plain edges | handles are fan-out labels, **not** route values — ADK dispatches to every successor |
| `MERGE` | `JoinNode` | waits for every predecessor, then hands the successor a dict keyed by predecessor name |
| `LOOP` | back-edge + guard router | per-node iteration state in `ctx.state`; the list is pinned on entry; `max_iterations` is a hard stop that sets `truncated` |
| `END` | terminal node | **must** emit content — see §9 |

### Node naming

Names become Python module names, ADK node names, and the `@N` segments in
`event.node_info.path`, so they must be unique, valid identifiers and stable.

Ordinals are assigned in **canvas-id order, not graph order**. Canvas ids look
like `node_<epoch_ms>_<counter>`, so sorting by id is chronological by creation:
a node added later sorts last and takes the next free ordinal, leaving every
existing name untouched. Ordering by position in the flow would renumber
everything downstream of an insertion and rewrite most of the package on a
one-node change.

The entry node is always `a2a_start`; others are `n_<type>_<ordinal>`.
`graph.json` keeps the `name → canvas_id` mapping so a run can be painted back
onto the canvas.

## 7. Running a Workflow

`app/runtime/package_runner.py` — and it contains no execution logic at
all. It renders the workflow's package, then shells out to that package's own
`run_once.py`. What you test is the artefact that ships, byte for byte.

```
POST /workflows/{id}/versions/{vid}/test   {payload, mode, rebuild}
        │
        ├─ compile the graph plan, check it under ADK          (§5)
        ├─ ensure_package(plan)  → render, or reuse if graph.json still matches
        ├─ create a WorkflowExecution row (status=pending), return it
        │
        └─ background: subprocess → run_once.py --json
                 │  drives the package over its own A2A surface
                 │  message mode: blocking message/send
                 │  task mode:    message/send + tasks/get polling
                 ▼
           envelope { ok, state, task_id, result, trace[], duration_ms }
                 │
                 ├─ WorkflowExecution.output   ← result + A2A lifecycle
                 └─ NodeExecutionLog rows      ← one per node event, keyed by canvas id
```

A **subprocess**, not an in-process import: a package's modules are named
`agent`, `core.*`, `nodes.*`, which would collide with this backend's own
imports and be cached across runs. Running with the package as the working
directory is exactly how the container imports it.

Packages are keyed on version id and reused across runs (~1.4 s versus ~4 s for
a fresh render), but only while the rendered `graph.json` still matches the
plan — so a version id reused after a canvas edit cannot test stale code.

### Per-node detail

`run_once.py` hands `to_a2a` a `Runner` whose `run_async` tees every ADK event.
That is the supported way in — `to_a2a` accepts a `Runner`, and `Runner` is a
plain class, so replacing the bound method on the instance needs no ADK
internals. The workflow is still driven over its real A2A surface; the trace is
a side channel, not a different execution.

Each step carries the node name, its `canvas_id`, and the loop iteration parsed
from `event.node_info.path` (`wf@1/n_body_3@2`), which is what makes loop
progress visible on the canvas.

### `run_once.py` is also a CLI

Useful on its own for debugging a package:

```
$ python run_once.py '{"text": "hello"}' --mode task
OK  state=completed  1110ms
task    65eded8e-63fb-43cb-a4f6-a258d5629113
polls   1

nodes:
  - a2a_start       [A2A_START]  (A2A Start)
  - n_normalise_1   [TRANSFORM]  (Normalise)
  - n_route_2       [CONDITION]  (Route by length)
  - n_summarise_3   [FUNCTION]   (Summarise)
  - n_end_5         [END]        (End)

result:
{"summary": "hello", "word_count": 1, "branch": "short"}
```

`--json` prints one machine-readable envelope, which is what the platform parses.

### Node policies

`NodePolicies` maps onto ADK rather than being interpreted by a platform engine:

| Canvas policy | Generated code |
|---|---|
| `timeout_seconds` | `@node(timeout=…)` |
| `retry.max_attempts` > 1 | `@node(retry_config=RetryConfig(max_attempts=…))` |
| `on_error: fail` | the exception propagates; the A2A task fails |
| `on_error: continue` | the module returns `{_error: true, _error_node: …}` so a router can branch on it — only for node types that declare `supports_on_error_continue` |

## 8. A2A and MCP Tool Protocols

### MCP — Model Context Protocol (TOOL / DATASOURCE nodes)

FastMCP Streamable HTTP transport. Three-step handshake per call:

```
1. POST /mcp  {"jsonrpc":"2.0","method":"initialize",...}
              ← header: Mcp-Session-Id: <sid>

2. POST /mcp  {"jsonrpc":"2.0","method":"tools/list","params":{}}
              header: Mcp-Session-Id: <sid>
              ← SSE stream or JSON: list of tool schemas

3. POST /mcp  {"jsonrpc":"2.0","method":"tools/call",
               "params":{"name":"tool_name","arguments":{...}}}
              header: Mcp-Session-Id: <sid>
              ← SSE stream or JSON: result
```

Helper functions in `orchestrator_agent.py`: `_mcp_url()`, `_mcp_init_session()`, `_mcp_headers()`, `_mcp_parse_tools_list()`, `_mcp_parse_call_result()`.

### A2A — Agent-to-Agent (REMOTE_AGENT nodes)

JSON-RPC 2.0 protocol. Single POST to the agent root:

```json
POST /
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "the task to delegate"}],
      "messageId": "<uuid>"
    }
  }
}
```

Response text is extracted from `result.artifacts[].parts[].text` or `result.history[-1].parts[].text`. The `_a2a_call()` helper in `orchestrator_agent.py` handles both shapes.

### How ORCHESTRATOR_AGENT uses tools (Google ADK)

```
Turn 1:
  → Anthropic API  {tools: [...], tool_choice: {type: "any"}, messages: [user]}
  ← stop_reason: "tool_use", content: [ToolUseBlock(name, id, input)]

Dispatch (_dispatch_tool_call):
  mcp_tool_map hit  → POST to MCP server
  a2a_map hit       → _a2a_call() to remote agent
  fn_map hit        → exec() inline Python
  ← each path calls _log_tool_node() with a fresh DB session

Turn 2+:
  → Anthropic API  {messages: [...previous..., tool_results]}
  ← stop_reason: "end_turn", content: [TextBlock]  → return as output dict
```

`tool_choice: {"type": "any"}` is applied only on **iteration 0** to force the model to use a tool on the first turn rather than answering from its own knowledge. Subsequent turns are unrestricted.

---

## 9. The Packaging / Deploy Layer

`POST /api/v1/workflows/{id}/versions/{version_id}/package`

### `packaging/render.py` — `render_package(plan, destination)`

Every file is either **copied verbatim** from `packaging/template/` or
**rendered** from a Jinja2 template in `packaging/render_templates/`. Nothing is
built by concatenating source strings.

```
<slug>-<version_id[:8]>/
├── main.py              ← rendered: builds the served app
├── agent.py             ← rendered: the ADK Workflow. The edge list is the topology
├── nodes/
│   ├── registry.py      ← rendered: NODES, CANVAS_IDS, NODE_TYPES, NODE_TITLES
│   ├── base.py          ← copied:   the node authoring contract
│   ├── a2a_start.py     ← rendered: one module per canvas node
│   └── n_<type>_<n>.py  ← rendered
├── core/                ← copied: config, agent_card, a2a_app, state, logging
├── tools/               ← copied: MCP / A2A / inline functions as ADK tools
├── tests/
│   ├── conftest.py      ← copied
│   ├── test_graph.py    ← rendered: this graph's structure
│   ├── test_nodes.py    ← rendered: every node against its contract
│   └── test_a2a.py      ← rendered: agent card, both invocation modes, polling
├── run_once.py          ← copied: run the workflow once (§7)
├── graph.json           ← the compiled plan; also served by GET /graph
├── requirements.txt     ← rendered from the node set
├── .env / .env.example  ← rendered by the env builder
├── Dockerfile / docker-compose.yml / ruff.toml / pytest.ini  ← copied
└── README.md            ← rendered, including an ASCII drawing of the graph
```

Rendering is **deterministic**: re-rendering an unchanged workflow produces a
zero-line diff, which is what makes the stable node naming worth having.

### The renderer validates what it writes

Rendering happens in a temporary directory and is only moved into place once
every check passes, so a failed build never leaves a half-written package.

1. **Canvas code bodies are compiled** before being embedded, so a syntax error
   fails packaging *naming the canvas node* instead of producing a package that
   raises on import.
2. **The whole tree is byte-compiled.**
3. **The graph is imported** in a subprocess. This step exists because
   byte-compiling is not enough: a template bug once emitted `DEFAULT_ROUTE =
   null`, which is *syntactically* valid Python and only failed at import with
   `NameError`. Importing also runs ADK's graph validation.
4. **`ruff check --select F`** runs over the tree — pyflakes rules catch
   generator bugs (unused imports, undefined names); ruff's style rules are
   opinions about code nobody hand-writes. Findings are advisory warnings.

### Node behaviour is real Python

A canvas `FUNCTION` or `TRANSFORM` body is emitted as a module-level function,
not a string handed to `exec`:

```python
def _transform(data: dict) -> dict:
    # The canvas expression, emitted as ordinary Python.
    result = None
    result = {'tier': 'premium', 'score': data['score']}
    return result if isinstance(result, dict) else {"result": result}
```

**Trust boundary:** that code came from whoever built the canvas and runs with
the package's privileges. The container is the boundary — treat canvas authorship
as equivalent to commit access to the package.

### Two rules that are load-bearing

Both fail at run time rather than at import, so they are worth knowing:

1. **Exactly one terminal node.** ADK raises *"multiple terminal nodes produced
   output"* during finalisation — after the work is done. Fan-out is fine
   mid-graph, but every path must reconverge.
2. **The terminal node must emit content.** The A2A executor promotes the
   aggregated status message's parts into the result artifact and only then
   marks the task `completed`. An event carrying only `output` produces no
   artifact and the task sits at `working` forever.
   `nodes/base.py::terminal_event` emits both, and only the END node does.

### `a2a-sdk` is pinned below 1.0 deliberately

In 1.x `AgentCard` became a protobuf message that dropped `url`,
`preferred_transport` and `protocol_version` in favour of a repeated
`supported_interfaces`, so `core/agent_card.py` would not build there. ADK
supports both generations behind its own `IS_A2A_V1` switch, so without the pin
an install silently resolves to 1.x. Card construction is isolated in one module
so a future move is a single-file change.

### Credentials never reach the package

The generated directory is a git repository *and* serves its own `graph.json`
from `GET /graph`, so a credential in either would be exposed twice over.

`NodeDefinition.secret_config_keys` declares which config keys hold credentials.
The compiler moves each to an environment variable, carries the canvas value as
that variable's **default**, and strips it from the config the plan holds — so
`graph.json`, the rendered modules and `GET /graph` are clean *by construction*
rather than by remembering to redact.

The value lands only in `.env`, which both `.gitignore` and `.dockerignore`
exclude. The committed `.env.example` names every variable, says which node
needs it, and leaves credentials blank.

Each tool gets its own variable, so an agent with three tools gets three:

```
mcp  Search  -> MCP_SEARCH_URL
mcp  Lookup  -> MCP_LOOKUP_URL
a2a  Helper  -> A2A_HELPER_URL / A2A_HELPER_TOKEN
```

### Authentication

Set `A2A_AUTH_TOKEN` and the package requires `Authorization: Bearer <token>` on
every JSON-RPC call **and advertises it on the agent card**, so callers discover
the requirement rather than finding out from a 401:

```json
"security": [{"bearerAuth": []}],
"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
```

The agent card and `/health` stay public — the card is how a caller learns a
token is needed, and Cloud Run's probe cannot send one.

Left blank, the agent accepts unauthenticated calls and logs a warning at
start-up. Fine behind a private network or Cloud Run IAM; not fine on the open
internet.

### Task persistence

`to_a2a` defaults to an in-memory task store. On Cloud Run that means a
`tasks/get` routed to a different instance than the one that accepted the
message returns not-found, and a restart drops every in-flight task.

Set `TASK_STORE_DSN` to a SQLAlchemy async DSN and tasks are persisted instead:

```
TASK_STORE_DSN=postgresql+asyncpg://user:pass@host:5432/tasks
```

A DSN the driver cannot parse fails at start-up naming the variable, rather than
quietly falling back — persistence you asked for and did not get is worse than a
clear error.

### Cloud Run

The Dockerfile expands `PORT` at start-up (JSON-form `CMD` wrapped in `sh -c`,
so signals still reach the process) and runs as a non-root user. Set
`CLOUD_RUN_URL` to the service URL: it becomes the agent card's `url`, which is
how other agents find this one.

`AGENT_NAME` must be a valid Python identifier — `agent.py` passes it to
`Workflow(name=...)`, which ADK validates as a node name.

For local Vertex AI, `docker-compose.yml` mounts the host's gcloud credentials:

```yaml
volumes:
  - ~/.config/gcloud:/home/agent/.config/gcloud:ro
```

Run `gcloud auth application-default login` once on the host, or set
`GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`.

## 10. Frontend Architecture

### Zustand store — `src/store/workflowStore.ts`

Single source of truth. All components subscribe to slices directly — no prop drilling.

Key state:

| Field | What it holds |
|-------|--------------|
| `nodes` / `edges` | ReactFlow canvas state |
| `selectedNodeId` | Which node's config panel is open |
| `currentWorkflow` | The workflow being edited |
| `lastCompile` | Most recent compile result (`version_id`, `is_valid`, `errors`) |
| `executions` | Execution history shown in the Runs panel |
| `nodeStatus` | `{nodeId: "success"|"failed"|"running"}` — drives canvas badges |
| `isSaving` / `isExecuting` / `isDeploying` | Toolbar loading states |

Key actions:
- `addNode(type, position)` — creates node with default data, adds to canvas
- `saveAndCompile()` — POSTs canvas, stores `lastCompile`
- `executeWorkflow(payload)` — POSTs to execute endpoint
- `loadNodeLogs(workflowId, executionId)` — fetches `NodeExecutionLog` rows, sets `nodeStatus`

### Component map

| File | Responsibility |
|------|---------------|
| `Canvas/index.tsx` | ReactFlow canvas, drop zone, edge creation, node selection |
| `NodePalette/index.tsx` | Left sidebar, draggable tiles, fetches `/palette` on mount |
| `NodeConfigPanel/index.tsx` | Right panel, type-specific config forms, calls `updateNodeConfig()` |
| `Toolbar/index.tsx` | Save/Compile, Run modal (schema-driven or raw JSON), Deploy, Runs drawer |
| `WorkflowList.tsx` | Landing page, list / create / delete workflows |
| `nodes/NodeComponents.tsx` | `WorkflowNode`, `OrchestratorAgentNode`, `NodeStatusBadge` renderers |
| `nodes/index.ts` | `STATIC_PALETTE` array + `PALETTE_BY_TYPE` lookup map |
| `api/client.ts` | Axios wrapper: `workflowApi` + `resourceApi` |

**`OrchestratorAgentNode`** reads connected tools live from `edges` in the store and renders them in the node body — this is a display-only read, not a backend call.

**`NodeStatusBadge`** is an absolute-positioned overlay (top-right corner of the node card) that shows ✓ / ✗ / spinner based on `nodeStatus[id]`.

**`RunInputModal`** in the Toolbar offers an **Invocation** choice (*Message* —
wait for the answer / *Task* — submit, then poll `tasks/get`) and reads
`config.payload_schema.fields` from the `A2A_START` node in the store. If fields
are defined it renders a typed form (text, number, boolean toggle); otherwise it
falls back to a raw JSON editor. Because the form and a real A2A caller read the
same contract, the test form cannot drift from what the agent advertises.

---

## 11. End-to-End Walkthrough

A workflow that receives a question, delegates to a remote diet agent via the
orchestrator, and returns the answer.

### Canvas layout

```
[A2A_START] ──output──> [ORCHESTRATOR_AGENT] ──output──> [END]
                                 ▲
                          [REMOTE_AGENT]  (connected via the bottom "tools" handle)
```

### Step 1 — Save & Compile

`POST /api/v1/workflows/{wf_id}/versions`  body: `{ canvas: { nodes: [...], edges: [...] } }`

**Layer 1** checks each node's config against its `config_schema`.

**Layer 2** builds a NetworkX DiGraph and confirms: exactly one `A2A_START`,
exactly one `END`, everything reachable, no rogue cycles. It notices the
`REMOTE_AGENT → ORCHESTRATOR_AGENT` edge uses `target_handle="tools"`, so it is
tool-provision rather than flow, and exempts that node from connectivity rules.

**Layer 3** compiles the IR, embedding the remote agent into the orchestrator's
`resolved_tools`. The response carries `version_id`, `is_valid`, `errors` and
`warnings`; the IR is stored as `ir_json`.

### Step 2 — Run (test)

`POST /api/v1/workflows/{wf_id}/versions/{version_id}/test`
body: `{ payload: {"question": "..."}, mode: "message" }`

1. **Compile the graph plan** and check it under ADK. Three graph nodes
   (`a2a_start`, `n_orchestrator_agent_2`, `n_end_3`) — the remote agent is a
   *tool*, not a node. Its endpoint and token become `A2A_HELPER_URL` and
   `A2A_HELPER_TOKEN`, and are stripped from the config.
2. **Render or reuse the package.** Reused when the on-disk `graph.json` still
   matches the plan.
3. **Create a `WorkflowExecution`** row (`status=pending`) and return it
   immediately, so the UI can poll.
4. **In the background**, spawn `run_once.py --json` inside the package. It
   drives the workflow over the package's own A2A surface — a blocking
   `message/send` in message mode, or `message/send` plus `tasks/get` polling in
   task mode — while a hooked `Runner` records every node event.
5. **Record the outcome**: the result and A2A lifecycle into
   `WorkflowExecution.output`, and one `NodeExecutionLog` row per node event
   keyed by canvas node id.

```json
{
  "result": { "answer": "…" },
  "a2a": { "task_id": "6d8edadf-…", "state": "completed", "mode": "message", "polls": 0 },
  "duration_ms": 1421,
  "package_dir": "/…/packages/diet-router-abc12345",
  "warnings": []
}
```

### Step 3 — The canvas shows what happened

The Runs panel polls `GET /executions/{id}` until the status is terminal, then
`GET /executions/{id}/node_logs`. The store turns those rows into `nodeStatus`,
and `NodeStatusBadge` paints a ✓ or ✗ on each node that ran. A node on an
untaken branch has no row, so it stays unmarked. A looping node has one row per
iteration.

### Step 4 — Package

`POST /api/v1/workflows/{wf_id}/versions/{version_id}/package` renders the same
tree with lint and `git init`, and returns the directory plus a copy-paste
compose command. What was tested and what ships are the same code.

## 12. How to Add a New Node Type

Six steps. The important change from older versions of this document: a node
definition carries **no behaviour** — the code that runs lives in a Jinja2
template. Adding an `execute` method creates a second implementation that never
runs, and `tests/test_single_engine.py` will fail.

### 1. Declare the node type

```python
# app/nodes/tasks/my_node.py
from dataclasses import dataclass

from app.nodes.base import NodeDefinition, PaletteMetadata


@dataclass
class MyNode(NodeDefinition):
    node_type: str = "MY_NODE"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    output_handles: list = None

    # Only if the generated module returns an error payload instead of raising.
    supports_on_error_continue: bool = True
    # Any config key holding a credential, so the compiler moves it to the env.
    secret_config_keys: frozenset[str] = frozenset({"api_key"})

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="My Node",
            category="ai",              # triggers | ai | flow | data
            color="#8b5cf6",
            icon="Sparkles",            # any lucide-react icon name
            description="Does the thing",
            wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "api_key": {"type": "string"},
            },
            "required": ["endpoint"],
        }
        self.output_handles = ["output", "error"]
```

### 2. Register it

```python
# app/nodes/tasks/__init__.py — add to the imports and __all__
from .my_node import MyNode

# app/nodes/registry.py
from app.nodes.tasks import ..., MyNode
_register(MyNode())
```

### 3. Add it to the IR kind map

```python
# app/compiler/ir.py
_KIND_MAP = {
    ...
    "MY_NODE": "task.my_node",
}
```

### 4. Write the render template — this is where behaviour lives

```jinja
{# app/packaging/render_templates/nodes/my_node.py.j2 #}
{% include "nodes/_header.j2" %}
from google.adk import Event
from google.adk.agents.context import Context
from google.adk.workflow import node

from core.config import settings
from core.logging import node_logger
from core.state import merge_state
from nodes.base import NODE_KWARGS, node_error

NODE_ID = {{ node.name | tojson }}
ON_ERROR = {{ node.on_error | tojson }}
ENDPOINT = {{ endpoint | tojson }}
KEY_ENV = {{ key_env | tojson }}

log = node_logger(NODE_ID)


@node(name=NODE_ID, **NODE_KWARGS({{ node_kwargs }}))
async def {{ node.name }}(ctx: Context, node_input=None):
    data = node_input or {}
    try:
        result = {"did": "the thing"}
    except Exception as exc:  # noqa: BLE001
        if ON_ERROR != "continue":
            raise
        return node_error(NODE_ID, exc)

    merge_state(ctx, result)
    return Event(output={**data, **result})
```

**Three rules the generated module must follow.** Each package's own rendered
`tests/test_nodes.py` enforces them, and `tests/test_adk_contract.py`
proves why they matter against real ADK behaviour:

- The signature is `(ctx: Context, node_input=None)`. ADK's default `state`
  binding passes the upstream node's output to a parameter *named* `node_input`;
  any other name is looked up in `ctx.state` and raises if absent.
- **No return annotation.** A `-> Event` hint is inferred as the node's
  `output_schema`, and every downstream edge then fails ADK's edge validation
  with *"Schema mismatch"*.
- Only the terminal node emits content (`terminal_event`). An intermediate node
  returning `Event(message=…)` would put its text in the task's result artifact.

Then map the type to the template, and give the renderer its context:

```python
# app/packaging/render.py
NODE_TEMPLATES = {
    ...
    "MY_NODE": "nodes/my_node.py.j2",
}

# in _node_context()
if node.node_type == "MY_NODE":
    return {
        **base,
        "endpoint": config.get("endpoint") or "",
        # The compiler already moved api_key to an env var and stripped it.
        "key_env": env.get("api_key", ""),
    }
```

`tests/test_render.py::test_every_supported_node_type_has_a_template` fails if
you skip the mapping — otherwise the node would silently render as a
pass-through.

### 5. Add it to the frontend palette

```ts
// ../frontend/src/nodes/index.ts
{
  type: 'MY_NODE', label: 'My Node', category: 'ai',
  color: '#8b5cf6', icon: 'Sparkles', description: 'Does the thing',
  wave: 1, is_trigger: false, is_terminal: false,
  output_handles: ['output', 'error'],
},
```

### 6. Register the React renderer, and optionally a config form

```ts
// ../frontend/src/nodes/NodeComponents.tsx — add to the `generic` array
const generic = ['A2A_START', 'MY_NODE', ...]
```

```tsx
// ../frontend/src/components/NodeConfigPanel/index.tsx
{nodeType === 'MY_NODE' && <MyNodeForm cfg={cfg} save={save} />}
```

Without a form the panel still shows the metadata fields and a read-only schema
reference, so a new type is usable before its form exists.

### Checklist

```bash
cd backend
python -m pytest                                    # platform, incl. the ADK contract
cd app/packaging/template && python -m pytest        # the template's own suite
cd ../../../../frontend && npx tsc --noEmit && npm run build
```

