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

When you click a node, the right panel opens. This is `NodeConfigPanel/index.tsx`. It looks at the node's `type` and renders a different form for each type. For example, an `LLM_AGENT` node shows a dropdown to pick Google Gemini vs Vertex AI, then shows different fields depending on which provider you pick. All the form changes call `updateNodeConfig()` which updates the node's `config` object in the store.

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

## Step 4 — Running It

When someone hits "Run", the platform does **not** execute the workflow itself.
It renders the workflow's package and runs *that*, over the package's own A2A
interface.

That sounds like a detour, and it is the most important design decision in the
codebase. An earlier version had an engine here that walked the IR directly —
and it had already drifted from the generated packages. A `LOOP` ran real
iterations in the platform and a single pass in a container. Testing told you
about code that was never going to ship. Now there is one implementation, so a
test run and a deployment are the same thing.

### What happens

```
POST /workflows/{id}/versions/{vid}/test   {payload, mode}
  │
  ├─ compile the graph plan, and let ADK check it            (Step 3)
  ├─ render the package — or reuse it if nothing changed
  ├─ create a run record, return it straight away
  │
  └─ in the background: run the package's own run_once.py
         │  which calls the workflow the way a real caller would
         ▼
     { state, task_id, result, per-node trace }
         │
         ├─ the run record's output
         └─ one log row per node, keyed by canvas node
```

### The ADK graph

The generated `agent.py` is the whole topology, and it is short enough to read:

```python
root_agent = Workflow(
    name=settings.AGENT_NAME,
    edges=[
        (START, a2a_start),
        (a2a_start, n_condition_1),
        Edge(from_node=n_condition_1, to_node=n_transform_3, route="high_score"),
        Edge(from_node=n_condition_1, to_node=n_transform_4, route="low_score"),
        (n_transform_3, n_end_2),
        (n_transform_4, n_end_2),
    ],
)
```

Each node is one file in `nodes/`, and they all look like this:

```python
@node(name="n_transform_3", **NODE_KWARGS(timeout=30))
async def n_transform_3(ctx: Context, node_input=None):
    data = node_input or {}
    result = _transform(data)
    merge_state(ctx, result)
    return Event(output={**data, **result})
```

`node_input` is whatever the previous node returned — the same "dict flowing
through pipes" idea as before, except ADK does the passing. `ctx.state` is
where a node puts something later nodes need regardless of which branch ran.

### Special nodes

**CONDITION** returns `Event(route="high_score")`, and the edge in `agent.py`
maps each route value to a target. If two branches go to the *same* target they
must be merged onto one edge — ADK rejects two edges sharing the same pair of
endpoints.

**PARALLEL_FORK** fans out along several plain edges; ADK runs every successor.
Its branches then **have to** rejoin at a `MERGE`, because a workflow may end at
exactly one node.

**MERGE** becomes an ADK `JoinNode`. It waits for every branch and hands the
next node a dict keyed by which node produced what.

**LOOP** is a back-edge: the body routes back to the loop node, which decides
whether to go round again. The counter lives in `ctx.state`, and
`max_iterations` is a hard stop so a bad condition cannot spin forever.

**ORCHESTRATOR_AGENT** is an ADK `LlmAgent` inside the graph, with its connected
tools attached. It can call them several times before answering.

### Two rules that bite

Both fail while running rather than at import, so they are worth memorising:

1. **A workflow ends at exactly one node.** Fan out as much as you like in the
   middle, but every path has to come back together.
2. **The END node must emit content**, not just data. The A2A layer turns that
   content into the task's result. A node that returns only `output` leaves the
   caller polling a task that never finishes.

The validator catches both when you Save & Compile, which is why it complains
about two END nodes.

## Step 5 — The Node Types

### The entry node

| Node | What it does |
|------|-------------|
| `A2A_START` | Where every workflow begins. Another agent sends a message, and its payload is what the workflow receives. |

There is only one, because a packaged workflow is an agent: it is called, not
scheduled. If you want it to run on a timer, point Cloud Scheduler at it.

Its `payload_schema` is a list of named fields — that is both the contract the
agent advertises and what the Run panel builds its form from, so the two cannot
disagree.

### AI nodes

| Node | What it does |
|------|-------------|
| `LLM_AGENT` | One LLM agent. Bare, it is a single LLM call. Give it tools and it becomes an agent loop; wire it into another agent or a tool group and it becomes a sub-agent. |
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
| `LOOP` | Walks a list, or repeats until an exit condition becomes true. The list is read once on entry, so the body cannot change what is being walked. |
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

Packaging writes a real Python project. Nothing is generated by gluing strings
together: every file is either copied from a checked-in template or filled in
from a Jinja2 template.

```
my-workflow-abc12345/
├── main.py              ← starts the server
├── agent.py             ← the graph (the edge list is the whole topology)
├── nodes/               ← one file per canvas node, plus registry.py
├── core/                ← config, agent card, app assembly, state helpers
├── tools/               ← MCP servers, remote agents and functions as tools
├── tests/               ← the package tests itself: graph, nodes, A2A round-trip
├── run_once.py          ← run the workflow once from the command line
├── graph.json           ← the compiled plan
├── .env / .env.example  ← .env has your values; .env.example is the committed template
├── Dockerfile / docker-compose.yml
└── README.md
```

### It is checked before you get it

The renderer will not hand you a package that cannot run:

1. Any Python you typed on the canvas is compiled first, so a typo fails
   packaging *and names the node you typed it in*.
2. Every generated file is byte-compiled.
3. The graph is **imported** in a separate process. This catches things
   compiling cannot — a bug once emitted `null` instead of `None`, which is
   valid Python syntax and only failed at import.
4. `ruff` checks for unused imports and undefined names.

### Your credentials are not in it

The package is a git repository, and it serves its own `graph.json` over HTTP.
Anything you typed as a credential — an API key, a token, an MCP URL with a
password in it — is therefore **not** in any of those files. Each one became an
environment variable, and the value sits only in `.env`, which both
`.gitignore` and `.dockerignore` exclude. The committed `.env.example` names
every variable and leaves the secrets blank.

### Running it

```bash
cp .env.example .env      # fill in real values
docker compose up --build

curl http://localhost:8080/health
curl http://localhost:8080/.well-known/agent-card.json
```

The agent card is how other agents find this one and learn what it accepts.

### Calling it

Two ways, both `POST /`:

```bash
# Wait for the answer
-d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{
      "message":{"role":"user","kind":"message","messageId":"m1",
                 "parts":[{"kind":"text","text":"{\"text\":\"hello\"}"}]},
      "configuration":{"blocking":true}}}'

# Submit now, collect later
#   ...same, with "blocking": false → returns a task id
-d '{"jsonrpc":"2.0","id":"2","method":"tasks/get","params":{"id":"<task-id>"}}'
```

Use the second for a workflow too slow to hold a connection open for. By default
task ids only live in the process that created them; set `TASK_STORE_DSN` to a
database and they survive restarts and more than one instance.

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

User types `{"message": "what should I eat to lose weight?"}` and clicks Run on
the **Multi-Tool Orchestrator** workflow.

```
Browser
  └── POST /api/v1/workflows/{id}/versions/{vid}/test
        body: {"payload": {"message": "..."}, "mode": "message"}
        │
        ▼
Platform
  1. Compiles the graph plan from the stored IR, and builds it under ADK to
     check it is legal. Three graph nodes: a2a_start, n_orchestrator_agent_2,
     n_end_3. The three remote agents are *tools*, not nodes.
  2. Renders the package (or reuses it — nothing changed).
  3. Creates a run record and returns it, so the UI can start polling.
  4. Runs the package's run_once.py in a subprocess.
        │
        ▼
Inside the package
  run_once.py calls its own A2A endpoint:
        POST / {"method": "message/send", "configuration": {"blocking": true}}
        │
        ▼
  nodes/a2a_start.py
        payload → {"message": "what should I eat to lose weight?"}
        stored in ctx.state, passed on

  nodes/n_orchestrator_agent_2.py
        1. Builds its tools once, from the env vars the compiler assigned:
             a2a_diet_advisor(message)    → A2A_DIET_ADVISOR_URL
             a2a_text_summarizer(message) → A2A_TEXT_SUMMARIZER_URL
             a2a_calculator(message)      → A2A_CALCULATOR_URL
        2. LlmAgent(model=settings.LLM_MODEL, tools=[...])
        3. Runner.run_async():
             Gemini reads the question and the tool descriptions
             Gemini calls a2a_diet_advisor
             → POST to the diet advisor (A2A JSON-RPC message/send)
             → "Eat more protein and fewer processed foods..."
             Gemini writes its answer
        4. Returns {"result": "Here is some advice: ..."}

  nodes/n_end_3.py
        Emits the result as *content*, which is what makes the A2A task
        reach `completed` and carry a result artifact.
        │
        ▼
run_once.py prints:
  { "ok": true, "state": "completed", "task_id": "...",
    "result": {...},
    "trace": [a2a_start, n_orchestrator_agent_2, n_end_3] }
        │
        ▼
Platform
  → run record: status success, output = result + A2A lifecycle
  → one node log row per trace step, keyed by canvas node id
        │
        ▼
Browser
  Runs panel polls until the status is terminal, then fetches node logs
  → green ✓ on each canvas node that ran
  → the A2A task id, state and duration shown alongside the result
```

Notice what is *not* in that flow: the platform never interprets a node. It
compiles, renders, and then runs the same package a deployment would.

## Key Things to Remember

| Concept | What it means in practice |
|---------|--------------------------|
| **Always Save & Compile** | The canvas is just a drawing. Running and packaging both work from the compiled version, so an unsaved change has no effect. |
| **There is one execution path** | The platform does not run workflows itself. Testing renders the package and runs that, so what you test is what ships. |
| **`node_input` is just a dict** | Every node takes the previous node's dict and returns a dict. `ctx.state` is for values later nodes need whichever branch ran. |
| **Tool nodes are not graph nodes** | REMOTE_AGENT / TOOL / FUNCTION wired to an orchestrator's `tools` handle become tools it may call. They get no file of their own in the package. |
| **The package is just Python** | Open `agent.py` and `nodes/` in any generated package. Real modules, one per node, no magic. You can run `pytest` in there.  |
| **A workflow ends at one node** | And that node has to emit content, or the caller waits on a task that never completes. |
| **A2A and MCP are just HTTP** | Both protocols are HTTP POST with conventions about request/response shape. There's nothing exotic under the hood. |
| **Docker networking** | If the backend runs in Docker and agents run on your host machine, use `http://host.docker.internal:PORT` — not `http://localhost:PORT` — because `localhost` inside a container means the container itself. |
