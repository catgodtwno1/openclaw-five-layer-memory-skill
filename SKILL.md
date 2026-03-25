---
name: ops-five-layer-memory
description: "Five-layer memory stack testing, benchmarking, and monitoring for OpenClaw. Use when: (1) running a full 5-layer health check or benchmark, (2) diagnosing which memory layer is failing, (3) setting up cron monitoring for memory health, (4) comparing memory performance across machines. Triggers on: 五层记忆, five-layer memory, memory benchmark, 记忆测试, L1-L5 check, memory health."
---

# Five-Layer Memory Stack Testing

Test, benchmark, and monitor all 5 memory layers in OpenClaw.

## Layer Architecture

| Layer | Name | Backend | What It Stores |
|-------|------|---------|----------------|
| L1 | LCM | SQLite (`~/.openclaw/lcm.db`) | Conversation summaries (DAG) |
| L2 | LanceDB Pro | Lance files (`~/.openclaw/`) | Semantic memory (vector store) |
| L3 | Cognee Sidecar | Docker (LanceDB + Graph DB) | Knowledge graph + chunks |
| L3.5 | MemOS | Docker (Neo4j + Qdrant) | Structured memory objects |
| L5 | Daily Files | Filesystem (`workspace/memory/`) | Raw daily notes |

## Quick Commands

### Full Benchmark (50 rounds default)

```bash
python3 scripts/memory-5a-bench.py
```

Options:
- `python3 scripts/memory-5a-bench.py 100` — 100 rounds
- `--memos-url http://10.10.10.66:8765` — point to NAS MemOS
- `--cognee-url http://10.10.10.66:8766` — point to NAS Cognee

Output: per-layer pass/fail, avg/P50/P95/P99 latency, CSV at `/tmp/memory-5a-bench.csv`

### Quick Health Check (monitoring)

```bash
bash scripts/memory-5a-monitor.sh
```

Exit 0 = all OK, exit 1 = failures (prints alert message for cron pickup).

## Test Points Per Round (17 total)

| Layer | Tests | What's Checked |
|-------|-------|----------------|
| L1 | count, content, fts, models, parents | SQLite row counts, FTS index, model diversity, DAG parents |
| L2 | files, write, recall | Lance file existence, store API, recall API |
| L3 | health, login, search | Cognee auth endpoint, token auth, CHUNKS search |
| L3.5 | search, add | MemOS search + add APIs |
| L5 | dir, list, write, read | Directory exists, .md files present, write+read roundtrip |

## Setting Up Cron Monitoring

Create an OpenClaw cron job to run the monitor every hour:

```
/cron add --every 60m --label "memory-health" -- bash ~/.openclaw/workspace/skills/ops-five-layer-memory/scripts/memory-5a-monitor.sh
```

The monitor script outputs alerts only on failure — cron delivers them to your configured channel.

## Interpreting Results

**Healthy baseline (NAS endpoints):**
- L1: < 5ms (local SQLite)
- L2: < 50ms (local LanceDB)
- L3: ~300-400ms avg (NAS Cognee)
- L3.5: ~200-500ms avg (NAS MemOS)
- L5: < 1ms (local filesystem)

**Common failures:**
- L3 health fails → Cognee container down (`docker restart oc-cognee-api` on NAS)
- L3.5 search fails → MemOS container or Neo4j down
- L1 count = 0 → LCM never ran compaction (check `summaryModel` config)
- L2 no .lance files → LanceDB Pro plugin not enabled

## Customizing Endpoints

Default endpoints point to NAS (10.10.10.66). For local testing:

```bash
python3 scripts/memory-5a-bench.py --memos-url http://127.0.0.1:8765 --cognee-url http://127.0.0.1:8000
```

The monitor script (`memory-5a-monitor.sh`) defaults to localhost. Edit the URLs inside if your services are remote.
