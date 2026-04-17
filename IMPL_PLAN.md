# vibeqlite Implementation Plan

Phases map directly to the build order in [ARCHITECTURE.md](ARCHITECTURE.md).
Complete them in order — each phase produces a working, testable increment.

Pre-implementation tasks (system prompt, API contract, cluster.yaml, token
measurement) are tracked in [PLAN.md](PLAN.md). Complete those first.

---

## Stack

- **Language**: Python 3.11+
- **HTTP server**: FastAPI + uvicorn
- **HTTP client** (gossip): httpx (async)
- **SQL classification**: sqlglot
- **Token counting**: tiktoken (fallback: `len(text) // 4`)
- **LLM backend**: Ollama (local) via REST — abstracted behind `LLMClient`
- **Config**: PyYAML
- **Tests**: pytest + httpx async test client

---

## Phase 0 — Project Scaffold

**Goal**: Repo has the correct layout, dependencies install, and imports resolve.

### Files to create

```
pyproject.toml          # or requirements.txt — project deps
node/
  __init__.py
  server.py             # empty FastAPI app
  llm_engine.py         # empty LLMClient stub
  memory.py             # empty MemoryDoc stub
  prompts/
    system.md           # ✅ already committed
cluster/
  __init__.py
  gossip.py             # empty
  registry.py           # empty
  clock.py              # empty
cli/
  __init__.py
  vibeqlite.py          # empty REPL stub
cluster.yaml.example    # example config (from PLAN.md Task 3)
```

### Tasks

- [ ] Create `pyproject.toml` with deps: `fastapi`, `uvicorn`, `httpx`, `sqlglot`,
      `tiktoken`, `pyyaml`, `pytest`, `pytest-asyncio`
- [ ] Verify `python -m pytest` runs (0 tests, 0 failures)
- [ ] Verify `uvicorn node.server:app` starts without import errors

### Done when

`uvicorn node.server:app --port 8001` starts and `GET /status` returns `404`
(FastAPI default — no routes yet).

---

## Phase 1 — Single Node, Single Query

**Goal**: One node answers a SELECT and an INSERT against an in-memory memory doc.
No persistence. No gossip. This is the core loop.

### Files to create/modify

```
node/memory.py          # MemoryDoc: load, save, get_text, update_table_section
node/llm_engine.py      # LLMClient: call_ollama(), build_prompt(), parse_response()
node/server.py          # POST /query, GET /status — minimal
```

### Tasks

**`memory.py`**
- [ ] `MemoryDoc` class: holds the memory document as a string
- [ ] `get_text() -> str` — returns the full document
- [ ] `update_table_section(table_name: str, new_section: str)` — replaces the
      `## Table: <name>` block using a regex section match
- [ ] `update_schema_section(new_section: str)` — replaces the `## Schema` block
- [ ] `token_estimate() -> int` — tiktoken count, fallback to `len // 4`
- [ ] Initialise a blank memory doc with the correct header format if none exists

**`llm_engine.py`**
- [ ] `LLMClient` class, constructed with `node_id`, `personality`, `model`, `base_url`
- [ ] `_load_system_prompt() -> str` — reads `node/prompts/system.md`, substitutes
      `{NODE_ID}` and `{PERSONALITY}`
- [ ] `_build_prompt(memory_doc: str, task: str) -> str` — substitutes `{MEMORY_DOC}`
      and `{TASK}`
- [ ] `call(task: str, memory_doc: str) -> dict` — POSTs to Ollama `/api/generate`,
      parses JSON response, raises `LLMParseError` if response is not valid JSON or
      missing `task_type`
- [ ] `classify_sql(sql: str) -> Literal["read", "write", "ddl", "unknown"]` —
      uses sqlglot, no LLM call

**`node/server.py`**
- [ ] Load config from `cluster.yaml` (node id, personality, model, peers)
- [ ] Instantiate `MemoryDoc` and `LLMClient` at startup
- [ ] `POST /query` — classify SQL, call LLM, apply memory update if write,
      return response envelope (see API.md)
- [ ] `GET /status` — return node id, vector clock, token estimate, context usage %

### Done when

```bash
# Start node
uvicorn node.server:app --port 8001

# INSERT
curl -s -X POST localhost:8001/query \
  -H 'Content-Type: application/json' \
  -d '{"sql": "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)", "consistency": "yolo"}'

curl -s -X POST localhost:8001/query \
  -H 'Content-Type: application/json' \
  -d '{"sql": "INSERT INTO users (name, age) VALUES ('"'"'Alice'"'"', 30)", "consistency": "yolo"}'

# SELECT returns Alice
curl -s -X POST localhost:8001/query \
  -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT * FROM users", "consistency": "yolo"}'
```

---

## Phase 2 — Persistent Memory Document

**Goal**: Memory doc survives a node restart. Alice is still there after
`uvicorn` is killed and restarted.

### Files to create/modify

```
node/memory.py          # add file-backed load/save
```

### Tasks

- [ ] `MemoryDoc.__init__(path: Path)` — load from file if it exists, otherwise
      create blank
- [ ] `save()` — write to file atomically (write to `.tmp`, then `rename`)
- [ ] Call `save()` in `server.py` after every successful write or DDL response
- [ ] The memory file path is configurable per node (default: `data/{node_id}.md`)

### Done when

Insert Alice, kill the server, restart it, SELECT returns Alice.

---

## Phase 3 — DDL Gossip

**Goal**: `CREATE TABLE` updates the Schema section and broadcasts a synchronous
`ddl_change` gossip message to all peers.

### Files to create/modify

```
cluster/registry.py     # NodeRegistry: load cluster.yaml, list peers
cluster/gossip.py       # GossipClient: post_to_peer(), broadcast_ddl()
node/server.py          # POST /gossip endpoint; DDL path in POST /query
```

### Tasks

**`cluster/registry.py`**
- [ ] `NodeRegistry(config_path: Path)` — parses `cluster.yaml`
- [ ] `peers(exclude_self: str) -> list[NodeConfig]` — returns all nodes except self
- [ ] `NodeConfig`: id, url, personality

**`cluster/gossip.py`**
- [ ] `GossipClient(registry: NodeRegistry, self_id: str)`
- [ ] `post_gossip(node: NodeConfig, message: dict) -> bool` — async httpx POST to
      `/gossip`, returns True on 200
- [ ] `broadcast_ddl(message: dict) -> dict[str, bool]` — posts to all peers,
      **awaits all responses** (synchronous DDL), returns per-node success map
- [ ] `broadcast_write(message: dict)` — fire-and-forget async broadcast (Phase 4)

**`node/server.py`**
- [ ] `POST /gossip` — receives gossip message, dispatches on `type`:
  - `ddl_change`: call `memory.update_schema_section()` with the `schema_snapshot`,
    no LLM involved, return `{"status": "ok"}`
  - Other types: stub returning `{"status": "ok"}` for now
- [ ] In `POST /query`, DDL path: after updating local schema, call
      `gossip.broadcast_ddl()`, wait for responses, return OK with per-peer summary
      in response headers
- [ ] Flag peers that did not acknowledge as `SCHEMA_DIVERGED` in `/status`

### Done when

```bash
# Start two nodes on :8001 and :8002
# CREATE TABLE on node 8001
curl -X POST localhost:8001/query -d '{"sql":"CREATE TABLE users (id INTEGER, name TEXT)"}'

# SHOW SCHEMA on node 8002 reflects the table
curl -X POST localhost:8002/query -d '{"sql":"SHOW SCHEMA"}'
```

---

## Phase 4 — Second Node + Async Write Gossip

**Goal**: An INSERT on node A is gossiped asynchronously to node B. After a short
wait, SELECT on node B returns the row.

### Files to create/modify

```
cluster/gossip.py       # broadcast_write() — fire-and-forget
node/server.py          # gossip write messages after successful INSERT/UPDATE/DELETE
node/llm_engine.py      # add gossip_merge task type handling
```

### Tasks

**`cluster/gossip.py`**
- [ ] `broadcast_write(message: dict)` — fire-and-forget `asyncio.create_task()`
      to POST to each peer's `/gossip`; log failures, do not raise

**`node/server.py`**
- [ ] After a successful write, construct a `write` gossip message:
  - `from`: self node id
  - `type`: `"write"`
  - `vector_clock`: current clock (Phase 5 adds real clocks; use `{}` for now)
  - `statement`: the original SQL
  - `summary`: taken from the LLM response `explanation` field
- [ ] Call `gossip.broadcast_write()` after returning OK to client

**`node/server.py` — `/gossip` handler**
- [ ] `write` type: build a `gossip_merge` task string from the message, call LLM,
      apply `updated_table_section` to memory doc, save

**`llm_engine.py`**
- [ ] `build_gossip_task(message: dict) -> str` — formats the incoming gossip
      message into the `{TASK}` string for the system prompt

### Done when

```bash
# Insert on node 8001
curl -X POST localhost:8001/query -d '{"sql":"INSERT INTO users (name) VALUES ('"'"'Bob'"'"')"}'

# Wait 1s, select on node 8002
sleep 1
curl -X POST localhost:8002/query -d '{"sql":"SELECT * FROM users"}'
# Bob appears (or node 8002 says it probably heard about Bob)
```

---

## Phase 5 — Vector Clocks + Deduplication

**Goal**: Gossip messages carry vector clocks. Nodes deduplicate replayed messages.
Out-of-order delivery is handled gracefully.

### Files to create/modify

```
cluster/clock.py        # VectorClock
node/memory.py          # store and update clock in memory doc header
cluster/gossip.py       # attach clock to outgoing messages; check on receipt
node/server.py          # update clock on every write, DDL, compaction
```

### Tasks

**`cluster/clock.py`**
- [ ] `VectorClock(node_id: str, initial: dict = {})` 
- [ ] `tick() -> dict` — increment self counter, return copy
- [ ] `merge(other: dict) -> dict` — take max of each component
- [ ] `dominates(other: dict) -> bool` — True if self >= other on all components
- [ ] `to_dict() -> dict`

**`node/memory.py`**
- [ ] Parse `# Vector Clock: {...}` header on load
- [ ] `update_clock(clock: dict)` — merges and persists

**`cluster/gossip.py`**
- [ ] Maintain a `seen: set[str]` of `"{from}:{clock_hash}"` to deduplicate
- [ ] Reject gossip messages already in `seen`
- [ ] Attach current clock to all outgoing messages

**`node/server.py`**
- [ ] Tick clock on every write and DDL before gossiping
- [ ] Include clock in all gossip messages and in `/query` response envelope

### Done when

A gossip message delivered twice is applied only once. Send the same write gossip
payload to `/gossip` twice; the second application has no effect on the memory doc.

---

## Phase 6 — VIBE_CHECK Consistency Mode

**Goal**: `SELECT` with `consistency: "vibe_check"` asks all nodes and returns the
majority answer. Disagreements increment a conflict counter.

### Files to create/modify

```
cluster/gossip.py       # broadcast_read(): fan out query to all peers, collect responses
node/server.py          # VIBE_CHECK path in POST /query
```

### Tasks

**`cluster/gossip.py`**
- [ ] `broadcast_read(sql: str, timeout_ms: int) -> list[QueryResponse]` — fan out
      POST `/query?internal=true` to all peers concurrently, collect responses within
      timeout, ignore non-responders (Timeout-Based Exclusion)
- [ ] Add `internal=true` query param so peers skip gossip on internal reads

**`node/server.py`**
- [ ] If `consistency == "vibe_check"`:
  - Ask self + all peers for their answer
  - Group responses by their `rows` content (JSON-normalised)
  - Majority group wins (>= `floor(n/2)+1`)
  - If no majority: return `VIBE_ERROR` with all responses attached
  - If majority but minority exists: include `vibe_error.type = "CONFLICT"` with
    minority responses in the envelope; increment conflict counter
- [ ] Expose conflict counter in `/status` as `conflict_count`

### Done when

Two nodes disagree on a row. `SELECT` with `vibe_check` returns the majority answer
and the response includes a `CONFLICT` vibe_error with the minority response.

---

## Phase 7 — Argument Protocol (Conflict Resolution)

**Goal**: When `VIBE_CHECK` detects a conflict, the Argument Protocol fires. The
Arbiter LLM picks a winner. The losing node receives a `correction` gossip.

### Files to create/modify

```
cluster/gossip.py       # post_gossip() already exists; add arbiter selection
node/server.py          # argument_protocol(): call explain on disagreeing nodes,
                        #   call arbiter, send correction
node/llm_engine.py      # explain task type, arbiter task type
```

### Tasks

**`node/server.py`**
- [ ] `argument_protocol(sql, majority_rows, minority_responses) -> dict`:
  1. Call `EXPLAIN VIBE` on each disagreeing node (POST `/query` with
     `{"sql": "EXPLAIN VIBE <original_sql>", "internal": true}`)
  2. Pick Arbiter (round-robin from peers, excluding disagreeing nodes)
  3. POST to Arbiter with both explanations as a special `arbitrate` task
  4. Arbiter returns winning answer
  5. Send `correction` gossip to losing nodes
  6. Return winning result to client with `resolution: "argument_protocol"` in
     `vibe_error`

**`node/llm_engine.py`**
- [ ] `build_explain_task(sql: str) -> str`
- [ ] `build_arbitrate_task(sql, explanation_a, explanation_b) -> str` — produces
      a prompt asking the LLM to pick a winner and justify it

**`node/server.py` — `/gossip` handler**
- [ ] `correction` type: call LLM with a `correction` task, apply
      `updated_table_section` to memory doc, save

### Done when

Two nodes disagree. `VIBE_CHECK` triggers the Argument Protocol. One node receives
a `correction`. Subsequent `VIBE_CHECK` on the same query returns a clean result
with no conflict.

---

## Phase 8 — Compaction

**Goal**: When a node's memory doc exceeds the threshold, it compacts. Compaction
is gossiped to peers. Manual `COMPACT MEMORY ON <node>` works via the CLI.

### Files to create/modify

```
node/memory.py          # threshold check, backup pre-compact file
node/llm_engine.py      # compaction task type
node/server.py          # trigger compaction after writes; handle COMPACT MEMORY command
cluster/gossip.py       # broadcast_compaction()
```

### Tasks

**`node/memory.py`**
- [ ] `needs_compaction(threshold_pct: float, budget_tokens: int) -> bool`
- [ ] `backup_pre_compact()` — copy current file to `data/{node_id}.pre-compact.md`
- [ ] `increment_compaction_count()`

**`node/server.py`**
- [ ] After every write: if `memory.needs_compaction()`, run compaction:
  1. `memory.backup_pre_compact()`
  2. Call LLM with compaction task (optionally `AGGRESSIVE` flag)
  3. For each section in `compacted_sections`: call `memory.update_table_section()`
  4. `memory.increment_compaction_count(); memory.save()`
  5. `gossip.broadcast_compaction()`
- [ ] In `POST /gossip`: `compaction` type logs the event, no memory change on peers
      (they only receive a notification, not the compacted doc itself)
- [ ] Parse `COMPACT MEMORY ON <node>` and `COMPACT MEMORY ON <node> AGGRESSIVE` as
      vibeqlite custom commands — forward to the target node's `/compact` endpoint

**`node/server.py`**
- [ ] `POST /compact` — internal endpoint accepting `{"aggressive": bool}`, triggers
      compaction on this node

### Done when

Insert 30+ rows until threshold is crossed. Compaction fires automatically.
`GET /status` shows `compaction_count: 1` and `context_usage_pct` has dropped.
`mem.pre-compact.md` exists. The table section in the memory doc is now summarised.

---

## Phase 9 — Node Personalities

**Goal**: Personality is loaded from `cluster.yaml` and injected into the system
prompt. Different nodes answer the same query differently.

### Files to create/modify

```
node/llm_engine.py      # _load_personality_block()
node/prompts/system.md  # ✅ personality blocks already defined
cluster.yaml.example    # personality field already defined
```

### Tasks

- [ ] `_load_personality_block(personality: str) -> str` — reads the named block
      from the `## Personality Inserts` section of `system.md`
- [ ] Inject into `{PERSONALITY}` placeholder in system prompt
- [ ] Add `personality` to `/status` response
- [ ] Verify `confident` node never returns `confidence: "low"`
- [ ] Verify `paranoid` node always sets `confidence: "low"` for partial data

### Done when

Three nodes with different personalities answer the same SELECT differently.
`SELECT * FROM vibe_nodes` shows each node's personality alongside its status.

---

## Phase 10 — Fun SQL Extensions

**Goal**: All the `vibeqlite`-specific commands work.

### Files to create/modify

```
node/server.py          # command parser: detect non-standard SQL before sqlglot
cli/vibeqlite.py        # REPL with pretty-printed output
```

### Commands to implement

- [ ] `SELECT * FROM vibe_nodes` — fan out `GET /status` to all nodes, format as table
- [ ] `EXPLAIN VIBE <sql>` — run the sql, then call LLM with `explain` task type
- [ ] `SHOW CONFLICTS` — return `conflict_count` and last `UNRESOLVED_CONFLICT` log
      entries from `/status`
- [ ] `SHOW SCHEMA` — return the `## Schema` section from memory doc
- [ ] `SHOW EVICTIONS` — return eviction log from memory doc or a dedicated log file
- [ ] `REFRESH MEMORY ON <node>` — POST to target `/refresh`, node reloads memory
      from disk
- [ ] `SELECT CONFIDENCE(*) FROM <table> WHERE id = <n>` — parse as confidence check,
      call LLM with `confidence` task
- [ ] `COMPACT MEMORY ON <node>` — POST to target `/compact`
- [ ] `COMPACT MEMORY ON <node> AGGRESSIVE` — POST to target `/compact` with
      `{"aggressive": true}`

### CLI (`cli/vibeqlite.py`)

- [ ] `readline`-based REPL loop
- [ ] Pretty-print tabular results
- [ ] Show `node_id`, `confidence`, and `vector_clock` in the result footer
- [ ] Show `X-Vibe-Confidence: low` as a visible warning

### Done when

All commands above return sensible output from the REPL. `SELECT * FROM vibe_nodes`
shows a live cluster health table.

---

## Phase 11 — CALL ELECTION

**Goal**: `CALL ELECTION` asks all nodes to make their case. The longest/most
compelling response wins. The winner is announced to the cluster.

### Files to create/modify

```
node/server.py          # POST /election — node returns its election case
node/llm_engine.py      # election task type ✅ already in system prompt
cluster/gossip.py       # broadcast_election(), announce_winner()
```

### Tasks

- [ ] `POST /election` — call LLM with `election` task, return `case` string
- [ ] In `POST /query` for `CALL ELECTION`:
  1. Broadcast election to all nodes, collect `case` responses
  2. Determine winner: node with the longest `case` string (ties: alphabetical by
     node id)
  3. Broadcast winner announcement as a gossip message of type `election_result`
  4. Return winner + all cases to client
- [ ] Add `is_leader` flag to `/status` (set by receiving `election_result` gossip)

### Done when

`CALL ELECTION` in the REPL prints each node's campaign speech and announces the
winner.

---

## Testing Strategy

Each phase should have at minimum:

| Phase | Test |
|---|---|
| 1 | Unit test: LLM response parsing handles malformed JSON gracefully |
| 1 | Integration test: INSERT then SELECT returns the correct row |
| 2 | Integration test: INSERT, restart, SELECT — data persists |
| 3 | Integration test: CREATE TABLE on node A appears in node B schema |
| 4 | Integration test: INSERT on node A, poll node B for 3s — row appears |
| 5 | Unit test: duplicate gossip message is rejected |
| 5 | Unit test: vector clock merge takes max of each component |
| 6 | Integration test: two nodes disagree, VIBE_CHECK returns majority + CONFLICT |
| 7 | Integration test: Argument Protocol resolves conflict, losing node updated |
| 8 | Integration test: token threshold crossed, compaction fires, schema preserved |
| 9 | Unit test: `confident` personality never returns `confidence: "low"` |

Run with: `pytest tests/ -v`

---

## Open Questions (Decide Before Starting Each Phase)

| Phase | Question |
|---|---|
| 1 | Which Ollama model to default to? (`llama3.2` is small and fast for local dev) |
| 1 | How to handle LLM timeout? (Suggest: 30s hard limit, return `VIBE_ERROR`) |
| 3 | DDL gossip timeout — what if one peer is slow? (Suggest: 5s, proceed, flag `SCHEMA_DIVERGED`) |
| 6 | VIBE_CHECK peer timeout — 2s default? |
| 8 | What is the default `context_budget_tokens`? Depends on model. Make it a required `cluster.yaml` field. |
