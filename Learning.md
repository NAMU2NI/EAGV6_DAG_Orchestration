# Session 8 — Learning Notes

Everything covered in our walkthrough of the codebase, screenshot by screenshot.

---

## 1. Project Structure

Two components that work together:

```
Assignment 8/
├── llm_gatewayV8/        ← Smart LLM proxy (FastAPI, port 8108)
└── S8SharedCode/
    ├── code/             ← Multi-agent orchestrator
    └── gateway/          ← Same gateway, lives here for the agent to use
```

---

## 2. The LLM Gateway

A **smart switchboard** in front of multiple LLM providers. The agent never calls providers directly — everything goes through the gateway.

### Two Pools

```
Request
  │
  ▼
Router Pool (4 tiny fast LLMs)
  → classifies input as TINY / LARGE / HUGE
  │
  ▼
Worker Pool (7 providers: Gemini, Groq, NVIDIA, GitHub, Cerebras, OpenRouter, Ollama)
  → does the actual work
```

### Tier Rules
| Tier | Token Range | Worker Order |
|---|---|---|
| TINY | < 1,000 | GitHub → OpenRouter → Groq … |
| LARGE | 1,000 – 8,000 | Gemini → Groq → NVIDIA … |
| HUGE | > 8,000 | 503 — input too large |

### Key Design: The Separation Wall
The router never sees the full prompt, tools, or agent context.
It only receives: `{token_count, 800-char sample}` → emits one word: `TINY / LARGE / HUGE`.

### Embedding Support (`POST /v1/embed`)
- Uses `nomic-embed-text` (Ollama local) with Gemini as fallback
- Fixed **768-dim** vectors for FAISS retrieval
- WARNING: changing the model after building an index invalidates all existing vectors

---

## 3. Gateway V8 Upgrades (over V7)

V8 is an **observability upgrade**, not an algorithmic one. Three additions:

### Delta 1 — Agent & Session Tagging
Every LLM call now carries `agent=<skill_name>` and `session=<run_id>` fields, logged to SQLite.

```
calls table row:
  provider=gemini  agent=researcher  session=s8-abc123  tokens=4100
```

### Delta 2 — `/v1/cost/by_agent`
Per-skill token breakdown for a session:
```json
{
  "researcher": {"calls": 10, "input_tokens": 12318, "output_tokens": 1126},
  "planner":    {"calls": 1,  "input_tokens": 814,   "output_tokens": 176}
}
```
V7 could not answer this — it had no `agent` column.

### Delta 3 — `/v1/chat/batch`
Send N requests, gateway fires them in parallel, returns results in order.
The orchestrator doesn't use it (it does parallelism at Python level via `asyncio.gather`).
Useful for external clients who don't want to write async code.

### Delta 4 — Retry-on-5xx
On a transient 5xx or timeout, gateway retries once with exponential backoff (0.5s → 1s, capped at 2s).
`retries` count is returned in the response.

### `agent_routing.yaml`
Maps skill names to preferred providers without any code changes:
```yaml
planner:    gemini    # strongest reasoning
critic:     groq      # fast pass/fail, ~120ms
retriever:  github    # simple lookup, cheapest
```
Priority: explicit `provider=` > `agent_routing.yaml` > `auto_route` > default failover.

---

## 4. Skills — The Core Design

**A skill is a triple:** prompt file + list of MCP tools + temperature. Nothing else.

```python
class Skill:
    prompt_path    # path to .md file
    tools_allowed  # e.g. ["web_search", "fetch_url"]
    temperature    # 0.0 for critic, 0.7 for researcher
    max_tokens
```

- **One `Skill` class**, parameterised by YAML — no Python subclass per skill
- **Every skill runs through the same dispatcher** in `skills.py`
- **What makes skills different = the text in the prompt file**

### Skills Catalogue (`agent_config.yaml`)

| Skill | Job | Tools | Special |
|---|---|---|---|
| planner | Decomposes query into DAG | none | First node every run |
| researcher | Multi-step web research | web_search, fetch_url | temperature=0.7 |
| retriever | FAISS knowledge search | search_knowledge | |
| distiller | Extract structured fields | none | critic:true |
| summariser | Condense long content | none | |
| critic | Pass/fail evaluation | none | temperature=0.0 |
| formatter | Final user-facing answer | none | Terminal node |
| coder | Write Python code | none | internal_successors:[sandbox_executor] |
| sandbox_executor | Run Python code | none | NOT an LLM call |
| browser | Reserved (stub) | none | Session 9 |

---

## 5. The Orchestrator — Growing DAG

The agent loop is a **growing directed graph** (NetworkX DiGraph).

- **Nodes** = skill instances
- **Edges** = dependencies (B waits for A's output)
- **Acyclic** = no backwards edges, no cycles

### The Executor Loop

```python
while True:
    ready = graph.ready_nodes()          # all predecessors complete/skipped
    if not ready and not graph.has_running():
        break

    # run all ready nodes IN PARALLEL
    outcomes = await asyncio.gather(*[_run_one(nid) for nid in ready])

    for nid, result in outcomes:
        graph.extend_from(nid, result)   # grow the graph
        store.write_node(...)            # persist
        store.write_graph(...)           # persist
```

### Five Ways the Graph Grows

| Mechanism | What happens |
|---|---|
| Planner's seed plan | Initial list of nodes emitted as JSON |
| `result.successors` | Any skill can emit new nodes dynamically |
| `internal_successors` in YAML | e.g. coder always triggers sandbox_executor |
| Critic auto-insertion | When `critic:true` skill has children, Critic inserted between |
| Recovery re-plan | Failed node → new Planner queued with failure report |

### The Meta-Level Shift (S7 → S8)

| | Session 7 | Session 8 |
|---|---|---|
| Model role | Picks next tool each step | Writes the whole plan once |
| Loop controller | Model (reactive) | Executor (Python) |
| Parallelism | None — sequential | Yes — asyncio.gather |

> **S7: model was inside the loop. S8: model writes the graph (the program). The executor runs it.**

---

## 6. The Planner

Always the **first node**. Reads the user query, emits a JSON plan.

### Prompt Structure (4 parts)
1. `prompts/planner.md` — available skills + instructions
2. `USER_QUERY` — the user's question
3. `MEMORY HITS` — FAISS-ranked items from past sessions (empty on first run)
4. `INPUTS` — echoes USER_QUERY (Planner has no upstream nodes)

### Output Format
```json
{
  "rationale": "one sentence plan",
  "nodes": [
    {"skill": "researcher", "inputs": ["USER_QUERY"],
     "metadata": {"label": "r1", "question": "What is..."}},
    {"skill": "formatter",  "inputs": ["n:r1"],
     "metadata": {"label": "out"}}
  ]
}
```

### Three Planning Rules
1. **Parallelize N items** — one node per item, not one sequential chain
2. **Insert Critic for strict constraints** — e.g. "exactly 5-7-5 syllables"
3. **Don't repeat failing steps** — if FAILURE appears in prompt, try differently

### The Planner is Just Another Skill
The Executor has no special Planner code. It runs through the same dispatcher as every other skill. The Planner appears in two situations:
- Session start (decomposition)
- Mid-run recovery (critic-fail or upstream failure)

---

## 7. The Label System (`n:<label>`)

### The Problem
The Planner (an LLM) needs to wire nodes together but doesn't know the integer IDs the orchestrator will assign.

### The Solution
The Planner invents **human-readable labels**. The orchestrator translates them to real IDs.

### How It Works (two passes in `extend_from`)

**Pass 1 — Create nodes, build label map:**
```
Planner emits: researcher (label:"r1"), formatter (label:"out")
Orchestrator assigns:  "r1" → n:2,  "out" → n:3
label_to_id = {"r1": "n:2", "out": "n:3"}
```

**Pass 2 — Resolve inputs, draw edges:**
```
formatter input "n:r1"
  → strip "n:" → suffix = "r1"
  → "r1" found in label_to_id → resolves to "n:2"
  → draw edge: n:2 → n:3
```

### Input Forms Supported
| Form | Resolves to |
|---|---|
| `"USER_QUERY"` | Original user query text |
| `"n:r1"` (label) | Real node ID via label_to_id |
| `"n:3"` (integer) | Direct node reference |
| `"art:sha256..."` | Artifact bytes |
| anything else | Falls back to parent node |

---

## 8. The Critic

An LLM that reads another LLM's output and emits `pass` or `fail` with a rationale.

### Auto-Insertion
Only `distiller` has `critic: true`. When distiller completes, the orchestrator **automatically** inserts a Critic between distiller and every one of its children:

```
BEFORE (what Planner planned):    AFTER (what orchestrator builds):
  distiller → formatter             distiller → critic → formatter
```

### On `pass` → child runs normally
### On `fail` → three things happen:
1. Child node marked `skipped`
2. Recovery Planner queued with failure rationale
3. Cap: one re-plan per branch maximum (prevents loops)

### The Haiku Experiment — The Critic's Blind Spot
Query: *"Write a haiku — MUST be exactly 4-6-4 syllables"*

Result: Coder wrote 5-7-5. Critic said **PASS**.

Why? The LLM matched on the keyword "syllable" and approved. It didn't actually count.

### Mechanism vs Policy
| | Mechanism | Policy |
|---|---|---|
| What it is | The splice wiring (flow.py, recovery.py) | What the Critic checks (critic.md) |
| Works | Tested with unit tests | Depends on LLM capability |
| Fix when broken | Change orchestrator code | Change critic.md or give Critic a tool |

> **Conflating the two produces a debugging session that touches the wrong code.**

### Fix for counting problems
Give Critic a `count_syllables` tool → verdict grounded in real arithmetic, not LLM guessing.

---

## 9. Formatter and SandboxExecutor — Trust and Verify

The Coder node has **two children** that run in parallel:

```
n:5 coder
  output = {
    "code":    "print(abs(2100000 - 3600000))",  ← Python
    "summary": "Paris and Berlin are closest"     ← LLM text
  }
       │
  ┌────┴────┐
  ▼         ▼
formatter   sandbox_executor
reads        reads
"summary"    "code"
   │            │
   ▼            ▼
user sees    runs: python main.py
 this         stdout: "1500000"
              logged to graph
```

| | Formatter | SandboxExecutor |
|---|---|---|
| Reads | `summary` (LLM words) | `code` (Python) |
| Trusts | The LLM | Nothing — runs it |
| Failure | Prompt problem | Code/logic problem |
| Output | User-facing answer | Actual computed result |

### SandboxExecutor is NOT an LLM call
It calls `sandbox.run_python(code)` directly — no gateway, no model.

### The Sandbox — Usability Boundary, NOT Security
```
DOES:                          DOES NOT:
  Scrub env vars (PATH,HOME)     Block network calls
  Fresh temp dir (deleted after) Restrict filesystem
  30s timeout                    Drop privileges
  Cap stdout/stderr at 1MB       Container/chroot isolation
```
> *"The sandbox catches mistakes, not attacks."*

### Two Failure Modes Once Sandbox Exists
- **LLM error** → fix the prompt or model
- **Subprocess error** → read stderr, fix the code

---

## 10. Persistence and Resume

Every graph modification writes to disk immediately.

### Directory Layout
```
state/sessions/<sid>/
    query.txt          ← original user query
    graph.json         ← entire graph (all nodes + edges)
    nodes/
        n_001.json     ← NodeState for n:1 (planner)
        n_002.json     ← NodeState for n:2 (researcher)
        ...
```

### Atomic Writes — No Half-Written Files
```python
tmp = path.with_suffix(".tmp")  # write to temp first
f.write(data)
os.replace(tmp, path)           # all-or-nothing swap
```
Crash before swap → old file safe. Crash after → new file safe. Never corrupted.

### graph.json is Human-Readable
You can `cat graph.json` and read every node's status, inputs, and result without running any code.
`AgentResult` (Pydantic) is saved via `model_dump(mode="json")` + `_result_typed:true` sentinel.
On load, validated back via `AgentResult.model_validate()` — fails loud (`SessionLoadError`) rather than silently degrading.

### Resume
```bash
python flow.py --resume s8-0be05ca6
```
1. Load `graph.json`
2. Reset any `running` nodes to `pending` (were mid-flight when killed)
3. Continue executor loop — complete nodes are treated as satisfied predecessors

### The Limitation — Node Boundary, Not Tool-Call Boundary
```
Researcher was on tool call 3 of 6 when killed
→ On resume: re-runs from tool call 1
   (all 3 previous tool calls repeated)
```
This is an explicit design boundary, not a bug. Fixing it requires ~60 lines in `mcp_runner.py`.

---

## 11. Recovery Classification

Not every node failure deserves a re-plan. Three categories, three responses.

### The Disaster That Led to This
First implementation: queue recovery Planner on every failure.
Result: 503 → Planner → 503 → Planner → 503 → … → 60 nodes, zero useful work.

### `classify_failure()` in `recovery.py`

```python
"503", "timeout", "connection"   → transient        → SKIP
"malformed", "ValidationError"   → validation_error → SKIP
everything else                  → upstream_failure → REPLAN
```

### Decision Table
| Reason | Action | Why |
|---|---|---|
| `transient` | skip | Gateway already retried; re-plan won't help |
| `validation_error` | skip | Prompt bug — fix the .md file, not the run |
| `upstream_failure` + skill=planner | skip | No Planner-recovers-Planner loops |
| `upstream_failure` + other skill | replan | Queue one recovery Planner |

### Safety Caps
- One re-plan per branch maximum
- Planner failures never trigger another Planner
- `MAX_NODES = 60` hard cap prevents runaway growth

### Unit Tests Pin the Classifier
22 tests in `tests/test_recovery.py`:
- 18 test `classify_failure()` against real gateway error strings
- 4 test the critic-fail splice mechanics

If the gateway changes its error format → tests break loudly → bug caught before it reaches a real run.

---

## 12. Memory Hits — FAISS Vector Search

At session start, called **once**:
```python
memory_hits = memory_svc.read(query)  # FAISS vector search over past sessions
```

The same hits are injected into **every skill's prompt** for that entire run (via `render_prompt`). No skill's Python code changes — the injection happens inside `render_prompt`.

### Smart Planner Behaviour
- Memory hits present → Planner can route through `retriever` or straight to `formatter`
- Memory empty → Planner schedules `researcher` nodes to fetch fresh

### Prompt Structure with Memory
```
[prompts/planner.md]
USER_QUERY: Find populations of London, Paris, Berlin
MEMORY HITS (3 from FAISS):
  - [fact] London population 2024
      chunk: London's population is approximately 9.7 million...
INPUTS:
  [{"id": "USER_QUERY", "kind": "query", "value": "Find populations..."}]
```

---

## 13. Anatomy of One Node Firing

Every node — Planner, Researcher, Critic, Formatter — goes through **the exact same dispatch path**:

```
resolve_inputs()     ← materialise input IDs into dicts
render_prompt()      ← template + inputs + memory hits
LLM().chat() or      ← gateway call (with agent= and session=)
  run_with_tools()
parse_skill_json()   ← strip markdown fences, extract JSON
return AgentResult   ← success, output, successors, elapsed_s
```

**What differs between skills:** only the `.md` prompt file and `tools_allowed`.
**The dispatch is identical.**

### Populations Query — Full Execution Trace

```
Wave 1:  n:1 planner        (no predecessors)
         → emits: london, paris, berlin researchers + coder + formatter

Wave 2:  n:2 researcher(london)  ─┐
         n:3 researcher(paris)   ─┤  IN PARALLEL (asyncio.gather)
         n:4 researcher(berlin)  ─┘  each calls web_search tool

Wave 3:  n:5 coder           (waits for n:2, n:3, n:4)
         → orchestrator auto-adds n:7 sandbox_executor

Wave 4:  n:6 formatter       ─┐  IN PARALLEL
         n:7 sandbox_executor ─┘

Final: formatter.output["final_answer"] → returned to user
```

---

## 14. Key Architectural Principles

| Principle | Where it appears |
|---|---|
| One class parameterised by YAML | `Skill` class in `skills.py` |
| Separation of concerns wall | Router never sees worker's prompt |
| Mechanism vs policy | Critic splice (mechanism) vs critic.md quality (policy) |
| Trust and verify | Formatter (trust) + SandboxExecutor (verify) run in parallel |
| Fail loud, never silent | `SessionLoadError`, malformed NodeSpec logs, atomic writes |
| Node boundary for resume | Re-runs interrupted nodes from top, not mid-tool-call |
| Classify before recovering | `classify_failure()` prevents recovery loops |
| Memory as shared context | FAISS hits injected into every skill's prompt once per session |
| Label system for wiring | Planner uses human labels; orchestrator translates to integer IDs |
| `MAX_NODES = 60` hard cap | Prevents infinite graph growth from runaway recovery |
