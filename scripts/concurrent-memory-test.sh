#!/usr/bin/env bash
# concurrent-memory-test.sh — 四台机器并发测试 MemOS + Cognee
# 用法: bash concurrent-memory-test.sh [rounds] [--memos-only|--cognee-only]
# 默认 10 轮，测 MemOS + Cognee 两层

set -euo pipefail

ROUNDS=${1:-10}
FILTER=${2:-"all"}  # all / --memos-only / --cognee-only

MACHINES=(
  "老大:10.10.20.178"
  "老二:10.10.20.90"
  "老三:10.10.20.151"
  "老四:10.10.20.225"
)

# MemOS 和 Cognee 都在老大
# ── URL 部署模式 ──────────────────────────────────────────
# 部署模式（3选1，取消注释那一行）:
#
# 模式A — NAS 集中模式（推荐，多机协作）
#   所有机器连接 NAS MemOS + Cognee，共享数据
#   优点：数据统一管理；缺点：NAS 并发上限约 20 路
#
# 模式B — 本地独立模式（单机器测试用）
#   每台机器跑自己的 MemOS + Cogneus，无共享数据
#
# 模式C — 混合模式（本地 LLM/Cache，NAS 存数据）
#   MemOS/Cognee 在本地跑，指向 NAS Neo4j + Qdrant
#
# 切换模式：取消对应行的注释，并注释掉其他行
# ──────────────────────────────────────────────────────────

# 模式A: NAS 集中模式（当前默认）
: "${MEMOS_URL:=http://10.10.10.66:8765}"
: "${COGNEE_URL:=http://10.10.10.66:8766}"

# 模式B: 本地独立模式
# MEMOS_URL="${MEMOS_URL:-http://127.0.0.1:8765}"
# COGNEE_URL="${COGNEE_URL:-http://127.0.0.1:8000}"

# 模式C: 本地服务 + NAS 存储层
# MEMOS_URL="${MEMOS_URL:-http://127.0.0.1:8765}"
# COGNEE_URL="${COGNEE_URL:-http://127.0.0.1:8000}"
# 并确保 .env 里指向 NAS Neo4j/Qdrant

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_DIR="/tmp/concurrent-memory-test-${TIMESTAMP}"
mkdir -p "$RESULT_DIR"

cat << 'WORKER' > "$RESULT_DIR/worker.py"
#!/usr/bin/env python3
"""单机 worker：在目标机器上执行 MemOS + Cognee 读写测试"""
import sys, json, time, urllib.request, urllib.error, statistics, random, string

machine_name = sys.argv[1]
rounds = int(sys.argv[2])
memos_url = sys.argv[3]
cognee_url = sys.argv[4]
test_filter = sys.argv[5] if len(sys.argv) > 5 else "all"

results = {"machine": machine_name, "rounds": rounds, "tests": []}

def api_call(method, url, data=None, headers=None, timeout=15):
    """HTTP 请求封装"""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = int((time.monotonic() - start) * 1000)
            return {"ok": True, "status": resp.status, "body": resp.read().decode(), "ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": str(e)[:200], "ms": elapsed}

def rand_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

# === MemOS 测试 ===
def test_memos(round_num):
    tag = f"concurrent-{machine_name}-r{round_num}-{rand_id()}"
    out = {"layer": "MemOS", "round": round_num, "ops": []}

    # 1. 写入
    payload = {
        "messages": [{"role": "user", "content": f"并发测试记忆 {tag}: {machine_name} 在第 {round_num} 轮写入的测试数据"}],
        "user_id": "test_concurrent"
    }
    r = api_call("POST", f"{memos_url}/product/add", payload, timeout=30)
    out["ops"].append({"op": "add", "ok": r["ok"] and "200" in r.get("body",""), "ms": r["ms"], "error": r.get("error")})

    # 2. 搜索
    payload = {"query": tag, "user_id": "test_concurrent", "relativity": 0}
    r = api_call("POST", f"{memos_url}/product/search", payload, timeout=60)
    search_ok = r["ok"] and "200" in r.get("body","")
    out["ops"].append({"op": "search", "ok": search_ok, "ms": r["ms"], "error": r.get("error")})

    return out

# === Cognee 测试 ===
def test_cognee(round_num):
    tag = f"concurrent-{machine_name}-r{round_num}-{rand_id()}"
    out = {"layer": "Cognee", "round": round_num, "ops": []}

    # 1. 健康检查 (401 = auth required, still means service is up)
    r = api_call("GET", f"{cognee_url}/api/v1/settings")
    health_ok = r["ok"] or "401" in str(r.get("error","")) or "HTTP Error 401" in str(r.get("error",""))
    out["ops"].append({"op": "health", "ok": health_ok, "ms": r["ms"], "error": r.get("error")})

    # 2. 登录获取 token (form-urlencoded)
    token = None
    login_data = "username=default_user%40example.com&password=default_password".encode()
    req = urllib.request.Request(f"{cognee_url}/api/v1/auth/login", data=login_data,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            elapsed = int((time.monotonic() - start) * 1000)
            body = resp.read().decode()
            r = {"ok": True, "body": body, "ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        r = {"ok": False, "error": str(e)[:200], "ms": elapsed}
    if r["ok"]:
        try:
            token = json.loads(r["body"]).get("access_token")
        except:
            pass
    out["ops"].append({"op": "login", "ok": bool(token), "ms": r["ms"], "error": r.get("error")})

    # 3. 搜索
    if token:
        r = api_call("GET",
                     f"{cognee_url}/api/v1/search?query=test&search_type=CHUNKS",
                     headers={"Authorization": f"Bearer {token}"})
        out["ops"].append({"op": "search", "ok": r["ok"], "ms": r["ms"], "error": r.get("error")})
    else:
        out["ops"].append({"op": "search", "ok": False, "ms": 0, "error": "no token"})

    # 4. 第二次搜索（验证并发稳定性）
    if token:
        r = api_call("GET",
                     f"{cognee_url}/api/v1/search?query=memory&search_type=CHUNKS",
                     headers={"Authorization": f"Bearer {token}"})
        out["ops"].append({"op": "search2", "ok": r["ok"], "ms": r["ms"], "error": r.get("error")})
    else:
        out["ops"].append({"op": "search2", "ok": False, "ms": 0, "error": "no token"})

    return out

# === 执行 ===
all_tests = []
start_time = time.monotonic()

for i in range(1, rounds + 1):
    if test_filter in ("all", "--memos-only"):
        all_tests.append(test_memos(i))
    if test_filter in ("all", "--cognee-only"):
        all_tests.append(test_cognee(i))

total_time = time.monotonic() - start_time

# === 汇总 ===
summary = {"machine": machine_name, "rounds": rounds, "total_sec": round(total_time, 1)}
for layer in ["MemOS", "Cognee"]:
    layer_tests = [t for t in all_tests if t["layer"] == layer]
    if not layer_tests:
        continue
    ops_map = {}
    for t in layer_tests:
        for op in t["ops"]:
            name = op["op"]
            if name not in ops_map:
                ops_map[name] = {"pass": 0, "fail": 0, "latencies": []}
            if op["ok"]:
                ops_map[name]["pass"] += 1
            else:
                ops_map[name]["fail"] += 1
            ops_map[name]["latencies"].append(op["ms"])

    layer_summary = {}
    for name, data in ops_map.items():
        lats = sorted(data["latencies"])
        layer_summary[name] = {
            "pass": data["pass"], "fail": data["fail"],
            "P50": lats[len(lats)//2] if lats else 0,
            "P95": lats[int(len(lats)*0.95)] if lats else 0,
            "P99": lats[int(len(lats)*0.99)] if lats else 0,
            "max": max(lats) if lats else 0
        }
    summary[layer] = layer_summary

# 输出 JSON
print(json.dumps(summary, ensure_ascii=False))
WORKER

echo "╔══════════════════════════════════════════════════════╗"
echo "║  四机并发记忆测试 — ${ROUNDS} 轮 × ${#MACHINES[@]} 台"
echo "║  MemOS: ${MEMOS_URL}"
echo "║  Cognee: ${COGNEE_URL}"
echo "║  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# 分发 worker 到各机器
for entry in "${MACHINES[@]}"; do
  name="${entry%%:*}"
  ip="${entry##*:}"
  echo "📤 分发 worker → $name ($ip)..."
  scp -q "$RESULT_DIR/worker.py" scott@$ip:/tmp/concurrent-worker.py 2>/dev/null
done

echo ""
echo "🚀 同时启动四台机器..."
echo ""

# 并发启动
PIDS=()
for entry in "${MACHINES[@]}"; do
  name="${entry%%:*}"
  ip="${entry##*:}"
  outfile="$RESULT_DIR/${name}.json"
  ssh scott@$ip "python3 /tmp/concurrent-worker.py '$name' $ROUNDS '$MEMOS_URL' '$COGNEE_URL' '$FILTER'" > "$outfile" 2>/dev/null &
  PIDS+=($!)
  echo "  ⏳ $name ($ip) PID=$! 已启动"
done

echo ""
echo "⏳ 等待全部完成..."

# 等待所有完成
FAILED=0
for i in "${!PIDS[@]}"; do
  entry="${MACHINES[$i]}"
  name="${entry%%:*}"
  if wait "${PIDS[$i]}"; then
    echo "  ✅ $name 完成"
  else
    echo "  ❌ $name 失败"
    FAILED=$((FAILED+1))
  fi
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "                    📊 汇总结果"
echo "═══════════════════════════════════════════════════════"
echo ""

# 解析并展示结果
python3 << PYEND
import json, os, sys

result_dir = "$RESULT_DIR"
machines = ["老大", "老二", "老三", "老四"]

all_data = []
for m in machines:
    path = os.path.join(result_dir, f"{m}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                d = json.load(f)
            all_data.append(d)
        except:
            print(f"⚠️  {m}: 结果解析失败")
            all_data.append(None)
    else:
        print(f"⚠️  {m}: 无结果文件")
        all_data.append(None)

# 按层展示
for layer in ["MemOS", "Cognee"]:
    print(f"\n{'='*55}")
    print(f"  {layer} 并发结果")
    print(f"{'='*55}")
    print(f"{'机器':<8} {'操作':<10} {'Pass':>6} {'Fail':>6} {'P50':>8} {'P95':>8} {'Max':>8}")
    print("-" * 55)
    
    total_pass = 0
    total_fail = 0
    
    for d in all_data:
        if not d or layer not in d:
            continue
        machine = d["machine"]
        for op, stats in d[layer].items():
            p = stats["pass"]
            f = stats["fail"]
            total_pass += p
            total_fail += f
            mark = "✅" if f == 0 else "❌"
            print(f"{machine:<8} {op:<10} {p:>6} {f:>6} {stats['P50']:>6}ms {stats['P95']:>6}ms {stats['max']:>6}ms {mark}")
    
    rate = total_pass / (total_pass + total_fail) * 100 if (total_pass + total_fail) > 0 else 0
    print(f"\n  总计: {total_pass} pass / {total_fail} fail ({rate:.1f}%)")

# 时间汇总
print(f"\n{'='*55}")
print(f"  耗时汇总")
print(f"{'='*55}")
for d in all_data:
    if d:
        print(f"  {d['machine']}: {d['total_sec']}s")

print(f"\n结果目录: {result_dir}")
PYEND

echo ""
echo "完成 ✅"
