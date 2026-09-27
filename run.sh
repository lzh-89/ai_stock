#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
URL="http://${HOST}:${PORT}"
LOG_DIR="${ROOT_DIR}/logs"
LOG_FILE="${LOG_DIR}/server.log"

cd "$ROOT_DIR"

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装 Python 3。"
  exit 1
fi

listen_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
  fi
}

port_processes() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
  fi
}

is_project_service() {
  local PID="$1"
  local CMD

  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  case "$CMD" in
    *"backend.server"*|*"backend/server.py"*)
      return 0
      ;;
  esac
  return 1
}

project_pids() {
  local PID

  for PID in $(listen_pids); do
    if is_project_service "$PID"; then
      echo "$PID"
    fi
  done
}

stop_existing_service() {
  PIDS="$(project_pids)"
  if [ -z "${PIDS}" ]; then
    return 0
  fi

  echo "检测到项目服务已经在运行：${URL}"
  echo "正在停止旧服务..."
  for PID in ${PIDS}; do
    kill "${PID}" 2>/dev/null || true
  done

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ -z "$(listen_pids)" ]; then
      echo "旧服务已停止。"
      return 0
    fi
    sleep 0.5
  done

  echo "旧服务没有及时退出，请手动检查端口 ${PORT}。"
  exit 1
}

stop_existing_service

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "端口 ${PORT} 已被其他程序占用。"
    echo
    port_processes
    echo
    echo "可以换一个端口启动，例如："
    echo
    echo "  PORT=8001 ./run.sh"
    echo
    exit 1
  fi
fi

echo "启动 A 股中短线雷达..."
echo "项目目录：${ROOT_DIR}"
echo "访问地址：${URL}"
mkdir -p "$LOG_DIR"
echo "日志文件：${LOG_FILE}"
echo
echo "停止服务：按 Ctrl+C"
echo

{
  echo
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') start ${URL} ====="
  python3 -u -m backend.server --host "$HOST" --port "$PORT"
} 2>&1 | tee -a "$LOG_FILE"
