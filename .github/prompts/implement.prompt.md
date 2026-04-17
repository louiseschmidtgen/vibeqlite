---
mode: agent
description: Implement vibeqlite phase by phase
---

# vibeqlite Implementation

You are implementing vibeqlite — a distributed database where every node is an LLM
holding data in its context window. Nodes gossip to achieve eventual maybe
consistency using the NOODLE consensus algorithm.

## Required reading — load these files before writing any code

- [ARCHITECTURE.md](../../ARCHITECTURE.md) — full system design and all message schemas
- [IMPL_PLAN.md](../../IMPL_PLAN.md) — phased build order with file-level task lists
- [PLAN.md](../../PLAN.md) — pre-implementation decisions (API contract, cluster.yaml format, token strategy)
- [node/prompts/system.md](../../node/prompts/system.md) — the LLM system prompt (already written)

## Stack

- Python 3.11+
- FastAPI + uvicorn (HTTP server)
- httpx async (gossip client)
- sqlglot (SQL classification — no custom parser)
- tiktoken (token counting, fallback `len(text) // 4`)
- Ollama REST API (LLM backend, abstracted behind `LLMClient`)
- PyYAML (config)
- pytest + pytest-asyncio (tests)

## Project layout

```
node/
  server.py           # FastAPI app: /query /gossip /status /compact /refresh /election
  llm_engine.py       # LLMClient: prompt building, Ollama calls, response parsing
  memory.py           # MemoryDoc: file-backed markdown memory document
  prompts/
    system.md         # system prompt — do not modify
cluster/
  gossip.py           # GossipClient: write/ddl/compaction broadcast
  registry.py         # NodeRegistry: parses cluster.yaml
  clock.py            # VectorClock
cli/
  vibeqlite.py        # readline REPL
cluster.yaml.example  # example config
pyproject.toml        # dependencies
tests/
  test_memory.py
  test_llm_engine.py
  test_server.py
  test_gossip.py
  test_clock.py
```

## Memory document format

Every node's memory is a markdown file. This exact format must be maintained:

```markdown
# Node: {node_id}
# Vector Clock: {"saturn": 5, "pluton": 3}
# Last Updated: 2026-04-17T12:00:00Z
# Compaction Count: 0

## Schema
- users: id INTEGER, name TEXT, age INTEGER, city TEXT

## Table: users
| id | name  | age | city   |
|----|-------|-----|--------|
| 1  | Alice | 30  | Berlin |
```

`## Schema` and `## Table: <name>` are the two section types. Section replacement
uses a regex header match — the section runs from its `##` header to the next `##`
header or end of file.

## LLM interaction rules

- Every LLM call gets the full system prompt (with `{NODE_ID}`, `{PERSONALITY}`,
  `{MEMORY_DOC}`, `{TASK}` substituted) plus a single user message containing the
  task string.
- LLM must return raw JSON. Strip any markdown fences before `json.loads()`.
- If `json.loads()` fails or `task_type` is missing, raise `LLMParseError` and
  return a `VIBE_ERROR` to the client. Do not retry.
- Never call the LLM for schema updates — `ddl_change` gossip applies `schema_snapshot`
  literally with no LLM involvement.

## Gossip message types

All messages: `{"from": str, "type": str, "vector_clock": dict, ...}`

| type | extra fields | applied how |
|---|---|---|
| `write` | `statement`, `summary` | LLM gossip_merge task |
| `ddl_change` | `statement`, `schema_snapshot` | literal schema section replace |
| `compaction` | `compaction_count`, `summary` | log only, no memory change |
| `correction` | `table`, `row_id`, `correct_value`, `reason` | LLM correction task |
| `election_result` | `winner` | set `is_leader` flag |

## API response envelope

```json
{
  "node_id": "saturn",
  "vector_clock": {},
  "compaction_count": 0,
  "confidence": "high",
  "rows": [],
  "affected_rows": null,
  "headers": {},
  "vibe_error": null
}
```

`vibe_error` types: `CONFLICT`, `VIBE_ERROR`, `LLM_REFUSED`, `LLM_PARSE_ERROR`

## cluster.yaml format

```yaml
context_budget_tokens: 8192
compaction_threshold_pct: 80
gossip_timeout_ms: 2000
ddl_gossip_timeout_ms: 5000

nodes:
  - id: saturn
    url: http://localhost:8001
    personality: default
  - id: pluton
    url: http://localhost:8002
    personality: confident
```

Each node is told its own `id` via `--node-id` CLI arg at startup. Validated against
`cluster.yaml`.

## Token counting

```python
def token_estimate(text: str, model: str = "gpt-3.5-turbo") -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return len(text) // 4
```

## Coding standards

- Type-annotate all public functions
- Raise specific exceptions (`LLMParseError`, `GossipError`) — no bare `Exception`
- Use `async def` for all I/O (Ollama calls, gossip HTTP requests)
- `memory.save()` is atomic: write to `{path}.tmp` then `os.replace()`
- All gossip POST failures are logged and swallowed for write gossip; DDL gossip
  failures are surfaced to the client
- No global mutable state outside of `MemoryDoc` and `VectorClock` instances on
  the app object

## Build order

Implement phases in order. Do not start phase N+1 until phase N passes its "done
when" test. Each phase has a concrete acceptance test in IMPL_PLAN.md.

Phase 0 → scaffold  
Phase 1 → single node query loop  
Phase 2 → persistence  
Phase 3 → DDL gossip  
Phase 4 → write gossip  
Phase 5 → vector clocks  
Phase 6 → VIBE_CHECK  
Phase 7 → Argument Protocol  
Phase 8 → compaction  
Phase 9 → personalities  
Phase 10 → fun SQL extensions + REPL  
Phase 11 → CALL ELECTION  

## Start here

Ask: "Which phase?" Then implement only that phase. Show the files changed, the
acceptance test from IMPL_PLAN.md, and confirm it passes before stopping.
