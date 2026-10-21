#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="$PWD/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: .venv가 없습니다. 먼저 bash sh/setup_venv.sh를 실행하세요."
    exit 1
fi

for port in 8100 8600; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ERROR: $port 포트를 다른 프로세스가 사용 중입니다."
        exit 1
    fi
done

BACKEND_PID=""
FRONTEND_PID=""
cleanup() {
    [ -z "$FRONTEND_PID" ] || kill "$FRONTEND_PID" 2>/dev/null || true
    [ -z "$BACKEND_PID" ] || kill "$BACKEND_PID" 2>/dev/null || true
    [ -z "$FRONTEND_PID" ] || wait "$FRONTEND_PID" 2>/dev/null || true
    [ -z "$BACKEND_PID" ] || wait "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT

LLM_BACKEND=fake LANGCHAIN_TRACING_V2=false "$PYTHON" server.py &
BACKEND_PID=$!
for _ in $(seq 1 40); do
    if curl -fsS --max-time 1 http://127.0.0.1:8100/health >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "ERROR: API 서버가 시작되지 않았습니다."
        exit 1
    fi
    sleep 0.5
done
if ! curl -fsS --max-time 1 http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "ERROR: API 서버 준비 시간이 초과되었습니다."
    exit 1
fi

"$PYTHON" -m uvicorn web_demo_server:app --host 127.0.0.1 --port 8600 &
FRONTEND_PID=$!
for _ in $(seq 1 40); do
    if curl -fsS --max-time 1 http://127.0.0.1:8600/health >/dev/null 2>&1; then
        echo "채팅 화면: http://127.0.0.1:8600 (종료: Ctrl+C)"
        wait "$FRONTEND_PID"
        exit $?
    fi
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "ERROR: 프런트 서버가 시작되지 않았습니다."
        exit 1
    fi
    sleep 0.5
done
echo "ERROR: 프런트 서버 준비 시간이 초과되었습니다."
exit 1
