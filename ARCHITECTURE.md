# vibeqlite Architecture

## Decision Record: Option C — Structured Memory + Typed Gossip

---

## Context

Replace every component of a traditional distributed relational database with one or more LLMs. Clients send standard-looking SQL. Each LLM "remembers" the data in its context window. Nodes gossip to achieve *eventual maybe consistency*.

The goal is fun, educational, and honest about LLM limitations.

---

## The Core Insight

In a real database the pipeline is:

```
SQL → parser → query planner → executor → storage engine → disk
```

In vibeqlite:

```
SQL → LLM (does everything, including making stuff up)
```

Each node is a running LLM conversation. Its **context window is its storage**. When the context window fills up, the node gets memory pressure and starts forgetting rows. This is not a bug. It is a feature we call **Organic Eviction**.

---

## Why Not Options A or B

**Option A — Single Oracle**: One LLM, one memory file. Easy to build but no distribution, no gossip, no fun.

**Option B — Raw Gossip**: Nodes broadcast free-form English to each other. Maximally chaotic. Nodes diverge immediately and there is no way to observe or debug anything. Fun for five minutes, then unusable.

**Option C (this document)** gives you real observable state (memory docs you can diff), structured propagation (gossip you can log and replay), and a foundation for all the fun extensions.

---

## Component Layout

```
vibeqlite/
├── node/
│   ├── server.py           # HTTP API: /query  /gossip  /status
│   ├── llm_engine.py       # LLM wrapper: manages system prompt + conversation thread
│   ├── memory.py           # Read/write the node's memory document
│   └── prompts/
│       └── system.md       # Node personality + role instructions
├── cluster/
│   ├── gossip.py           # Broadcast typed write messages to peers
│   ├── registry.py         # Node discovery (static config or simple DNS)
│   └── clock.py            # Vector clock implementation
└── cli/
    └── vibeqlite.py        # sqlite3-style REPL
```

---

## Node Internals

Each node owns two things:

### 1. Memory Document

A structured markdown file that is the node's persistent (ish) state. Updated after every write. The LLM reads this at the start of every query.

```markdown
# Node: saturn
# Vector Clock: {"saturn": 5, "pluton": 3, "neptune": 4}
# Last Updated: 2026-04-17T12:00:00Z

## Table: users
| id | name  | age | city   |
|----|-------|-----|--------|
| 1  | Alice | 30  | Berlin |
| 2  | Bob   | 25  | London |

## Table: orders
(empty)
```

### 2. LLM Conversation Thread

Used to:
- Parse SQL intent (with `sqlglot` doing the heavy lifting so we don't need to prompt-engineer a parser)
- Formulate query answers from the memory document
- Merge incoming gossip into the memory document
- Reason about conflicts

The system prompt gives each node a personality (see Node Personalities below).

---

## Protocol: Write Path

```
Client  →  POST /query  →  Node saturn
                              │
                              ├─ sqlglot: classify as WRITE
                              ├─ LLM: confirm intent, check constraints (vibes)
                              ├─ memory.py: update memory document
                              ├─ Return OK to client
                              │
                              └─ gossip.py: async broadcast to pluton, neptune
                                    │
                              POST /gossip  →  Node pluton
                                              │
                                              ├─ LLM: read gossip message
                                              └─ memory.py: merge into memory doc

```

### Gossip Message Format

Typed, versioned, replayable:

```json
{
  "from": "saturn",
  "type": "write",
  "vector_clock": {"saturn": 5, "pluton": 3, "neptune": 4},
  "statement": "INSERT INTO users (name, age) VALUES ('Alice', 30)",
  "summary": "users table: added row id=1 name=Alice age=30"
}
```

The `summary` field is written by the sending LLM in natural language. The receiving LLM uses this (not only the raw SQL) to update its memory document. This is intentional — it lets nodes have subtly different interpretations. Chaos is part of the design.

---

## Protocol: Read Path

```
Client  →  POST /query  →  Node saturn
                              │
                              ├─ sqlglot: classify as READ
                              ├─ LLM: read memory document, formulate answer
                              └─ Return result (possibly stale, possibly confident)
```

The node returns its local view. It does not contact peers unless the consistency mode requires it (see NOODLE below).

---

## NOODLE Consensus

**NOODLE** — *Nodes Optimistically Offering Data with Loose Eventual-consistency*

Raft is a sturdy wooden raft: reliable, load-bearing, safe. NOODLE is a pool noodle. You are still floating. Mostly.

### Why NOODLE instead of Raft

| Property | Raft | NOODLE |
|---|---|---|
| Leader election | Strict majority vote, stable terms | Any node can feel like a leader |
| Log replication | Append-only WAL, deterministic | Gossip + natural language summaries |
| Commit ordering | Guaranteed | Optimistic, timestamp-ish |
| Conflict detection | Prevented by design | Detected after the fact, argued about |
| Split-brain recovery | Quorum prevents it | Largest memory doc wins |
| Failure model | Crash-fault tolerant | Vibe-fault tolerant |

### Leadership by Vibes

NOODLE has no formal leader election. Any node can assert leadership by responding to a `CALL ELECTION` command. The winner is determined by which node's LLM makes the most convincing case. Ties are broken by response length (longer = more confident, obviously).

### Soft Quorum

Writes are acknowledged immediately to the client. Gossip propagates asynchronously. A read in `VIBE CHECK` mode asks all reachable nodes and returns the majority answer. The quorum threshold is:

```
floor(n_nodes / 2) + 1   # where n_nodes is however many answer before the timeout
```

Nodes that are slow are simply not consulted. This is called **Timeout-Based Exclusion** and is absolutely a real distributed systems term now.

### Conflict Resolution: The Argument Protocol

When a `VIBE CHECK` read detects disagreement between nodes:

1. Each disagreeing node is asked to explain its answer (one LLM call each)
2. The explanations are sent to a **neutral third node** (the Arbiter, rotates round-robin)
3. The Arbiter's LLM reads both arguments and picks a winner
4. The losing node is sent a `correction` gossip message
5. If the Arbiter itself is one of the disagreeing nodes, it recuses and the next node in rotation is used

This is called **The Argument Protocol**. It is O(3) LLM calls per conflict, which is fine.

### Split-Brain Recovery

When a partitioned node rejoins:

1. It sends a `HELLO` message with its current vector clock
2. Peers respond with their vector clocks
3. Any node whose vector clock is strictly ahead sends a full `SYNC` gossip (its entire memory document)
4. The rejoining node's LLM merges the sync message into its own memory
5. If the merge is ambiguous the node enters `CONFUSED` state and returns a warning header on subsequent reads: `X-Vibe-Confidence: low`

---

## Consistency Modes

Set per-session: `SET CONSISTENCY = 'mode'`

| Mode | Behaviour | Real analogy |
|---|---|---|
| `YOLO` (default) | Read from local node, no peer contact | Eventual consistency, optimistic |
| `VIBE_CHECK` | Ask all nodes, return majority via Argument Protocol | Quorum read |
| `GOSSIP_STORM` | Write + force immediate synchronous gossip to all peers | Synchronous replication |
| `AMNESIA_SAFE` | Re-send full memory document on every gossip message | Anti-entropy repair |

---

## Node Personalities

Each node is started with a personality in its system prompt. This changes *how* it answers, not what it knows. Personalities are optional but highly recommended.

```yaml
# node config
personality: "paranoid"   # options: default, confident, paranoid, lazy, pedantic, chaotic
```

| Personality | Behaviour |
|---|---|
| `default` | Answers plainly. Admits uncertainty when asked. |
| `confident` | Never admits uncertainty. May invent data with high conviction. |
| `paranoid` | Adds `X-Vibe-Confidence: low` to every response. Asks clarifying questions. |
| `lazy` | Returns cached answer if it "seems close enough." Gossip delivery is best-effort. |
| `pedantic` | Rejects syntactically imperfect SQL. Adds unsolicited schema advice. |
| `chaotic` | Random personality per request. Do not use in production (there is no production). |

---

## Fun SQL Extensions

```sql
-- Cluster health + each node's current mood
SELECT * FROM vibe_nodes;

-- Node explains its reasoning for a query
EXPLAIN VIBE SELECT * FROM users WHERE name = 'Alice';

-- Show unresolved conflicts between nodes
SHOW CONFLICTS;

-- Trigger an election (winner by most convincing argument)
CALL ELECTION;

-- Force a node to re-read its memory document from scratch
REFRESH MEMORY ON saturn;

-- Ask a node how confident it is about a specific row
SELECT CONFIDENCE(*) FROM users WHERE id = 1;
```

---

## Failure Modes

These are features.

| Failure | What actually happens |
|---|---|
| **Context window full** | Node enters Organic Eviction. Oldest INSERTs forgotten first. `SHOW EVICTIONS` lists what was lost. |
| **Hallucination split-brain** | Two nodes invent different values for the same key. Detected on next `VIBE_CHECK`. Resolved by Argument Protocol. |
| **Gossip loop** | A node re-gossips a message it received. Prevented by including `from` + vector clock; receivers deduplicate. |
| **LLM refusal** | Node responds with an error: `ERROR: node saturn declined to store this data (ethical concern)`. The row is not inserted. Other nodes are not gossiped. |
| **Confident wrongness** | `SELECT` returns plausible but fabricated data with no error. This is the default operating mode. |
| **Arbiter unavailable** | Conflict falls back to `YOLO` resolution (first response wins). Logged as `UNRESOLVED_CONFLICT`. |
| **All nodes disagree** | Returned as a `VIBE_ERROR` with all responses attached. Client decides. |

---

## Implementation Notes

- **SQL parsing**: Use [`sqlglot`](https://github.com/tobymao/sqlglot) to classify intent (READ / WRITE / DDL / UNKNOWN). Do not write a SQL parser. Let the LLM handle interpretation.
- **LLM backend**: Design for swappable backends. Start with [Ollama](https://ollama.com) locally (free, private). Abstract behind a `LLMClient` interface so OpenAI / Anthropic / etc. can slot in.
- **Memory document format**: Markdown tables for human readability when debugging. The LLM can read markdown natively.
- **Vector clocks**: Lightweight dict `{node_id: int}`. Increment the local counter on every write. Merge by taking the max of each component.
- **Transport**: Simple HTTP with JSON bodies. No gRPC, no message bus. Nodes are HTTP servers.
- **Node discovery**: Static config file for now (`cluster.yaml` listing peers). DNS-based discovery is a later problem.

---

## Build Order (Recommended)

1. Single node, single memory doc, single LLM call per query — no gossip
2. Persistent memory document survives process restart
3. Second node + async gossip on writes
4. Vector clocks + deduplication
5. `VIBE_CHECK` consistency mode (multi-node reads)
6. Argument Protocol for conflict resolution
7. Node personalities
8. Fun SQL extensions (`EXPLAIN VIBE`, `SHOW CONFLICTS`, etc.)
9. `CALL ELECTION`

---

## What We Are Not Building

- A WAL
- Snapshotting  
- Backpressure
- Authentication (nodes trust each other unconditionally, like a family)
- Durability guarantees of any kind
