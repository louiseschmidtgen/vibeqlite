# vibeqlite

> A distributed database where the storage engine is vibes.

**vibeqlite** replaces every component of a traditional distributed database with LLMs. Each node is a large language model holding your data in its context window. Nodes gossip to achieve *eventual maybe consistency* using the **NOODLE** consensus algorithm.

Send standard-ish SQL. Get answers back. They might even be correct.

---

## How it works

```
Client: INSERT INTO users (name, age) VALUES ('Alice', 30)
  → Node pluton: understood! updating my memory...
  → Node pluton: *whispers to saturn and neptune* "hey I just heard about Alice"
  → Node saturn: got it, probably
  → Node neptune: Alice? oh yes, definitely heard of her

Client: SELECT * FROM users WHERE name = 'Alice'
  → Node saturn: Alice, 30, Berlin (confident)
  → Node neptune: Alice, 31, Berlin (pretty sure)
  → vibeqlite: CONFLICT detected. 2/3 nodes agree on age 30. Returning majority.
```

---

## Consensus

vibeqlite uses **NOODLE** — *Nodes Optimistically Offering Data with Loose Eventual-consistency*.

Unlike Raft (a sturdy wooden raft), NOODLE is a pool noodle. You're still floating. Mostly.

| Mode | Command | Behaviour |
|---|---|---|
| `YOLO` | `SET CONSISTENCY = 'yolo'` | Ask one node, trust it completely |
| `VIBE CHECK` | `SET CONSISTENCY = 'vibe_check'` | Ask all nodes, return majority answer |
| `GOSSIP STORM` | `SET CONSISTENCY = 'gossip_storm'` | Force immediate sync to all nodes |
| `AMNESIA SAFE` | `SET CONSISTENCY = 'amnesia_safe'` | Re-send full memory on every gossip |

---

## Fun SQL extensions

```sql
-- Check cluster health and node mood
SELECT * FROM vibe_nodes;

-- Ask a node to explain its reasoning
EXPLAIN VIBE SELECT * FROM users;

-- See what nodes disagree about
SHOW CONFLICTS;

-- Demand an election (winner decided by vibes, not Raft)
CALL ELECTION;
```

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, component breakdown, and honest list of failure modes.

---

## Status

Pre-alpha. The nodes are still figuring themselves out.
