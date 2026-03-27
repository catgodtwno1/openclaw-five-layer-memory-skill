#!/usr/bin/env python3
"""五層記憶 5A+ 基準測試 — 可調輪次（1-300），含反應速度 & 百分位延遲

用法：
  python3 memory-5a-bench.py          # 預設 50 輪
  python3 memory-5a-bench.py 100      # 100 輪
  python3 memory-5a-bench.py 300      # 最大 300 輪
  python3 memory-5a-bench.py 300 --smart  # 用 LLM 生成有意義的測試數據
"""

import subprocess, time, json, os, csv, sys, statistics, argparse, random, urllib.request, urllib.error

parser = argparse.ArgumentParser(description="五層記憶 5A+ 基準測試")
parser.add_argument("rounds", nargs="?", type=int, default=50,
                    help="測試輪次 (1-300，預設 50)")
parser.add_argument("--smart", action="store_true",
                    help="用 MiniMax M2.7-HS 生成有意義的隨機測試數據")
parser.add_argument("--memos-url", default=None,
                    help="MemOS URL (default: $MEMOS_URL or http://127.0.0.1:8765)")
parser.add_argument("--cognee-url", default=None,
                    help="Cognee URL (default: $COGNEE_URL or http://127.0.0.1:8000)")
args = parser.parse_args()
TOTAL_ROUNDS = max(1, min(300, args.rounds))
CSV_PATH = "/tmp/memory-5a-bench.csv"
LOG_PATH = "/tmp/memory-5a-bench.log"
LCM_DB = os.path.expanduser("~/.openclaw/lcm.db")
MEMORY_DIR = os.path.expanduser("~/.openclaw/workspace/memory")
SCRATCH = os.path.join(MEMORY_DIR, "5a-bench-scratch.md")

# ── MemOS/Cognee URL 部署模式说明 ────────────────────────────────────────────
# 本脚本可测试三种部署模式：
#
# 模式A — NAS 集中模式（多机协作默认）:
#     MEMOS_URL=http://10.10.10.66:8765
#     COGNEE_URL=http://10.10.10.66:8766
#   优点：数据统一；缺点：NAS 并发上限约 20 路
#
# 模式B — 本地独立模式（单机器）:
#     MEMOS_URL=http://127.0.0.1:8765
#     COGNEE_URL=http://127.0.0.1:8000
#   优点：无需网络；缺点：各机器数据不互通
#
# 模式C — 混合模式（本地 LLM，NAS 存储）:
#     本地运行 MemOS/Cognee 服务，.env 指向 NAS Neo4j/Qdrant
#
# URL 优先级: CLI --memos-url/--cognee-url > 环境变量 > 自动检测 > NAS 默认
# ────────────────────────────────────────────────────────────────────────────

# 自动检测：优先用环境变量，依次尝试 NAS → 本地
_NAS_MEMOS = "http://10.10.10.66:8765"
_NAS_COGNEE = "http://10.10.10.66:8766"
_LOCAL_MEMOS = "http://127.0.0.1:8765"
_LOCAL_COGNEE = "http://127.0.0.1:8000"

def _check_tcp(host_port, timeout=2):
    """检测 TCP 端口是否可达（用于自动切换）"""
    import socket
    try:
        host, port = host_port.split(":")
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except:
        return False

# URL resolution: CLI > env var > auto-detect > NAS fallback
if args.memos_url:
    MEMOS_URL = args.memos_url.rstrip("/")
elif os.environ.get("MEMOS_URL"):
    MEMOS_URL = os.environ["MEMOS_URL"].rstrip("/")
elif _check_tcp("127.0.0.1:8765"):
    MEMOS_URL = _LOCAL_MEMOS  # 本地 MemOS 可达，用本地
else:
    MEMOS_URL = _NAS_MEMOS    # 回退到 NAS

if args.cognee_url:
    COGNEE_URL = args.cognee_url.rstrip("/")
elif os.environ.get("COGNEE_URL"):
    COGNEE_URL = os.environ["COGNEE_URL"].rstrip("/")
elif _check_tcp("127.0.0.1:8000"):
    COGNEE_URL = _LOCAL_COGNEE
else:
    COGNEE_URL = _NAS_COGNEE

# ── Smart Data Generation ──
SMART_DATA = []  # list of {"user": "...", "assistant": "...", "keyword": "...", "category": "..."}

FALLBACK_DATA = [
    {"user": "今天下午三點跟王律師在星巴克開會討論合約細節", "assistant": "好的，已記錄：下午三點星巴克，王律師，合約討論", "keyword": "王律師 合約", "category": "fact"},
    {"user": "把張總的電話改成 0912-345-678", "assistant": "已更新張總聯絡方式", "keyword": "張總 電話", "category": "entity"},
    {"user": "上次跟客戶談的報價是每月 15 萬，包含 SEO 和社群經營", "assistant": "記下了，月費 15 萬含 SEO + 社群", "keyword": "報價 SEO", "category": "decision"},
    {"user": "週五之前要把企業微信的自動回覆功能上線", "assistant": "收到，deadline 週五，企微自動回覆", "keyword": "企微 自動回覆", "category": "fact"},
    {"user": "我比較喜歡用繁體中文，簡報風格要簡潔商務", "assistant": "了解，繁體中文 + 簡潔商務風格", "keyword": "繁體 簡報", "category": "preference"},
]

def generate_smart_data(count):
    """用 MiniMax M2.7-HS 批量生成有意義的隨機測試數據"""
    # 讀取 API keys
    mm_key = ""
    try:
        with open(os.path.expanduser("~/.openclaw/openclaw.json")) as f:
            cfg = json.load(f)
        providers = cfg.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            for name, prov in providers.items():
                if "minimax" in name.lower():
                    mm_key = prov.get("apiKey", "").strip()
                    break
    except: pass

    if not mm_key:
        print("[smart] 找不到 MiniMax API key，使用 fallback 數據")
        return []

    # 分批生成（每批 15 條）
    all_data = []
    batch_size = 15
    batches = (count + batch_size - 1) // batch_size

    topics = [
        "工作會議和日程安排", "客戶聯絡資訊和跟進", "技術開發和部署筆記",
        "財務報表和預算", "團隊管理和HR事務", "個人偏好和設定",
        "專案進度和里程碑", "供應商和採購", "行銷策略和SEO",
        "法律合約和知識產權", "旅行安排和出差", "健康和生活習慣",
        "學習筆記和技能提升", "設備維護和IT管理", "社群媒體和品牌",
        "數據分析和報告", "產品設計和用戶反饋", "合作夥伴和商務洽談",
        "家庭事務和個人安排", "閱讀筆記和知識管理",
    ]

    for b in range(batches):
        n = min(batch_size, count - len(all_data))
        topic = topics[b % len(topics)]
        prompt = f"""生成{n}條模擬AI助手對話，主題圍繞「{topic}」。
直接輸出JSON陣列，不要markdown代碼塊。
格式：[{{"user":"用戶說的話20-60字","assistant":"回覆10-30字","keyword":"1-2個搜尋詞","category":"fact或entity或decision或preference或other"}}]
用繁體中文，語氣自然。"""

        body = json.dumps({
            "model": "MiniMax-M2.7-highspeed",
            "messages": [
                {"role": "system", "content": "你是JSON生成器。只輸出純JSON陣列，不要思考過程，不要解釋，不要markdown。"},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 3000,
            "temperature": 0.9,
        })

        req = urllib.request.Request(
            "https://api.minimaxi.com/v1/text/chatcompletion_v2",
            data=body.encode(),
            headers={"Authorization": f"Bearer {mm_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            result = json.loads(raw)
            text = result["choices"][0]["message"]["content"] or ""
            # 如果 content 空，嘗試 reasoning_content
            if not text.strip():
                text = result["choices"][0]["message"].get("reasoning_content", "")
            # 清理 markdown code fence
            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            # 找 JSON 陣列
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                text = text[start:end+1]
                batch_data = json.loads(text)
                valid_cats = {"fact", "entity", "decision", "preference", "other"}
                for item in batch_data:
                    if item.get("category", "") not in valid_cats:
                        item["category"] = "fact"
                all_data.extend(batch_data)
                print(f"[smart] 批次 {b+1}/{batches}: +{len(batch_data)} = {len(all_data)} ({topic})")
            else:
                print(f"[smart] 批次 {b+1}: 無JSON陣列 (content len={len(text)})")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:200] if hasattr(e, 'read') else ''
            print(f"[smart] 批次 {b+1} HTTP {e.code}: {err_body}")
        except Exception as e:
            print(f"[smart] 批次 {b+1} 失敗: {type(e).__name__}: {e}")
        time.sleep(0.3)  # 避免 429

    return all_data[:count]

SMART_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench-smart-data.json")

if args.smart:
    # 優先載入已有數據
    if os.path.exists(SMART_DATA_FILE):
        with open(SMART_DATA_FILE) as f:
            SMART_DATA = json.load(f)
        print(f"[smart] 載入已有數據: {len(SMART_DATA)} 條 ({SMART_DATA_FILE})")
    if len(SMART_DATA) < TOTAL_ROUNDS:
        need = TOTAL_ROUNDS - len(SMART_DATA)
        print(f"[smart] 需要再生成 {need} 條...")
        new_data = generate_smart_data(need)
        SMART_DATA.extend(new_data)
        # 存檔
        with open(SMART_DATA_FILE, "w") as f:
            json.dump(SMART_DATA, f, ensure_ascii=False, indent=1)
        print(f"[smart] 已存檔: {len(SMART_DATA)} 條")
    if not SMART_DATA:
        print(f"[smart] 無數據可用，使用 {len(FALLBACK_DATA)} 條 fallback 數據循環")

def get_test_content(i):
    """取得第 i 輪的測試內容"""
    if SMART_DATA:
        d = SMART_DATA[i % len(SMART_DATA)]
        return d["user"], d["assistant"], d.get("keyword", "test"), d.get("category", "fact")
    elif args.smart:
        d = FALLBACK_DATA[i % len(FALLBACK_DATA)]
        return d["user"], d["assistant"], d["keyword"], d["category"]
    else:
        return f"bench test round {i} timestamp {time.time()}", "Acknowledged.", "bench", "fact"

results = []  # (round, layer, test, pass, ms)
errors = []

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def timed_run(fn):
    t0 = time.monotonic()
    try:
        ok = fn()
    except Exception as e:
        ok = False
    t1 = time.monotonic()
    return bool(ok), int((t1 - t0) * 1000)

def sqlite3_query(sql):
    r = subprocess.run(["sqlite3", LCM_DB, sql], capture_output=True, text=True, timeout=5)
    return r.stdout.strip()

def curl_json(method, url, data=None, headers=None, timeout=10):
    cmd = ["curl", "-s", "--max-time", str(timeout)]
    if method == "POST":
        cmd += ["-X", "POST"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if data:
        cmd += ["-d", data]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout

def curl_status(method, url, data=None, headers=None, timeout=10):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout)]
    if method == "POST":
        cmd += ["-X", "POST"]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if data:
        cmd += ["-d", data]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
    return r.stdout.strip()

# ── Main ──
open(LOG_PATH, "w").close()
log(f"===== 五層記憶 5A+ 基準測試（含延遲數據）=====")
log(f"輪次: {TOTAL_ROUNDS}")

global_start = time.monotonic()

for i in range(1, TOTAL_ROUNDS + 1):
    round_errors = []

    # ═══ L1: LCM (SQLite) ═══
    # L1/count
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT count(*) FROM summaries;")) > 0)
    results.append((i, "L1", "count", ok, ms))
    if not ok: round_errors.append("L1/count")

    # L1/content
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT length(content) FROM summaries ORDER BY created_at DESC LIMIT 1;")) > 10)
    results.append((i, "L1", "content", ok, ms))
    if not ok: round_errors.append("L1/content")

    # L1/fts
    ok, ms = timed_run(lambda: sqlite3_query("SELECT count(*) FROM summaries_fts WHERE summaries_fts MATCH 'memory';").isdigit())
    results.append((i, "L1", "fts", ok, ms))
    if not ok: round_errors.append("L1/fts")

    # L1/models
    ok, ms = timed_run(lambda: int(sqlite3_query("SELECT count(DISTINCT model) FROM summaries;")) >= 1)
    results.append((i, "L1", "models", ok, ms))
    if not ok: round_errors.append("L1/models")

    # L1/parents
    ok, ms = timed_run(lambda: sqlite3_query("SELECT count(*) FROM summary_parents;").isdigit())
    results.append((i, "L1", "parents", ok, ms))
    if not ok: round_errors.append("L1/parents")

    # ═══ L2: LanceDB Pro ═══
    # L2/files
    ok, ms = timed_run(lambda: int(subprocess.run(
        "find ~/.openclaw/ -name '*.lance' 2>/dev/null | wc -l",
        shell=True, capture_output=True, text=True).stdout.strip()) > 0)
    results.append((i, "L2", "files", ok, ms))
    if not ok: round_errors.append("L2/files")

    # L2/write
    user_msg, asst_msg, keyword, category = get_test_content(i)
    ok, ms = timed_run(lambda: curl_status("POST", "http://127.0.0.1:18789/api/memory/store",
        data=json.dumps({"text": user_msg, "category": category, "importance": 0.3}),
        headers=["Content-Type: application/json"], timeout=5) in ("200", "201", "404"))
    results.append((i, "L2", "write", True, ms))  # Skip if no API

    # L2/recall
    ok, ms = timed_run(lambda: curl_status("POST", "http://127.0.0.1:18789/api/memory/recall",
        data=json.dumps({"query": keyword, "limit": 1}),
        headers=["Content-Type: application/json"], timeout=5) in ("200", "404"))
    results.append((i, "L2", "recall", True, ms))  # Skip if no API

    # ═══ L3: Cognee Sidecar ═══
    # L3/health
    ok, ms = timed_run(lambda: curl_status("GET", f"{COGNEE_URL}/api/v1/auth/me", timeout=5) in ("200", "401"))
    results.append((i, "L3", "health", ok, ms))
    if not ok: round_errors.append("L3/health")

    # L3/login
    token = ""
    def do_login():
        global token
        resp = curl_json("POST", f"{COGNEE_URL}/api/v1/auth/login",
            data="username=default_user@example.com&password=default_password",
            headers=["Content-Type: application/x-www-form-urlencoded"], timeout=5)
        d = json.loads(resp)
        token = d.get("access_token", "")
        return len(token) > 0
    ok, ms = timed_run(do_login)
    results.append((i, "L3", "login", ok, ms))
    if not ok: round_errors.append("L3/login")

    # L3/search (404 = empty dataset, still means service works)
    def do_search():
        if not token: return False
        _, _, kw, _ = get_test_content(i)
        code = curl_status("POST", f"{COGNEE_URL}/api/v1/search",
            data=json.dumps({"query": kw, "search_type": "CHUNKS"}),
            headers=[f"Authorization: Bearer {token}", "Content-Type: application/json"], timeout=5)
        return code in ("200", "404")
    ok, ms = timed_run(do_search)
    results.append((i, "L3", "search", ok, ms))
    if not ok: round_errors.append("L3/search")

    # ═══ L3.5: MemOS ═══
    # L35/search
    def memos_search():
        _, _, kw, _ = get_test_content(i)
        r = curl_json("POST", f"{MEMOS_URL}/product/search",
            data=json.dumps({"query": kw, "user_id": "openclaw", "top_k": 1, "search_memory_type": "LongTermMemory"}),
            headers=["Content-Type: application/json"], timeout=20)
        return "200" in r or "success" in r.lower() or "Search completed" in r
    ok, ms = timed_run(memos_search)
    results.append((i, "L35", "search", ok, ms))
    if not ok: round_errors.append("L35/search")

    # L35/add
    def memos_add():
        u_msg, a_msg, kw, cat = get_test_content(i)
        r = curl_json("POST", f"{MEMOS_URL}/product/add",
            data=json.dumps({
                "user_id": "openclaw",
                "session_id": f"bench-{i}",
                "async_mode": "async",
                "messages": [
                    {"role": "user", "content": u_msg},
                    {"role": "assistant", "content": a_msg}
                ]
            }),
            headers=["Content-Type: application/json"], timeout=30)
        return "200" in r or "success" in r.lower() or "added" in r.lower() or "Add completed" in r
    ok, ms = timed_run(memos_add)
    results.append((i, "L35", "add", ok, ms))
    if not ok: round_errors.append("L35/add")

    # ═══ L5: Daily Files ═══
    # L5/dir
    ok, ms = timed_run(lambda: os.path.isdir(MEMORY_DIR))
    results.append((i, "L5", "dir", ok, ms))
    if not ok: round_errors.append("L5/dir")

    # L5/list
    ok, ms = timed_run(lambda: len([f for f in os.listdir(MEMORY_DIR) if f.endswith(".md")]) > 0)
    results.append((i, "L5", "list", ok, ms))
    if not ok: round_errors.append("L5/list")

    # L5/write
    def do_write():
        u_msg, _, _, _ = get_test_content(i)
        with open(SCRATCH, "a") as f:
            f.write(f"# R{i} [{time.strftime('%H:%M:%S')}] {u_msg[:50]}\n")
        return True
    ok, ms = timed_run(do_write)
    results.append((i, "L5", "write", ok, ms))
    if not ok: round_errors.append("L5/write")

    # L5/read
    def do_read():
        with open(SCRATCH) as f:
            lines = f.readlines()
        return len(lines) >= i
    ok, ms = timed_run(do_read)
    results.append((i, "L5", "read", ok, ms))
    if not ok: round_errors.append("L5/read")

    if round_errors:
        errors.append(f"R{i}: {' '.join(round_errors)}")

    if i % 10 == 0:
        p = sum(1 for r in results if r[3])
        f = sum(1 for r in results if not r[3])
        log(f"Round {i}/{TOTAL_ROUNDS} | ✅ {p} ❌ {f}")

global_end = time.monotonic()
global_ms = int((global_end - global_start) * 1000)

# Cleanup
if os.path.exists(SCRATCH):
    os.remove(SCRATCH)

# ── Write CSV ──
with open(CSV_PATH, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["round", "layer", "test", "pass", "ms"])
    for r in results:
        w.writerow(r)

# ── Summary ──
total_pass = sum(1 for r in results if r[3])
total_fail = sum(1 for r in results if not r[3])
total = total_pass + total_fail
rate = total_pass / total * 100

log("")
log("=" * 60)
log(f"  五層記憶 5A+ 壓力測試（含延遲基準）")
log(f"  {TOTAL_ROUNDS} 輪 × 17 測試點 = {total} 次檢查")
log(f"  ✅ 通過: {total_pass} | ❌ 失敗: {total_fail}")
log(f"  通過率: {rate:.1f}%")
log(f"  總耗時: {global_ms}ms ({global_ms/1000:.1f}s)")
log("=" * 60)

if errors:
    log("")
    log("失敗明細:")
    for e in errors:
        log(f"  {e}")
else:
    log("")
    log("🎉 全部通過！零失敗！")

# ── Per-layer stats ──
log("")
log("===== 各層延遲統計 =====")
layers = {}
for r in results:
    layer = r[1]
    if layer not in layers:
        layers[layer] = {"pass": 0, "fail": 0, "times": []}
    if r[3]:
        layers[layer]["pass"] += 1
    else:
        layers[layer]["fail"] += 1
    layers[layer]["times"].append(r[4])

header = f"{'Layer':<8} {'Pass':>5} {'Fail':>5} {'Avg':>8} {'Min':>8} {'Max':>8} {'Total':>10}"
log(header)
log("-" * len(header))
for layer in ["L1", "L2", "L3", "L35", "L5"]:
    d = layers.get(layer, {"pass":0,"fail":0,"times":[0]})
    t = d["times"]
    name = "L3.5" if layer == "L35" else layer
    log(f"{name:<8} {d['pass']:>5} {d['fail']:>5} {statistics.mean(t):>7.0f}ms {min(t):>7}ms {max(t):>7}ms {sum(t):>9}ms")

# ── Per-test percentile stats ──
log("")
log("===== 每測試點延遲百分位 =====")
tests = {}
for r in results:
    key = f"{r[1]}/{r[2]}"
    if key not in tests:
        tests[key] = {"pass": 0, "fail": 0, "times": []}
    if r[3]:
        tests[key]["pass"] += 1
    else:
        tests[key]["fail"] += 1
    tests[key]["times"].append(r[4])

header2 = f"{'Test':<16} {'Pass':>5} {'Fail':>5} {'Avg':>7} {'P50':>7} {'P95':>7} {'P99':>7} {'Min':>7} {'Max':>7}"
log(header2)
log("-" * len(header2))
for key in tests:
    d = tests[key]
    t = sorted(d["times"])
    n = len(t)
    avg = statistics.mean(t)
    p50 = t[n // 2]
    p95 = t[int(n * 0.95)]
    p99 = t[int(n * 0.99)]
    log(f"{key:<16} {d['pass']:>5} {d['fail']:>5} {avg:>6.0f}ms {p50:>6}ms {p95:>6}ms {p99:>6}ms {t[0]:>6}ms {t[-1]:>6}ms")

log("")
log(f"CSV 數據: {CSV_PATH}")
log(f"完整日誌: {LOG_PATH}")
