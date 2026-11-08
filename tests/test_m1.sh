#!/usr/bin/env bash
# M1 冒烟测试：不依赖 DSM，直接跑守护进程 + api.cgi 的本地链路。
#   ./tests/test_m1.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TMP="$(mktemp -d)"
PID=""
cleanup() {
    [ -n "$PID" ] && kill "$PID" 2>/dev/null || true
    [ -n "$PID" ] && wait "$PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

TOKEN="0123456789abcdef0123456789abcdef"
mkdir -p "$TMP/var"
cp spk/var-default-config.json "$TMP/var/config.json"
printf '%s' "$TOKEN" > "$TMP/var/token"

python3 src/bin/autofilesync-daemon.py --var "$TMP/var" > "$TMP/daemon.log" 2>&1 &
PID=$!
for _ in $(seq 1 40); do
    [ -S "$TMP/var/http/autofilesync.sock" ] && break
    sleep 0.1
done
[ -S "$TMP/var/http/autofilesync.sock" ] || { echo "FAIL: socket 未创建"; cat "$TMP/daemon.log"; exit 1; }

api() {
    curl -sf --unix-socket "$TMP/var/http/autofilesync.sock" \
        -H "X-AFS-Token: $TOKEN" "http://localhost$1" || true
}

fail() { echo "FAIL: $*"; exit 1; }

echo "== 1. 无 token 必须 401"
code=$(curl -s -o /dev/null -w '%{http_code}' --unix-socket "$TMP/var/http/autofilesync.sock" http://localhost/api/status)
[ "$code" = "401" ] || fail "期望 401，实际 $code"

echo "== 2. 带 token 的 status"
out=$(api /api/status)
echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"], d; assert len(d["jobs"])==7, len(d["jobs"]); assert d["rsync"]["ok"] is False or d["rsync"]["ok"] is True' \
    || fail "status 结构不符"

echo "== 3. config 与 about"
api /api/config | python3 -c 'import json,sys; d=json.load(sys.stdin); assert len(d["config"]["jobs"])==7' || fail "config 不符"
api /api/about | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["name"]=="autofilesync"' || fail "about 不符"

echo "== 4. logs（文件不存在时返回空）"
api "/api/logs?n=10" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"] and d["lines"]==[]' || fail "logs 不符"

echo "== 5. api.cgi 转发"
export AFS_SOCK="$TMP/var/http/autofilesync.sock" AFS_AUTH_CGI=/bin/false
HTTP_X_AFS_TOKEN="$TOKEN" REQUEST_METHOD=GET QUERY_STRING=action=status \
    python3 src/ui/api.cgi | tail -n 1 | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"]' \
    || fail "api.cgi 转发失败"

echo "== 6. SIGTERM 后 socket 清理"
kill -TERM "$PID"; wait "$PID" 2>/dev/null || true; PID=""
sleep 0.3
[ ! -e "$TMP/var/http/autofilesync.sock" ] || fail "socket 未被清理"

echo "ALL PASS"
