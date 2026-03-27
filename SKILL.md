---
name: ops-five-layer-memory
description: "Five-layer memory stack testing, benchmarking, and monitoring for OpenClaw. Use when: (1) running a full 5-layer health check or benchmark, (2) diagnosing which memory layer is failing, (3) setting up cron monitoring for memory health, (4) comparing memory performance across machines. Triggers on: 五层记忆, five-layer memory, memory benchmark, 记忆测试, L1-L5 check, memory health."
---

# Five-Layer Memory Stack Testing

Test, benchmark, and monitor all 5 memory layers in OpenClaw.

## Layer Architecture

| Layer | Name | Backend | LLM Provider | What It Stores |
|-------|------|---------|--------------|----------------|
| L1 | LCM | SQLite (`~/.openclaw/lcm.db`) | Claude Haiku-4-5 | Conversation summaries (DAG) |
| L2 | LanceDB Pro | Lance files (`~/.openclaw/`) | Qwen2.5-32B-Instruct (SiliconFlow, ¥7-8/月) | Semantic memory (vector + BM25) |
| L3 | Cognee Sidecar | Docker (LanceDB + Graph DB) | Qwen2.5-32B-Instruct (SiliconFlow, ¥7-8/月) | Knowledge graph + chunks |
| L3.5 | MemOS | Docker (Neo4j + Qdrant) | Qwen2.5-32B-Instruct (SiliconFlow, ¥7-8/月) | Structured memory objects |
| L5 | Daily Files | Filesystem (`workspace/memory/`) | None | Raw daily notes |

### LLM Provider Strategy (2026-03-26)

L2/L3/L3.5 all use **SiliconFlow Qwen2.5-32B-Instruct** (~¥7-8/月, 1000 RPM, ~1.5s latency). This avoids MiniMax 429 rate limiting caused by concurrent API calls across layers (L2+L3+L3.5 同時打 API 會超過 MiniMax C≥15 瞬時併發閾值). MiniMax M2.7-highspeed reserved for main session fallback and subagent conversations.

**Embedding**: SiliconFlow BAAI/bge-m3 (1024 dims) — shared across L2/L3/L3.5.

**MemOS Embedder**: Must set `MOS_EMBEDDER_BACKEND=universal_api` (default is `ollama`, will cause connection errors if Ollama not installed).

## Quick Commands

### Full Benchmark (50 rounds default)

```bash
python3 scripts/memory-5a-bench.py
```

Options:
- `python3 scripts/memory-5a-bench.py 300` — 300 rounds
- `python3 scripts/memory-5a-bench.py 5 --smart` — use LLM-generated diverse test data
- `--memos-url http://<MEMOS_HOST>:8765` — point to remote MemOS server
- `--cognee-url http://<COGNEE_HOST>:8000` — point to remote Cognee server

Output: per-layer pass/fail, avg/P50/P95/P99 latency, CSV at `/tmp/memory-5a-bench.csv`

### Smart Data Mode

`--smart` generates diverse test memories (conversations, preferences, decisions) via MiniMax M2.7-HS. Data is cached in `scripts/bench-smart-data.json` (auto-generated at runtime, not committed) — subsequent runs reuse cached data without API calls.

```bash
# First run: generates data + benchmarks
python3 scripts/memory-5a-bench.py 300 --smart

# Second run: loads cached data, no API needed
python3 scripts/memory-5a-bench.py 300 --smart
```

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
export MEMOS_URL=http://<MEMOS_HOST>:8765
export COGNEE_URL=http://<COGNEE_HOST>:8000

# NAS endpoints (if using NAS as backend)
# export MEMOS_URL=http://<NAS_HOST>:8765
# export COGNEE_URL=http://<NAS_HOST>:8766
```

This way `python3 scripts/memory-5a-bench.py` just works on any machine without flags.

## Setting Up Cron Monitoring

Create an OpenClaw cron job to run the monitor every hour:

```
/cron add --every 60m --label "memory-health" -- bash ~/.openclaw/workspace/skills/ops-five-layer-memory/scripts/memory-5a-monitor.sh
```

The monitor script outputs alerts only on failure — cron delivers them to your configured channel.

## Interpreting Results

**Healthy baseline (Scott#1 local, Qwen2.5-7B, 300 rounds 2026-03-26):**
- L1: P50=9ms, P95=16ms (local SQLite)
- L2: P50=6ms write, P50=5ms recall (local LanceDB)
- L3: P50=56ms login, P50=19ms search (local Cognee)
- L3.5: P50=73ms add, P50=91ms search (local MemOS, 4 workers)
- L5: < 1ms (local filesystem)

**Healthy baseline (remote from Scott#3, 100 rounds):**
- L2: P50=18ms (local LanceDB)
- L3: P50=57ms search (network hop to Scott#1)
- L3.5: P50=113ms add (network hop to Scott#1)

**Common failures:**
- L3 health fails → Cognee container down (`docker restart oc-cognee-api`)
- L3 search 401 → wrong Cognee instance (check `--cognee-url` / `COGNEE_URL`)
- L3.5 search fails → MemOS container or Neo4j down
- L3.5 add timeout (30s+) → Neo4j memory dedup O(n) scan; clean bench garbage (see Maintenance below)
- L1 count = 0 → LCM never ran compaction (check `summaryModel` config)
- L2 no .lance files → LanceDB Pro plugin not enabled

## Maintenance

### Cleaning Bench Garbage from MemOS

Stress tests leave test memories in Neo4j. Over time this causes MemOS add latency to degrade from ~80ms to 30s+ (Neo4j dedup does O(n) full-table scans).

```bash
# Count bench garbage
docker exec memos-neo4j cypher-shell -u neo4j -p 12345678 \
  "MATCH (n:Memory) WHERE n.memory CONTAINS 'bench' RETURN count(n)"

# Delete bench garbage
docker exec memos-neo4j cypher-shell -u neo4j -p 12345678 \
  "MATCH (n:Memory) WHERE n.memory CONTAINS 'bench' DETACH DELETE n RETURN count(*)"

# Restart MemOS after cleanup
docker restart memos-api
```

### MemOS Configuration (docker-compose.yml)

Key settings for stability:
```yaml
command: uvicorn main:app --host 0.0.0.0 --port 8765 --workers 4  # multi-worker prevents blocking
environment:
  ASYNC_MODE: async  # avoid sync mode which forces LLM extraction (10-15s delay)
  MOS_CHAT_MODEL: Qwen/Qwen2.5-32B-Instruct  # SiliconFlow ~¥7-8/月
  MEMRADER_MODEL: Qwen/Qwen2.5-32B-Instruct
  MOS_EMBEDDER_BACKEND: universal_api  # ⚠️ 必須設！默認 ollama 會報錯
```

### Cognee Configuration

Key settings:
```
LLM_MODEL=openai/Qwen/Qwen2.5-32B-Instruct
DEFAULT_SEARCH_TYPE=CHUNKS  # pure vector, no LLM graph completion
```
Container must bind `0.0.0.0:8000` (not 127.0.0.1) for remote access.

## Known Issues

- **L2 filesystem scan latency spike:** `L2/files` test runs `find ~/.openclaw/ -name '*.lance'` which can hit 3s+ on cold cache. Actual LanceDB write latency is ~7ms. This is a test artifact, not a functional issue.
- **Cognee login+search combined:** These two operations share a single function (`do_login_and_search()`) to avoid Python closure bugs with the auth token. Login and search latencies are still reported separately.
- **NAS vs local Cognee auth:** NAS Cognee and local Cognee are separate instances with independent auth. Login tokens are NOT interchangeable.
- **Neo4j memory accumulation:** MemOS add latency degrades with total memory count due to O(n) dedup scans. Keep total memories under ~2000 for <100ms add latency. Run cleanup periodically.

## Benchmark History

| Date | Rounds | Pass Rate | L3.5 P50 | Notes |
|------|--------|-----------|----------|-------|
| 2026-03-26 | 300 | **100%** (5100/5100) | 82ms | Qwen2.5-7B, 4 workers, post-cleanup |
| 2026-03-26 | 500 | 99.6% (8468/8500) | 88ms | MiniMax→Qwen2.5-7B migration day |
| 2026-03-25 | 100 | 100% (1700/1700) | 110ms | Scott#1 local, MiniMax M2.7-HS |
| 2026-03-25 | 100 | 47.1% (800/1700) | 113ms | Scott#3 remote (L1/L5 path mismatch) |

## Concurrent Multi-Machine Test

四台机器同时并发测试 MemOS + Cognee，验证共享后端在并发负载下的稳定性。

### 用法

```bash
bash scripts/concurrent-memory-test.sh [rounds] [--memos-only|--cognee-only]
```

- `bash scripts/concurrent-memory-test.sh 10` — 10 轮，测 MemOS + Cognee
- `bash scripts/concurrent-memory-test.sh 50` — 50 轮压测
- `bash scripts/concurrent-memory-test.sh 10 --memos-only` — 只测 MemOS
- `bash scripts/concurrent-memory-test.sh 10 --cognee-only` — 只测 Cognee

### 测试点

| 层 | 操作 | 说明 |
|---|---|---|
| MemOS | add | 写入记忆（/product/add） |
| MemOS | search | 搜索记忆（/product/search） |
| Cognee | health | 健康检查（/api/v1/settings） |
| Cognee | login | 登录获取 token（/api/v1/auth/login） |
| Cognee | search | CHUNKS 搜索（/api/v1/search） |
| Cognee | search2 | 第二次搜索（验证并发稳定性） |

### 架构

- 四台机器通过 SSH 并行启动 worker.py
- 所有机器同时访问老大（10.10.20.178）上的 MemOS 和 Cognee 服务
- 输出按层汇总 Pass/Fail/P50/P95/Max

### Baseline (2026-03-27, 10 轮 × 4 台)

| 层 | Pass | Fail | 说明 |
|---|---|---|---|
| MemOS | 80/80 | 0 | add P50=81ms, search P50=235ms |
| Cognee | 160/160 | 0 | health P50=42ms, search P50=119ms |
