# No-Code A2A Workflow Platform — Engineering KT

This document is a code-level knowledge transfer for engineers new to this codebase. It explains every layer of the system, how data flows from a canvas click all the way to a running container, and how to add or change things without breaking anything.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Database Models](#3-database-models)
4. [The Node System](#4-the-node-system)
5. [The Compiler Pipeline](#5-the-compiler-pipeline)
6. [IR Topology — the internal graph format](#6-ir-topology)
7. [The Execution Engine](#7-the-execution-engine)
8. [A2A and MCP Tool Protocols](#8-a2a-and-mcp-tool-protocols)
9. [The Packaging / Deploy Layer](#9-the-packaging--deploy-layer)
10. [Frontend Architecture](#10-frontend-architecture)
11. [End-to-End Walkthrough](#11-end-to-end-walkthrough)
12. [How to Add a New Node Type](#12-how-to-add-a-new-node-type)

---

## 1. System Overview

The platform lets users build AI workflows visually on a canvas and execute them. A workflow is a directed graph of **nodes** (triggers, AI agents, transforms, conditions, etc.) connected by **edges**.

The pipeline has five layers:

```
Canvas (browser)
    │  drag-and-drop nodes, draw edges
    ▼
Save & Compile (API)
    │  canvas JSON → validated IR JSON (stored in DB)
    ▼
Execute (API)
    │  IR JSON → WorkflowEngine traversal → per-node execute()
    ▼
Deploy (API)
    │  IR JSON → standalone FastAPI app → Docker image → running container
    ▼
Running Container
    │  self-contained HTTP server, no dependency on main backend
```

**Stack**

| Part | Technology |
|------|-----------|
| Backend | Python 3.11, FastAPI, SQLAlchemy (async), PostgreSQL |
| AI agents | Anthropic SDK, Google ADK, LangGraph |
| Tool protocols | MCP (FastMCP Streamable HTTP), A2A JSON-RPC |
| Frontend | React 18, TypeScript, ReactFlow, Zustand, Tailwind CSS |
| Packaging | Docker, docker-compose |

---

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
│   ├── canvas.py               ← Pydantic: CanvasNode, CanvasEdge, CanvasPayload (request body)
│   └── workflow.py             ← Pydantic: API response shapes
│
├── api/
│   ├── workflows.py            ← REST endpoints: /workflows, /versions, /execute, /node_logs, /deploy
│   └── resources.py            ← REST endpoints: /agents, /models, /tools, /datasources
│
├── nodes/
│   ├── base.py                 ← Abstract NodeDefinition base class + PaletteMetadata dataclass
│   ├── registry.py             ← NODE_REGISTRY dict, get_node_definition(), get_palette()
│   │
│   ├── triggers/
│   │   ├── http_trigger.py     ← HTTP_TRIGGER node
│   │   ├── schedule_trigger.py ← SCHEDULE_TRIGGER node
│   │   └── webhook_trigger.py  ← WEBHOOK_TRIGGER node
│   │
│   └── tasks/
│       ├── orchestrator_agent.py ← ORCHESTRATOR_AGENT — Anthropic / ADK / LangGraph agentic loop
│       ├── agent.py            ← AGENT node (simple single-call agent)
│       ├── model.py            ← MODEL node (direct LLM call with prompt template)
│       ├── tool.py             ← TOOL node (MCP tool — standalone or wired to orchestrator)
│       ├── remote_agent.py     ← REMOTE_AGENT node (A2A remote agent)
│       ├── function.py         ← FUNCTION node (inline Python)
│       ├── condition.py        ← CONDITION branch node
│       ├── loop.py             ← LOOP iteration node
│       ├── transform.py        ← TRANSFORM node (JMESPath / Python / Jinja2)
│       ├── end.py              ← END terminal node
│       ├── datasource.py       ← DATASOURCE node (MCP data source)
│       ├── human_approval.py   ← HUMAN_APPROVAL node (suspend + resume)
│       ├── subworkflow.py      ← SUBWORKFLOW node (nested workflow)
│       ├── parallel_fork.py    ← PARALLEL_FORK node
│       └── merge.py            ← MERGE node
│
├── compiler/
│   ├── ir.py                   ← IR dataclasses (IRNode, IR) + canvas → IR compiler
│   ├── schema_validator.py     ← Compiler layer 1: per-node config JSON Schema checks
│   └── semantic_validator.py   ← Compiler layer 2: graph topology rules (NetworkX)
│
├── runtime/
│   ├── context.py              ← ExecutionContext dataclass (shared state during one run)
│   ├── engine.py               ← WorkflowEngine: async graph traversal, node dispatch, DB logging
│   └── handlers.py             ← run_agent(), run_model(), run_tool() concrete helpers
│
└── packaging/
    ├── builder.py              ← build_runner_package() + build_docker_image()
    └── codegen.py              ← generate_standalone_app() — IR → self-contained FastAPI main.py
```

---

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
One row per run. Tracks `trigger_payload`, `output`, `status` (`pending → running → success/failed`), timestamps.

### `node_execution_logs`
One row per node per execution. Written by the engine as each node starts and finishes. The frontend reads these to show green tick / red X badges on canvas nodes.

```
workflows (1)
  └── workflow_versions (many)
        └── workflow_executions (many)
              └── node_execution_logs (many — one per node)
```

---

## 4. The Node System

### `NodeDefinition` — `app/nodes/base.py`

Every node type extends this abstract class. Required fields:

| Field | Purpose |
|-------|---------|
| `node_type` | String key, e.g. `"ORCHESTRATOR_AGENT"` |
| `palette` | `PaletteMetadata` — label, color, icon, category for the canvas sidebar |
| `config_schema` | JSON Schema dict — validates the node's config block at compile time |
| `output_handles` | Named output ports, e.g. `["output", "error"]` |
| `is_trigger` | `True` if this node starts an execution path |
| `is_terminal` | `True` if this node ends an execution path |
| `execute(node_config, input_data, context)` | Async method called by the engine |

### Registry — `app/nodes/registry.py`

`NODE_REGISTRY` is a plain dict populated at import time. Every node calls `_register(SomeNode())`.

`get_node_definition("ORCHESTRATOR_AGENT")` is how the engine looks up a node's class at runtime.

`GET /api/v1/workflows/palette` calls `get_palette()` which serialises the registry to JSON for the frontend sidebar.

### Tool-provider nodes (special case)

`TOOL`, `DATASOURCE`, `REMOTE_AGENT`, and `FUNCTION` nodes serve as tools for the `ORCHESTRATOR_AGENT`. They connect via the bottom **tools** handle on the orchestrator. At compile time they are **not** added to the execution graph — their configs are embedded into the orchestrator's `resolved_tools` block in the IR instead. The engine never calls `execute()` on them directly; the orchestrator dispatches to them internally and writes their `node_execution_log` rows itself using a separate DB session.

---

## 5. The Compiler Pipeline

Triggered by `POST /api/v1/workflows/{id}/versions` (the Save & Compile button).

```
CanvasPayload (Pydantic validated)
        │
        ▼
schema_validator.validate_schemas()       ← Layer 1
        │   Iterates every node, calls NodeDefinition.validate_config()
        │   (jsonschema.validate against config_schema)
        │   Returns list of error strings
        ▼
semantic_validator.validate_semantics()   ← Layer 2
        │   Builds a NetworkX DiGraph
        │   Rules checked:
        │     - At least one trigger node
        │     - At least one END node
        │     - No inbound edges on trigger nodes
        │     - No outbound edges on END nodes
        │     - No cycles unless through a LOOP node
        │     - CONDITION must define branches in config
        │     - PARALLEL_FORK must have ≥ 2 outbound edges
        │   Returns (errors, warnings)
        ▼
ir.compile_to_ir()                        ← Layer 3
        │   Builds IRNode objects for each canvas node
        │   Detects tool-provision edges (target_handle="tools")
        │   Embeds resolved_tools into ORCHESTRATOR_AGENT config
        │   Returns IR object
        ▼
IR.to_dict() → stored as ir_json in workflow_versions table
```

If any errors from layers 1 or 2 exist, `is_valid=False` is stored and execution is blocked.

---

## 6. IR Topology

The IR (`ir_json` in the DB) is the internal graph format the execution engine reads. Here is a minimal annotated example:

```json
{
  "workflow_version_id": "abc-123",
  "entrypoints": ["node_trigger_1"],
  "nodes": {
    "node_trigger_1": {
      "kind": "trigger.http",
      "node_type": "HTTP_TRIGGER",
      "config": { "method": "POST", "path": "/run" },
      "policies": { "timeout_seconds": 60, "retry_max_attempts": 1, "on_error": "fail" },
      "next": ["node_orch_1"],
      "branches": {}
    },
    "node_orch_1": {
      "kind": "task.orchestrator_agent",
      "node_type": "ORCHESTRATOR_AGENT",
      "config": {
        "framework": "anthropic",
        "model": "claude-opus-4-7",
        "system_prompt": "You are a helpful agent.",
        "resolved_tools": {
          "mcp_servers": [],
          "a2a_agents": [
            {
              "name": "diet-agent",
              "endpoint": "http://host.docker.internal:8082",
              "description": "Gives diet advice",
              "node_id": "node_remoteagent_2",
              "node_type": "REMOTE_AGENT"
            }
          ],
          "functions": []
        }
      },
      "next": ["node_model_3"],
      "branches": {}
    },
    "node_model_3": {
      "kind": "task.model",
      "node_type": "MODEL",
      "config": { "model_id": "...", "prompt_template": "Summarise: {{ result }}" },
      "next": ["node_end_4"],
      "branches": {}
    },
    "node_end_4": {
      "kind": "task.end",
      "node_type": "END",
      "config": {},
      "next": [],
      "branches": {}
    }
  }
}
```

**Key fields explained:**

| Field | Meaning |
|-------|---------|
| `entrypoints` | Node IDs the engine starts from (trigger nodes) |
| `next` | Ordered list of next node IDs for sequential flow. Empty for branch nodes. |
| `branches` | `{handle_name: [node_id, ...]}` — used by CONDITION (`"true"/"false"`), LOOP (`"loop_body"/"done"`), PARALLEL_FORK |
| `resolved_tools` | Only on ORCHESTRATOR_AGENT. Fully-dereferenced tool configs including the canvas `node_id` for DB logging. |

Tool-provider nodes (`REMOTE_AGENT`, `TOOL`, etc.) do **not** appear as top-level keys in `nodes` — they are embedded inside the orchestrator's config.

---

## 7. The Execution Engine

`app/runtime/engine.py` — `WorkflowEngine`

### How a run starts

`POST /api/v1/workflows/{id}/versions/{version_id}/execute`

1. API handler loads `WorkflowVersion`, deserialises `ir_json` → `IR` object
2. Creates `WorkflowExecution` row (status: `pending`)
3. Calls `WorkflowEngine(db).execute(ir, execution_id, trigger_payload)`

### Graph traversal (simplified)

```python
async def _execute_node(ir, node_id, input_data, ctx):
    ir_node = ir.nodes[node_id]
    defn = get_node_definition(ir_node.node_type)

    _log_node_start(...)                          # NodeExecutionLog status=running
    output = await defn.execute(ir_node.config, input_data, ctx)
    _log_node_end(... status="success")           # NodeExecutionLog status=success

    if node_type == "CONDITION":
        branch = output["_branch"]
        next_nodes = ir_node.branches[branch]     # follow matching branch
    elif node_type == "PARALLEL_FORK":
        asyncio.gather(*[_execute_node(...)])      # fan out concurrently
    else:
        for next_id in ir_node.next:              # sequential default
            output = await _execute_node(ir, next_id, output, ctx)

    return output
```

Data flows as a plain Python `dict` from node to node. Each node receives what its upstream neighbour returned.

### ExecutionContext — `app/runtime/context.py`

Passed into every `execute()` call:

| Field | Purpose |
|-------|---------|
| `execution_id` | Used for DB logging |
| `db_session` | The engine's shared `AsyncSession` |
| `node_outputs` | `{node_id: output}` for the entire run |
| `trigger_payload` | Original HTTP request body |

> **Session isolation warning:** Never call `ctx.db_session.flush()` from inside a node's `execute()` while another flush may be in flight. `orchestrator_agent.py` uses `AsyncSessionLocal()` to open a fresh independent session when logging tool-provider node status — this avoids the `"Session is already flushing"` error.

---

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

### How ORCHESTRATOR_AGENT uses tools (Anthropic framework)

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

### `app/packaging/codegen.py` — `generate_standalone_app(ir_dict)`

Turns the IR dict into a complete Python file. The output:
- Is a working FastAPI app in a single `main.py`
- Has **zero** imports from `app.*` — runs without the main backend
- Inlines all node logic into a `while _next:` dispatch loop
- Conditionally imports `httpx`, `anthropic`, `jmespath` based on node types present

### `app/packaging/builder.py` — `build_runner_package()`

Writes a self-contained package directory:

```
packages/<slug>-<version_id[:8]>/
├── main.py             ← generated standalone app
├── requirements.txt    ← minimal deps inferred from node types
├── Dockerfile
├── docker-compose.yml  ← maps a free local port to container :8000
└── ir.json             ← copy of IR for debugging
```

Then `build_docker_image()` tries three strategies in order:
1. **Docker Python SDK** — fastest if Docker daemon is reachable
2. **`docker build` CLI subprocess** — fallback if SDK unavailable
3. **Manual** — returns the `docker compose up --build` command for the user to run on their host

Deploy status is polled via `GET /api/v1/workflows/deploys/{deploy_id}`.

---

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

**`RunInputModal`** in the Toolbar reads `config.body_schema.fields` from the HTTP_TRIGGER node in the store. If fields are defined it renders a typed form (text, number, boolean toggle); otherwise falls back to a raw JSON editor.

---

## 11. End-to-End Walkthrough

Canvas layout for this example:

```
[HTTP_TRIGGER] ──output──► [ORCHESTRATOR_AGENT] ──output──► [MODEL] ──output──► [END]
                                      ▲
                               [REMOTE_AGENT]
                          (bottom "tools" handle)
```

### Step 1 — Save & Compile

`POST /api/v1/workflows/{wf_id}/versions`

- **schema_validator**: checks all node configs — passes
- **semantic_validator**: confirms trigger + END present, identifies REMOTE_AGENT → ORCHESTRATOR_AGENT as a tool-provision edge (not a flow edge)
- **compile_to_ir**:
  - `HTTP_TRIGGER` → `IRNode(next=["node_orch"])`
  - `REMOTE_AGENT` → **not** an IR node; config embedded in `node_orch.config.resolved_tools.a2a_agents` with its `node_id`
  - `ORCHESTRATOR_AGENT` → `IRNode(next=["node_model"])`
  - `MODEL` → `IRNode(next=["node_end"])`
  - `END` → `IRNode(next=[])`
- `is_valid=True` stored, frontend shows "Compiled successfully"

### Step 2 — Execute

`POST .../execute`  body: `{"user_data": "get a diet tip"}`

| Node | What happens |
|------|-------------|
| HTTP_TRIGGER | Passes payload through; `NodeExecutionLog` written: running → success |
| ORCHESTRATOR_AGENT | Builds tools list from `resolved_tools.a2a_agents`. First Anthropic call forces tool use (`tool_choice: "any"`). Anthropic picks `a2a__diet-agent`. `_dispatch_tool_call` writes `NodeExecutionLog` for REMOTE_AGENT (fresh session: running → success). Second Anthropic call returns final text. Own log: running → success. |
| MODEL | Renders Jinja2 prompt with orchestrator output, calls Anthropic, returns summary. Log: running → success. |
| END | Returns final state. Engine writes `WorkflowExecution.status = success`. |

### Step 3 — Canvas badges appear

Frontend polls execution status. When `success`, calls `GET .../node_logs`.

`loadNodeLogs()` sets `nodeStatus = {node_trigger: "success", node_orch: "success", node_remoteagent: "success", node_model: "success", node_end: "success"}`.

`NodeStatusBadge` renders a green ✓ on every node.

---

## 12. How to Add a New Node Type

### 1. Create the node class

```python
# app/nodes/tasks/my_node.py
from dataclasses import dataclass
from typing import Any
from app.nodes.base import NodeDefinition, PaletteMetadata

@dataclass
class MyNode(NodeDefinition):
    node_type: str = "MY_NODE"
    version: str = "1"
    palette: PaletteMetadata = None
    config_schema: dict = None
    output_handles: list = None

    def __post_init__(self):
        self.palette = PaletteMetadata(
            label="My Node", category="flow",
            color="#123456", icon="Star",
            description="Does something useful", wave=1,
        )
        self.config_schema = {
            "type": "object",
            "properties": {
                "my_param": {"type": "string", "default": "hello"},
            },
        }
        self.output_handles = ["output"]

    async def execute(self, node_config: dict, input_data: dict, context: Any) -> dict:
        param = node_config.get("my_param", "hello")
        return {"result": f"{param}: {input_data}"}
```

### 2. Register it

```python
# app/nodes/tasks/__init__.py
from .my_node import MyNode

# app/nodes/registry.py
from app.nodes.tasks import ..., MyNode
_register(MyNode())
```

### 3. Add to the IR kind map

```python
# app/compiler/ir.py
_KIND_MAP = {
    ...
    "MY_NODE": "task.my_node",
}
```

### 4. Add to the frontend palette

```typescript
// frontend/src/nodes/index.ts
{
  type: 'MY_NODE', label: 'My Node', category: 'flow',
  color: '#123456', icon: 'Star', description: 'Does something useful',
  wave: 1, is_trigger: false, is_terminal: false, output_handles: ['output'],
},
```

### 5. Register the React Flow renderer

```typescript
// frontend/src/nodes/NodeComponents.tsx
const generic = [
  ...,
  'MY_NODE',   // ← add here; WorkflowNode renders it automatically
]
```

### 6. Add a config form (optional)

```typescript
// frontend/src/components/NodeConfigPanel/index.tsx
function MyNodeForm({ cfg, s }: FormProps) {
  return (
    <label>
      My Param
      <input value={String(cfg.my_param || '')}
             onChange={e => s('my_param', e.target.value)} />
    </label>
  )
}

// In the type switch:
if (nodeType === 'MY_NODE') return <MyNodeForm cfg={config} s={setter} />
```

The compiler, engine, palette API, and canvas all pick it up automatically after steps 1–3.
