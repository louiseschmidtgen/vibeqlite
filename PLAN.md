# vibeqlite Pre-Implementation Plan

These tasks must be completed before writing any code. Each produces a concrete
artifact that unblocks one or more build steps in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Task 1 — Draft the Base System Prompt

**Blocks**: Build step 1 (single node query)  
**Artifact**: `node/prompts/system.md`

The system prompt is the most critical piece. Everything the LLM does — reading the
memory document, writing it back, interpreting SQL, merging gossip — depends on
clear instructions here. The base prompt must be designed and manually tested before
any code is written.

### The prompt must instruct the LLM to

- Read the `## Schema` section of the memory document to understand table structures
- Read `## Table:` sections to find data
- Respond to READ queries by consulting the memory document and returning results in a structured format
- Respond to WRITE queries by confirming the change and outputting an updated memory document section
- Merge an incoming gossip `summary` into the relevant table section
- Run compaction by rewriting table sections in a denser form, never touching `## Schema`
- Behave according to its personality (injected at node startup)
- Output in a predictable, parseable format (see Task 2 for the response envelope)

### Suggested prompt structure

```
[ROLE]
You are a database node named {node_id}. Your memory document is your storage.
You answer SQL queries by reading your memory. You update your memory after writes.

[PERSONALITY]
{personality_instructions}

[MEMORY DOCUMENT]
{memory_doc}

[TASK]
{task}  ← injected per request: the SQL statement, gossip payload, or compaction trigger
```

### Testing checklist (manual, against Ollama)

- [ ] `SELECT * FROM users` against an empty memory → returns empty result, not an error
- [ ] `SELECT * FROM users` with 2 rows → returns correct rows
- [ ] `INSERT INTO users (name, age) VALUES ('Alice', 30)` → outputs updated table section
- [ ] Merge a `write` gossip summary → table section updated correctly
- [ ] Compaction of a 20-row table → schema section untouched, rows summarised
- [ ] `SELECT * FROM users WHERE name = 'Alice'` after compaction → still returns Alice (she was retained)

---

## Task 2 — Define the HTTP API Contract

**Blocks**: Build step 1 (server.py), Build step 3 (CLI)  
**Artifact**: `API.md`

Define request and response JSON schemas for all three endpoints before writing
`server.py` or `cli/vibeqlite.py`. This lets both sides be built independently.

### `POST /query`

**Request**
```json
{
  "sql": "SELECT * FROM users WHERE name = 'Alice'",
  "consistency": "yolo",
  "session_id": "optional-client-session-uuid"
}
```

**Response (success)**
```json
{
  "node_id": "saturn",
  "vector_clock": {"saturn": 5, "pluton": 3, "neptune": 4},
  "compaction_count": 2,
  "confidence": "high",
  "rows": [
    {"id": 1, "name": "Alice", "age": 30, "city": "Berlin"}
  ],
  "affected_rows": null,
  "headers": {}
}
```

**Response (write)**
```json
{
  "node_id": "saturn",
  "vector_clock": {"saturn": 6, "pluton": 3, "neptune": 4},
  "compaction_count": 2,
  "confidence": "high",
  "rows": null,
  "affected_rows": 1,
  "headers": {}
}
```

**Response (conflict — VIBE_CHECK mode)**
```json
{
  "node_id": "saturn",
  "vector_clock": {"saturn": 5, "pluton": 3, "neptune": 4},
  "compaction_count": 2,
  "confidence": "low",
  "rows": [{"id": 1, "name": "Alice", "age": 30, "city": "Berlin"}],
  "affected_rows": null,
  "headers": {"X-Vibe-Confidence": "low"},
  "vibe_error": {
    "type": "CONFLICT",
    "disagreeing_nodes": ["pluton"],
    "resolution": "majority",
    "minority_responses": [
      {"node": "pluton", "rows": [{"id": 1, "name": "Alice", "age": 31, "city": "Berlin"}]}
    ]
  }
}
```

**Response (unresolvable)**
```json
{
  "vibe_error": {
    "type": "VIBE_ERROR",
    "message": "all nodes disagree",
    "responses": [...]
  }
}
```

### `POST /gossip`

**Request**: any gossip message (see gossip message types in ARCHITECTURE.md)

**Response**
```json
{"status": "ok", "node_id": "saturn"}
```

On error:
```json
{"status": "error", "node_id": "saturn", "reason": "schema mismatch"}
```

### `GET /status`

**Response**
```json
{
  "node_id": "saturn",
  "personality": "paranoid",
  "vector_clock": {"saturn": 5, "pluton": 3, "neptune": 4},
  "compaction_count": 2,
  "memory_doc_size_chars": 4821,
  "memory_doc_token_estimate": 1205,
  "context_budget_tokens": 8192,
  "context_usage_pct": 14.7,
  "state": "ok",
  "peers": [
    {"node_id": "pluton", "url": "http://localhost:8002", "reachable": true},
    {"node_id": "neptune", "url": "http://localhost:8003", "reachable": false}
  ]
}
```

Possible `state` values: `ok`, `confused`, `schema_diverged`, `compacting`

---

## Task 3 — Define `cluster.yaml`

**Blocks**: Build step 4 (second node + gossip)  
**Artifact**: `cluster.yaml` (example / schema)

The registry needs a concrete format before `cluster/registry.py` can be written.

```yaml
# cluster.yaml — vibeqlite cluster configuration

context_budget_tokens: 8192      # total context window of the LLM backend
compaction_threshold_pct: 80     # trigger compaction at this % of context budget
gossip_timeout_ms: 2000          # how long to wait for a gossip peer before giving up
ddl_gossip_timeout_ms: 5000      # DDL gossip is synchronous; longer timeout

nodes:
  - id: saturn
    url: http://localhost:8001
    personality: default

  - id: pluton
    url: http://localhost:8002
    personality: confident

  - id: neptune
    url: http://localhost:8003
    personality: paranoid
```

### Decisions to lock down

- [ ] Is `context_budget_tokens` per-node or cluster-wide? (Recommend: per-node override with cluster default)
- [ ] How does a node know its own `id`? (Recommend: passed as a CLI arg at startup, validated against `cluster.yaml`)
- [ ] What happens if a node in `cluster.yaml` is not reachable at startup? (Recommend: log warning, continue — peers are optional)

---

## Task 4 — Compaction Token Measurement Strategy

**Blocks**: Build step 8 (compaction)  
**Artifact**: decision recorded in `Implementation Notes` of ARCHITECTURE.md + implementation in `memory.py`

The compaction threshold is "80% of context budget in tokens" — but counting tokens
requires a strategy. Options:

| Strategy | Accuracy | Cost | Dependency |
|---|---|---|---|
| `tiktoken` estimate | High (for OpenAI models) | ~0 (local) | `tiktoken` Python package |
| Character count heuristic | Low (~4 chars/token) | Zero | None |
| LLM API response `usage` field | Exact (for last request) | Zero (already returned) | None |
| Dedicated tokenizer per model | High | Moderate | Model-specific |

**Recommendation**: Use `tiktoken` as the default estimator (it's fast, local, and
accurate enough). Fall back to `len(text) // 4` if the model isn't in `tiktoken`'s
vocabulary (e.g. local Ollama models). Track running token estimate, not just chars.

### Decision checklist

- [ ] Confirm `tiktoken` covers the target models (it covers all OpenAI and many Ollama models via `cl100k_base`)
- [ ] Decide whether compaction threshold is checked after every write or only periodically
- [ ] Decide what happens if compaction itself produces output larger than the threshold (cap iterations at 3, then fall back to Organic Eviction)

---

## Summary

| Task | Artifact | Unblocks build step |
|---|---|---|
| 1. Base system prompt | `node/prompts/system.md` | 1 — single node query |
| 2. HTTP API contract | `API.md` | 1 (server), 3 (CLI) |
| 3. `cluster.yaml` format | `cluster.yaml` example | 4 — second node + gossip |
| 4. Token measurement strategy | note in ARCHITECTURE.md | 8 — compaction |

DDL handling is already decided and documented in ARCHITECTURE.md (synchronous
`ddl_change` gossip with literal schema application). No further design work needed
before implementing build step 3.

Complete tasks in order. Task 1 is the most time-consuming because it requires
iterative manual testing against the LLM backend.
