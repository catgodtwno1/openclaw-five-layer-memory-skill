#!/bin/bash
# 五層記憶健康監控 — 每小時跑一輪快速檢查，失敗即告警
# 設計為 cron 調用，失敗時通過 openclaw cron 機制發 Telegram 告警

set -uo pipefail

LOG="/tmp/memory-monitor-$(date +%Y%m%d).log"
ALERT_FILE="/tmp/memory-monitor-alert.txt"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

PASS=0
FAIL=0
ERRORS=""

check() {
    local name=$1 cmd=$2
    if eval "$cmd" >/dev/null 2>&1; then
        ((PASS++))
    else
        ((FAIL++))
        ERRORS+="$name "
    fi
}

log "===== Memory Monitor Start ====="

# L1: LCM
check "L1/db" "sqlite3 /Users/scott/.openclaw/lcm.db 'SELECT count(*) FROM summaries;' | grep -E '^[0-9]+$'"
check "L1/fts" "sqlite3 /Users/scott/.openclaw/lcm.db 'SELECT count(*) FROM summaries_fts LIMIT 1;'"

# L2: LanceDB
check "L2/files" "test \$(find /Users/scott/.openclaw/ -name '*.lance' 2>/dev/null | wc -l | tr -d ' ') -gt 0"

# L3: Cognee
check "L3/alive" "curl -sf --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v1/auth/me | grep -E '200|401'"
L3_TOKEN=$(curl -s --max-time 5 -X POST http://127.0.0.1:8000/api/v1/auth/login \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d 'username=default_user@example.com&password=default_password' 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
if [ -n "$L3_TOKEN" ]; then
    ((PASS++))
    # Search test
    L3_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X POST http://127.0.0.1:8000/api/v1/search \
        -H "Authorization: Bearer $L3_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"query":"memory","search_type":"CHUNKS"}' 2>/dev/null || echo "000")
    if [ "$L3_CODE" = "200" ] || [ "$L3_CODE" = "404" ]; then
        ((PASS++))
    else
        ((FAIL++)); ERRORS+="L3/search "
    fi
else
    ((FAIL++)); ERRORS+="L3/login "
    ((FAIL++)); ERRORS+="L3/search "
fi

# L3.5: MemOS
check "L35/search" "curl -sf --max-time 10 -X POST http://127.0.0.1:8765/product/search \
    -H 'Content-Type: application/json' \
    -d '{\"query\":\"test\",\"user_id\":\"openclaw\",\"top_k\":1}' | grep -qi 'success\|200'"

# L5: Files
check "L5/dir" "test -d /Users/scott/.openclaw/workspace/memory"
check "L5/files" "test \$(ls /Users/scott/.openclaw/workspace/memory/*.md 2>/dev/null | wc -l | tr -d ' ') -gt 0"

TOTAL=$((PASS + FAIL))
log "Result: ✅ $PASS ❌ $FAIL / $TOTAL checks"

if [ "$FAIL" -gt 0 ]; then
    MSG="⚠️ 五層記憶監控告警 ($(date '+%H:%M'))
❌ 失敗: $FAIL/$TOTAL
問題: $ERRORS
查看日誌: cat $LOG"
    log "ALERT: $ERRORS"
    echo "$MSG" > "$ALERT_FILE"
    # Output alert for cron to pick up
    echo "$MSG"
    exit 1
else
    log "All OK"
    # Clean old alert
    rm -f "$ALERT_FILE"
    exit 0
fi
