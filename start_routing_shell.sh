#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="127.0.0.1"
PORT="8000"
URL="http://${HOST}:${PORT}/web/shell/index.html"
LOG_FILE="${ROOT_DIR}/.routing_matrix_server.log"

is_port_listening() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  return 1
}

start_server() {
  echo "Starting routing_matrix_server.py on ${HOST}:${PORT}..."
  nohup python3 "${ROOT_DIR}/routing_matrix_server.py" \
    --host "${HOST}" \
    --port "${PORT}" \
    --root "${ROOT_DIR}" \
    > "${LOG_FILE}" 2>&1 &
}

wait_for_server() {
  for _ in $(seq 1 60); do
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS "http://${HOST}:${PORT}/" >/dev/null 2>&1; then
        return 0
      fi
    elif is_port_listening; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "${URL}"
    return 0
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${URL}" >/dev/null 2>&1 &
    return 0
  fi
  if command -v start >/dev/null 2>&1; then
    start "${URL}"
    return 0
  fi
  echo "Server started. Open this URL manually:"
  echo "${URL}"
  return 0
}

if is_port_listening; then
  echo "Server already running on port ${PORT}."
else
  start_server
  if wait_for_server; then
    echo "Server is ready."
  else
    echo "Server did not become ready in time. Check:"
    echo "${LOG_FILE}"
  fi
fi

open_browser

