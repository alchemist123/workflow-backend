# How the No-Code Platform Works — Junior Engineer Guide

---

## The Big Picture

Think of this platform like a **visual programming tool**. Instead of writing Python code, a user drags boxes onto a canvas, connects them with arrows, and clicks "Run". The platform turns that picture into a real running program.

The journey from "drawing on a canvas" to "running container" has five steps:

```
1. Draw it       → user drags nodes onto the canvas in the browser
2. Save it       → browser sends the drawing as JSON to the backend
3. Compile it    → backend validates the drawing and converts it to a clean internal format
4. Run it        → backend follows the graph, node by node, executing each one
5. Package it    → backend generates a self-contained Python app you can ship as Docker
```

---

## Step 1 — The Canvas (Frontend)

The frontend is built with **React** and a library called **ReactFlow**. ReactFlow handles all the drag-and-drop, connecting arrows between boxes, zooming, panning — all of that.

### What a "node" looks like in code

Every box on the canvas is just a JavaScript object:

```json
{
  "id": "node_1",
  "type": "HTTP_TRIGGER",
  "position": { "x": 100, "y": 200 },
  "metadata": { "title": "Start", "description": "Receives HTTP requests" },
  "config": { "method": "POST", "path": "/run" }
}
```

An edge (the arrow between two boxes) is also just an object:

```json
{
  "id": "edge_1",
  "source": "node_1",
  "target": "node_2",
  "source_handle": "output",
  "target_handle": "input"
}
```

### Where all the state lives

All the canvas state (nodes, edges, which node is selected, etc.) lives in a single **Zustand store** (`workflowStore.ts`). Zustand is like a global variable that React components can subscribe to. When the store changes, only the components that care about that piece of data re-render.

```
workflowStore
    ├── nodes[]          ← all boxes on the canvas
    ├── edges[]          ← all arrows
    ├── selectedNodeId   ← which box is currently clicked
    ├── nodeStatus{}     ← {nodeId: "success" | "failed" | "running"} for status badges
    └── lastCompile      ← result of the last Save & Compile
```

### The config panel

When you click a node, the right panel opens. This is `NodeConfigPanel/index.tsx`. It looks at the node's `type` and renders a different form for each type. For example, a `MODEL` node shows a dropdown to pick Google Gemini vs Vertex AI, then shows different fields depending on which provider you pick. All the form changes call `updateNodeConfig()` which updates the node's `config` object in the store.

---

## Step 2 — Saving (The API Call)

When you click "Save & Compile", the frontend serialises all the nodes and edges into a JSON payload and POSTs it to:

```
POST /api/v1/workflows/{workflow_id}/versions
Body: { "canvas": { "nodes": [...], "edges": [...] } }
```

The backend receives this and runs it through the compiler.

---

## Step 3 — The Compiler (Three Layers)

The compiler is the brain of the platform. It takes the raw canvas drawing and produces a clean, normalised structure called the **IR (Intermediate Representation)**. Think of it like a factory QA check — the canvas JSON comes in messy (it has x/y positions, colour metadata, etc.), and the IR comes out clean (just the execution-relevant information).

The compiler runs three layers in sequence:

### Layer 1 — Schema Validator

Checks each node's `config` block against a JSON Schema definition. Every node type has a `config_schema` that says "this field must be a string", "this field must be a URL", etc.

If you put a number where a string is expected, you get an error here.

### Layer 2 — Semantic Validator

Checks that the **graph makes logical sense** as a program. It builds a real graph data structure (using a library called NetworkX) and runs rules like:

- "There must be at least one trigger node (something that starts the workflow)"
- "There must be at least one END node"
- "No cycles are allowed (a node can't point back to itself)"
- "A CONDITION node must have at least one branch configured"

### Layer 3 — IR Compiler

Converts the validated canvas into the IR format. This is mostly about:

1. **Simplifying** — strips out x/y positions, colours, icons; keeps only what the runtime engine needs
2. **Resolving tool connections** — if a `REMOTE_AGENT` node is wired into an `ORCHESTRATOR_AGENT` via the "tools" handle, the compiler extracts the remote agent's config and embeds it directly inside the orchestrator's config block. The remote agent node itself disappears from the graph.
3. **Building the `next` list** — for each node, the IR records which nodes should run after it

The result looks like this (simplified):

```json
{
  "entrypoints": ["trigger_node"],
  "nodes": {
    "trigger_node": {
      "node_type": "HTTP_TRIGGER",
      "config": { "path": "/run", "method": "POST" },
      "next": ["agent_node"]
    },
    "agent_node": {
      "node_type": "ORCHESTRATOR_AGENT",
      "config": {
        "model": "gemini-2.0-flash",
        "resolved_tools": {
          "a2a_agents": [
            { "name": "diet_advisor", "endpoint": "http://host:8005" }
          ]
        }
      },
      "next": ["end_node"]
    },
    "end_node": {
      "node_type": "END",
      "config": {},
      "next": []
    }
  }
}
```

The IR is saved to the database. The canvas JSON is also saved (so you can re-open the visual diagram), but the **engine only ever reads the IR**.

---

## Step 4 — Execution (The Engine)

When someone hits "Run", the engine takes the IR and traverses it like following a recipe.

### How it works

The engine starts at the trigger node and follows the `next` list, one node at a time:

```
trigger_node  →  agent_node  →  end_node
```

For each node, it:

1. Writes a log entry: "this node is now running" (shown as a spinning badge in the UI)
2. Calls the node's `execute()` method with the current `state` dict
3. The node does its work and returns a new `state` dict
4. Writes a log entry: "this node succeeded / failed"
5. Moves to the next node, passing `state` along

`state` is just a Python dict that flows through the whole workflow like water through pipes. Each node receives what the previous node returned.

### Special nodes

**CONDITION node** — instead of `next`, it has `branches`:

```json
"branches": {
  "true": ["premium_node"],
  "false": ["standard_node"]
}
```

When the condition node runs, it evaluates a Python expression against the current `state` and sets `_branch = "true"` or `"false"`. The engine reads that and follows the matching branch.

**PARALLEL_FORK node** — runs all its branches at the same time using `asyncio.gather()`.

**ORCHESTRATOR_AGENT node** — runs a full AI agent loop (Google ADK) that can call tools multiple times before returning a final answer.

---

## Step 5 — The Node Types

### Trigger nodes (start a workflow)

| Node | What it does |
|------|-------------|
| `HTTP_TRIGGER` | Workflow starts when someone POSTs to a URL |
| `SCHEDULE_TRIGGER` | Workflow starts on a cron schedule |
| `WEBHOOK_TRIGGER` | Workflow starts when a webhook fires |

### AI nodes

| Node | What it does |
|------|-------------|
| `MODEL` | One LLM call — give it a prompt template, get text back. No tool use, no multi-turn. |
| `AGENT` | Google ADK single-turn agent. Can connect to MCP tools or A2A remote agents. |
| `ORCHESTRATOR_AGENT` | Full AI agent loop — picks tools, calls them, reasons about results, repeats until done. |

### Tool provider nodes (wire these into ORCHESTRATOR_AGENT via the tools handle)

| Node | What it does |
|------|-------------|
| `TOOL` | Connects to an MCP server. The orchestrator discovers and calls its tools. |
| `DATASOURCE` | Same as TOOL, semantically for read-only data sources. |
| `REMOTE_AGENT` | Calls another AI agent via the A2A protocol. Looks like just another tool to the orchestrator. |
| `FUNCTION` | Inline Python code you write directly in the config panel. |

### Flow control nodes

| Node | What it does |
|------|-------------|
| `CONDITION` | `if/else` branching — evaluates a Python expression against the current state |
| `LOOP` | Iterates over a list or repeats until a condition becomes false |
| `TRANSFORM` | Reshapes data between nodes. Three modes: JMESPath, Jinja2, Python |
| `PARALLEL_FORK` | Splits execution into parallel branches |
| `MERGE` | Rejoins parallel branches |

**TRANSFORM modes explained:**

- **JMESPath** — a query language for JSON. Like `{name: user.name, total: length(items)}` extracts specific fields from a nested object.
- **Jinja2** — a template language. `"Hello {{ name }}, you have {{ count }} items."` fills in values from the state dict.
- **Python** — runs arbitrary safe Python. Assign `result = ...` and that becomes the output.

---

## The Two Tool Protocols: MCP and A2A

The platform supports connecting to external services using two standard protocols.

### MCP (Model Context Protocol)

MCP is a protocol for AI agents to talk to tools. Think of it like a standardised USB plug — any MCP-compatible tool server plugs into any MCP-compatible AI agent without custom glue code.

**How a connection works:**

```
1. Open a session
   Client → "Hello, I'm initializing a session"
   Server → "OK, here's your session"

2. Discover available tools
   Client → "What tools do you have?"
   Server → "search_web, lookup_database, send_email"

3. Call a tool
   Client → "Call search_web with query='diet tips'"
   Server → "Result: [article1, article2, ...]"
```

**In code (using the official `mcp` Python SDK):**

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

# Discovery — find out what tools exist
async with streamablehttp_client("http://tool-server/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()     # → list of Tool objects

# Tool call — fresh session per call
async with streamablehttp_client("http://tool-server/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("search_web", arguments={"query": "diet tips"})
        # result.structuredContent → dict if the tool returns structured JSON
        # result.content           → list of text/image parts otherwise
        # result.isError           → True if the tool reported an error
```

We open a **fresh session for every tool call**. This keeps things simple and avoids session-expiry problems.

### A2A (Agent-to-Agent Protocol)

A2A is a protocol for AI agents to talk to each other. It's built on JSON-RPC — a simple standard for calling functions over HTTP.

**The request the orchestrator sends:**

```json
POST /
{
  "jsonrpc": "2.0",
  "id": "some-uuid",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{ "kind": "text", "text": "Give me a diet tip" }]
    }
  }
}
```

**The response the remote agent returns:**

```json
{
  "result": {
    "artifacts": [
      { "parts": [{ "kind": "text", "text": "Eat more protein..." }] }
    ]
  }
}
```

The `_a2a_call()` helper extracts the text from `result.artifacts[0].parts[0].text`. That text becomes the return value of the tool for the ADK agent.

---

## The Orchestrator Agent — How the AI Loop Works

The `ORCHESTRATOR_AGENT` node uses **Google ADK** (Agent Development Kit). Here's what happens inside step by step:

```
1. Collect all connected tools
   → MCP servers:     open session, list_tools(), wrap each as FunctionTool
   → A2A agents:      wrap _a2a_call() as a FunctionTool
   → Inline functions: wrap exec() as a FunctionTool

2. Create an LlmAgent with those tools + system prompt
   → System prompt gets "You MUST use tools for every request" appended

3. Runner.run_async() streams events:
   → LLM sees user message + tool descriptions
   → LLM decides which tool to call
   → ADK calls the Python FunctionTool
   → Tool result goes back to the LLM
   → LLM either calls another tool or gives its final answer

4. The final is_final_response() event contains the answer text
```

**Key point:** The LLM never directly makes HTTP calls. It says "call `a2a_diet_advisor` with `message='give me a tip'`" and ADK runs our Python function which makes the actual HTTP call. The LLM only ever sees function names and their results.

---

## Step 6 — Packaging (The Standalone Container)

When you click "Deploy", the platform generates a completely self-contained Python project. It has **zero dependency on this backend** — no database, no shared volumes, nothing. You can copy the folder to any machine and run it.

### How the code is generated

`codegen.py` reads the IR and writes a `main.py` file. It inlines every node's logic as actual Python code inside a `while _next:` loop:

```python
# Generated main.py (simplified)
async def run_workflow(trigger_payload: dict) -> dict:
    state = trigger_payload
    _next = "trigger_node"

    while _next:
        if _next == "trigger_node":
            state = state.get("body", state)
            _next = "agent_node"

        elif _next == "agent_node":
            # all the orchestrator logic inlined here
            _orch_result = await _run_orchestrator({...config baked in...}, state)
            state = _orch_result
            _next = "end_node"

        elif _next == "end_node":
            _next = None

    return state
```

The `while _next:` loop IS the engine. Simple, readable, no framework needed.

### What files are in the generated package

```
my-workflow-abc12345/
├── main.py            ← the entire workflow as standalone Python
├── requirements.txt   ← only what this specific workflow needs
├── Dockerfile         ← python:3.11-slim + uv for fast installs
├── docker-compose.yml ← mounts gcloud credentials, maps a free port
├── .env               ← blank key stubs (fill these in before running)
├── .env.example       ← documented template with instructions
├── ir.json            ← snapshot of the IR (useful for debugging)
└── README.md          ← step-by-step quick start
```

### How Vertex AI auth works in the container

The `docker-compose.yml` mounts your laptop's gcloud credentials folder into the container:

```yaml
volumes:
  - ~/.config/gcloud:/root/.config/gcloud:ro
environment:
  - GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json
```

Run this once on your laptop:

```bash
gcloud auth application-default login
```

That saves credentials to `~/.config/gcloud/`. The Docker container reads them through the mounted volume. No API keys in files, no secrets to manage.

---

## The Test Agents

The `test-agents/` directory has four lightweight agents you can run locally to test workflows without real AI or external services:

| Agent | Port | Logic |
|-------|------|-------|
| `diet-advisor` | 8005 | Returns nutrition tips based on keywords in the message |
| `sentiment-analyzer` | 8002 | Counts positive/negative words, returns a score 0–1 |
| `text-summarizer` | 8003 | Scores sentences by word frequency, returns top-N |
| `calculator` | 8004 | Parses and evaluates math expressions safely using Python's `ast` module |

Each agent handles **both formats**:

```python
@app.post("/")
async def handle_message(request: Request):
    body = await request.json()

    # Format 1: A2A JSON-RPC (sent by ORCHESTRATOR_AGENT)
    if body.get("method") == "message/send":
        parts = body["params"]["message"]["parts"]
        message = " ".join(p["text"] for p in parts if p["kind"] == "text")
        ...
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": {"artifacts": [...]}})

    # Format 2: Simple REST (sent by standalone REMOTE_AGENT node)
    message = body.get("message", "")
    ...
    return JSONResponse({"response": "...", "agent": "diet-advisor"})
```

The workflow seed scripts (`workflows/seed_*.py`) create workflow definitions in the database by calling the backend API. They don't run the workflows — they just register them so they appear in the UI.

---

## How Everything Connects — One Full Example

User types `{"message": "what should I eat to lose weight?"}` and clicks Run on the **Multi-Tool Orchestrator** workflow.

```
Browser
  └── POST /api/v1/workflows/{id}/execute  ← body: {"message": "..."}
        │
        ▼
Engine loads IR from database
  → starts at HTTP_TRIGGER node
  → state = {"message": "what should I eat to lose weight?"}
  → next = ORCHESTRATOR_AGENT

ORCHESTRATOR_AGENT._run_adk():
  1. Wraps A2A agents as FunctionTools:
       a2a_diet_advisor(message)    → POST http://host:8005/
       a2a_text_summarizer(message) → POST http://host:8003/
       a2a_calculator(message)      → POST http://host:8004/

  2. Creates LlmAgent("gemini-2.0-flash", tools=[...])

  3. Runner.run_async() starts:
       Gemini reads: "what should I eat to lose weight?" + tool descriptions
       Gemini decides: call a2a_diet_advisor
       ADK runs our function → HTTP POST to diet-advisor (A2A JSON-RPC)
       Diet advisor returns: "Eat more protein and fewer processed foods..."
       Gemini writes final answer: "Here is some advice: ..."

  4. Returns {"result": "Here is some advice: ..."}

state = {"result": "Here is some advice: ..."}
  → next = END

END: _next = None → workflow returns state

Engine writes WorkflowExecution.status = "success"
Frontend polls for node logs → shows green ✓ on every canvas node
```

---

## Key Things to Remember

| Concept | What it means in practice |
|---------|--------------------------|
| **IR is the source of truth** | The canvas is just a drawing. Once compiled, the engine only reads the IR. Always Save & Compile after any canvas change. |
| **`state` is just a dict** | Every node takes a Python dict and returns a Python dict. That dict passes from node to node until END. |
| **Tool nodes vanish from the IR** | REMOTE_AGENT / TOOL / FUNCTION nodes wired to an orchestrator get embedded inside the orchestrator's config. They don't appear as separate nodes in the IR. |
| **The packaged container is just Python** | Open `main.py` in any generated package. It's plain readable Python. No magic. |
| **A2A and MCP are just HTTP** | Both protocols are HTTP POST with conventions about request/response shape. There's nothing exotic under the hood. |
| **Docker networking** | If the backend runs in Docker and agents run on your host machine, use `http://host.docker.internal:PORT` — not `http://localhost:PORT` — because `localhost` inside a container means the container itself. |
