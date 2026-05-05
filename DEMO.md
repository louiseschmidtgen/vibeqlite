# vibeqlite Demo Instructions

> Live demo for the lightning talk. Practice this at least twice before the talk.
> The demo is pre-scripted — every command and expected output is listed below.
> The only manual step is patching neptune's memory file before the show.

---

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) running locally with `llama3.2` pulled
- vibeqlite installed

```bash
# Verify Ollama is running and has the model
ollama list | grep llama3.2

# Pull if missing
ollama pull llama3.2
```

---

## Setup (do this before the talk)

### 1. Install

```bash
git clone https://github.com/louiseschmidtgen/vibeqlite
cd vibeqlite
pip install -e .
```

### 2. Configure

```bash
cp cluster.yaml.example cluster.yaml
mkdir -p data
```

The default `cluster.yaml` defines three nodes — saturn (8001), pluton (8002),
neptune (8003) — with personalities: default, confident, and paranoid.

### 3. Open four terminal windows

Label them clearly — you'll be switching between them on stage:

| Terminal | Purpose                     |
|----------|-----------------------------|
| T1       | Node: saturn  (port 8001)   |
| T2       | Node: pluton  (port 8002)   |
| T3       | Node: neptune (port 8003)   |
| T4       | vibeqlite REPL              |

### 4. Start all three nodes

**T1 — saturn:**
```bash
NODE_ID=saturn CONFIG_PATH=cluster.yaml uvicorn node.server:app --port 8001
```

**T2 — pluton:**
```bash
NODE_ID=pluton CONFIG_PATH=cluster.yaml uvicorn node.server:app --port 8002
```

**T3 — neptune:**
```bash
NODE_ID=neptune CONFIG_PATH=cluster.yaml uvicorn node.server:app --port 8003
```

Wait until all three print `Application startup complete.` before continuing.

### 5. Verify cluster health

```bash
curl -s http://localhost:8001/status | python -m json.tool
```

You should see saturn's status with its vector clock and an empty schema.

### 6. Pre-patch neptune's memory to create the conflict

This is the key setup step. After the INSERT in step 3 of the demo, neptune will
have synced Alice's age as 30. You need to manually change it to 31 in neptune's
memory file to create the conflict that powers slide 7.

**Do this right before going on stage (after a test run):**

```bash
# After running the INSERT once during your practice run:
sed -i 's/| 1  | Alice | 30  |/| 1  | Alice | 31  |/' data/neptune.md

# Verify the patch
grep Alice data/neptune.md
# Should show: | 1  | Alice | 31  |
```

---

## Demo Script (Slide 7)

Open the REPL in **T4**:

```bash
python -m cli.vibeqlite --url http://localhost:8001
```

You should see:
```
vibeqlite 🌊  connected to saturn (http://localhost:8001)
consistency: yolo
Type SQL or HELP. Ctrl-D to exit.
vibeqlite>
```

---

### Step 1 — Create a table and insert data

```sql
CREATE TABLE users (id INTEGER, name TEXT, age INTEGER);
```
```sql
INSERT INTO users (name, age) VALUES ('Alice', 30);
```

Expected output:
```
node: saturn | affected_rows: 1 | confidence: high
```

Point out that gossip is happening in the background — switch to T1 briefly to show
the uvicorn logs printing gossip requests to neptune and pluton.

---

### Step 2 — Read in YOLO mode

YOLO is the default — one node, total trust.

```sql
SELECT * FROM users;
```

Expected output:
```
+----+-------+-----+
| id | name  | age |
+----+-------+-----+
| 1  | Alice | 30  |
+----+-------+-----+
node: saturn | confidence: high
```

Say: *"One node, full trust. Fast. Probably fine."*

---

### Step 3 — Switch to VIBE CHECK and reveal the conflict

```sql
SET CONSISTENCY = 'vibe_check';
```
```sql
SELECT * FROM users;
```

Expected output (neptune is pre-patched to age=31):
```
⚡ CONFLICT on column 'age':
   saturn  → 30  (confidence: high)
   pluton  → 30  (confidence: high)
   neptune → 31  (confidence: medium)
Majority: 30. Returning majority result.
⚠  X-Vibe-Confidence: low — verify with other nodes
```

Say: *"Neptune is wrong. But we have majority. This is distributed systems working
as designed — except the node that's wrong is an LLM that made an editorial decision
about Alice's age."*

---

### Step 4 — Ask neptune to explain itself (the punchline)

```sql
EXPLAIN VIBE SELECT * FROM users;
```

Expected output (neptune's paranoid personality responding):
```
node: neptune | "I have Alice in my memory with age 31. I am
               pretty confident about this. I acknowledge the
               other nodes may differ. That is their problem."
```

Pause. Let it land.

Say: *"It's not a bug. The node has high self-confidence and low accuracy.
It's a perfectly normal distributed system."*

---

## Cleanup / Reset Between Practice Runs

```bash
# Stop all nodes (Ctrl-C in T1, T2, T3)

# Wipe all memory files to start fresh
rm -f data/saturn.md data/pluton.md data/neptune.md

# Re-patch neptune AFTER your first INSERT in the next practice run
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Node won't start | Check Ollama is running: `curl http://localhost:11434/api/tags` |
| Gossip fails silently | Check all three nodes are up before inserting |
| No conflict shown | Verify the patch: `grep Alice data/neptune.md` |
| LLM returns unexpected JSON | Model is hallucinating the response format — retry or use a larger model |
| Context too short error | Increase `context_budget_tokens` in `cluster.yaml` |

---

## Quick Reference — All Demo Commands

```sql
CREATE TABLE users (id INTEGER, name TEXT, age INTEGER);
INSERT INTO users (name, age) VALUES ('Alice', 30);
SELECT * FROM users;
SET CONSISTENCY = 'vibe_check';
SELECT * FROM users;
EXPLAIN VIBE SELECT * FROM users;
```

Bonus commands if you have time:
```sql
SELECT * FROM vibe_nodes;   -- cluster health + node moods
SHOW CONFLICTS;             -- all known disagreements
SHOW SCHEMA;                -- what the node thinks the schema is
```
