#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${ROOT_DIR}/.routing_matrix_server.pid"
SERVER_SCRIPT="${ROOT_DIR}/routing_matrix_server.py"

find_server_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    local saved_pid
    saved_pid="$(tr -d '[:space:]' < "${PID_FILE}")"
    if [[ "${saved_pid}" =~ ^[0-9]+$ ]] && kill -0 "${saved_pid}" 2>/dev/null; then
      local command_line
      command_line="$(ps -p "${saved_pid}" -o command= 2>/dev/null || true)"
      if [[ "${command_line}" == *"${SERVER_SCRIPT}"* ]]; then
        printf '%s\n' "${saved_pid}"
        return 0
      fi
    fi
  fi

  if command -v lsof >/dev/null 2>&1; then
    local candidate command_line
    while IFS= read -r candidate; do
      [[ "${candidate}" =~ ^[0-9]+$ ]] || continue
      command_line="$(ps -p "${candidate}" -o command= 2>/dev/null || true)"
      if [[ "${command_line}" == *"${SERVER_SCRIPT}"* ]]; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done < <(lsof -nP -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true)
  fi

  return 1
}

if ! SERVER_PID="$(find_server_pid)"; then
  rm -f "${PID_FILE}"
  echo "Routing matrix server is not running."
  exit 0
fi

echo "Stopping routing matrix server (PID ${SERVER_PID})..."
kill "${SERVER_PID}"

for _ in $(seq 1 30); do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    echo "Server stopped."
    exit 0
  fi
  sleep 0.1
done

echo "Server did not stop gracefully; forcing shutdown."
kill -KILL "${SERVER_PID}"
rm -f "${PID_FILE}"
echo "Server stopped."
