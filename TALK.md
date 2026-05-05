# vibeqlite: A Distributed Database of Non-Deterministic Value

**Lightning Talk — 5 minutes**
**Audience**: ~1000 engineers

---

## Slide 1 — Hook + Title (0:20)

**vibeqlite**
*A distributed database of non-deterministic value*

> "I built a distributed database. Three nodes, real SQL, gossip-based replication,
> conflict resolution, and a consensus algorithm.
>
> The storage engine is a large language model.
>
> This was a terrible idea. Let me tell you everything I learned from it."

---

## Slide 2 — LLMs Are Stateless (0:40)

An LLM is not a thinking machine. It is a **next-token predictor**.

```
f("The capital of France is") → "Paris"
```

Stateless function. No memory between calls. Every call starts from zero.

So where does "memory" come from in every AI product you've ever used?

> **You paste it into the prompt.**

That's RAG. That's chat history. That's every AI "memory" feature you've paid for.
They all reduce to: *put the thing you want it to remember into the input.*

vibeqlite takes this completely literally and builds a database out of it.

---

## Slide 3 — Markdown as Context + Compaction (0:45)

Each vibeqlite node holds its entire database as a markdown file:

```markdown
# Node: saturn
# Vector Clock: {"saturn": 5, "pluton": 3, "neptune": 4}

## Schema
- users: id INTEGER, name TEXT, age INTEGER

## Table: users
| id | name  | age |
|----|-------|-----|
| 1  | Alice | 30  |
| 2  | Bob   | 25  |
```

Before every query: load this file, paste it into the LLM prompt, ask what it knows.
After every write: ask the LLM to return an updated table, write it back to disk.

Real databases have B-trees, write-ahead logs, buffer pools, and page caches.
vibeqlite has `memory.md`.

**Compaction:** when the file grows too large, the LLM rewrites it into a denser form.
This is lossy. Bob's age might become "approximately 30-ish."
Rows the node finds uninteresting may quietly vanish.

If compaction hasn't run yet and you hit the context window hard limit:
**Organic Eviction** — oldest rows dropped silently. No ceremony.

---

## Slide 4 — Vibing: How I Built vibeqlite With an Agent (0:50)

Agentic coding is not autocomplete. It is a **loop**.

```
Goal
 → LLM thinks
 → calls a tool  (write file / run tests / read output)
 → observes result
 → thinks again
 → repeat
```

Building vibeqlite looked like this:

```
"Implement the gossip protocol"
→ agent reads IMPL_PLAN.md          ← tool: read file
→ agent writes cluster/gossip.py    ← tool: write file
→ agent runs pytest                 ← tool: shell
→ 3 tests fail
→ agent reads the traceback         ← tool: read output
→ agent patches gossip.py           ← tool: write file
→ tests pass → agent commits        ← tool: git
→ next phase
```

The plan lived in `IMPL_PLAN.md` — 11 numbered phases, each with exact files and a
"done when" condition the agent could verify by running tests.
Without a written plan, agents wander. With one, they execute.

Tests were the error signal, not the safety net. The agent wrote them first.
Git commits were the only long-term memory that survived between sessions.

> "The agent wrote a distributed consensus algorithm.
> It also named it.
> Nobody stopped it."

---

## Slide 5 — The NOODLE Consensus Algorithm (0:30)

Real databases use Raft or Paxos — carefully engineered leader election, quorums,
two-phase commit, and a lot of academic citations.

vibeqlite uses **NOODLE**:
*Nodes Optimistically Offering Data with Loose Eventual-consistency*

> "Unlike Raft (a sturdy wooden raft), NOODLE is a pool noodle.
> You're still floating. Mostly."

| Mode           | Command                           | What happens                        |
|----------------|-----------------------------------|-------------------------------------|
| `YOLO`         | `SET CONSISTENCY = 'yolo'`        | Ask one node, trust it completely   |
| `VIBE CHECK`   | `SET CONSISTENCY = 'vibe_check'`  | Poll all nodes, return majority     |
| `GOSSIP STORM` | `SET CONSISTENCY = 'gossip_storm'`| Force immediate sync to everyone    |
| `AMNESIA SAFE` | `SET CONSISTENCY = 'amnesia_safe'`| Resend full memory every round      |

---

## Slide 6 — What vibeqlite Broke (On Purpose) (0:25)

vibeqlite fails in every way real databases solved, slowly, over decades:

| Problem          | Real database          | vibeqlite                                  |
|------------------|------------------------|--------------------------------------------|
| Durability       | Write-ahead log        | "pretty sure I wrote that down"            |
| Consistency      | Two-phase commit       | NOODLE (pool noodle, not raft)             |
| Memory limits    | Buffer pool management | Organic Eviction™                          |
| Data loss        | Synchronous replication| Nodes gossip, probably                     |
| Conflict resolve | Deterministic merge    | Majority vote among vibes                  |

> "The best way to understand why PostgreSQL is impressive
> is to try to replace it with vibes."

---

## Slide 7 — Demo (1:30)

*See DEMO.md for full setup and commands.*

**Step 1 — Insert data, watch gossip**
```sql
vibeqlite> INSERT INTO users (name, age) VALUES ('Alice', 30);
```
```
node: saturn | affected_rows: 1 | confidence: high
⟳  gossiping to neptune... ok
⟳  gossiping to pluton... ok
```

**Step 2 — Read in YOLO mode (single node, full trust)**
```sql
vibeqlite> SET CONSISTENCY = 'yolo';
vibeqlite> SELECT * FROM users;
```
```
+----+-------+-----+
| id | name  | age |
+----+-------+-----+
| 1  | Alice | 30  |
+----+-------+-----+
node: saturn | confidence: high
```

**Step 3 — Introduce a conflict (pre-patched neptune memory to age=31)**
```sql
vibeqlite> SET CONSISTENCY = 'vibe_check';
vibeqlite> SELECT * FROM users;
```
```
⚡ CONFLICT on column 'age':
   saturn  → 30  (confidence: high)
   pluton  → 30  (confidence: high)
   neptune → 31  (confidence: medium)
Majority: 30. Returning majority result.
⚠  X-Vibe-Confidence: low — verify with other nodes
```

**Step 4 — The punchline**
```sql
vibeqlite> EXPLAIN VIBE SELECT * FROM users;
```
```
node: neptune | "I have Alice in my memory with age 31. I am
               pretty confident about this. I acknowledge the
               other nodes may differ. That is their problem."
```

> "It's not a bug. The node has high self-confidence and low accuracy.
> It's a perfectly normal distributed system."

---

## Slide 8 — Close (0:10)

> "Agentic coding isn't magic and it isn't hype.
> It's a junior engineer who reads fast, never gets tired,
> has no long-term memory, and needs you to write the plan down.
>
> vibeqlite is what happens when you let it run a bit too free."

**github.com/louiseschmidtgen/vibeqlite**

*Distributed. Eventually consistent. Non-deterministically valuable.*

---

## Timing

| Slide | Content                          | Time  | Running |
|-------|----------------------------------|-------|---------|
| 1     | Hook + title                     | 0:20  | 0:20    |
| 2     | LLMs are stateless               | 0:40  | 1:00    |
| 3     | Markdown as context + compaction | 0:45  | 1:45    |
| 4     | Vibing — how it was built        | 0:50  | 2:35    |
| 5     | NOODLE consensus + modes         | 0:30  | 3:05    |
| 6     | What vibeqlite broke             | 0:25  | 3:30    |
| 7     | Demo                             | 1:30  | 5:00    |
| 8     | Close                            | 0:10  | 5:10    |
