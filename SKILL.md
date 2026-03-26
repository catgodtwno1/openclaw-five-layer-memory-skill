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
- `--memos-url http://10.10.20.178:8765` — point to Scott#1 MemOS
- `--cognee-url http://10.10.20.178:8000` — point to Scott#1 Cognee

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

## Configuring Endpoints

URLs resolve in this order (highest priority first):

1. **CLI flags:** `--memos-url` / `--cognee-url`
2. **Environment variables:** `MEMOS_URL` / `COGNEE_URL`
3. **Default:** `http://127.0.0.1:8765` (MemOS) / `http://127.0.0.1:8000` (Cognee)

### Per-machine setup (recommended)

Add to `~/.zshrc` or `~/.bashrc` on each machine:

```bash
# Scott#1 (local services)
# No env needed — defaults to 127.0.0.1

# Scott#3 / Scott#4 (remote, pointing to Scott#1)
export MEMOS_URL=http://10.10.20.178:8765
export COGNEE_URL=http://10.10.20.178:8000

# NAS endpoints (if using NAS as backend)
# export MEMOS_URL=http://10.10.10.66:8765
# export COGNEE_URL=http://10.10.10.66:8766
```

This way `python3 scripts/memory-5a-bench.py` just works on any machine without flags.

## Setting Up Cron Monitoring

Create an OpenClaw cron job to run the monitor every hour:

```
/cron add --every 60m --label "memory-health" -- bash ~/.openclaw/workspace/skills/ops-five-layer-memory/scripts/memory-5a-monitor.sh
```

The monitor script outputs alerts only on failure — cron delivers them to your configured channel.

## Interpreting Results

**Healthy baseline (Scott#1 local):**
- L1: ~10ms (local SQLite)
- L2: ~7ms write, ~5ms recall (local LanceDB)
- L3: ~60ms login, ~95ms search (local Cognee)
- L3.5: ~55ms search, ~60ms add (local MemOS)
- L5: < 1ms (local filesystem)

**Healthy baseline (remote from Scott#3):**
- L3: ~60ms login, ~100ms search (network hop to Scott#1)
- L3.5: ~60ms search, ~220ms add (network hop to Scott#1)

**Common failures:**
- L3 health fails → Cognee container down (`docker restart oc-cognee-api`)
- L3 search 401 → wrong Cognee instance (check `--cognee-url` / `COGNEE_URL`)
- L3.5 search fails → MemOS container or Neo4j down
- L1 count = 0 → LCM never ran compaction (check `summaryModel` config)
- L2 no .lance files → LanceDB Pro plugin not enabled

## Known Issues

- **L2 filesystem scan latency spike:** `L2/files` test runs `find ~/.openclaw/ -name '*.lance'` which can hit 3s+ on cold cache. Actual LanceDB write latency is ~7ms. This is a test artifact, not a functional issue.
- **Cognee login+search combined:** These two operations share a single function (`do_login_and_search()`) to avoid Python closure bugs with the auth token. Login and search latencies are still reported separately.
- **NAS vs local Cognee auth:** NAS Cognee (10.10.10.66:8766) and local Cognee (10.10.20.178:8000) are separate instances with independent auth. Login tokens are NOT interchangeable.
