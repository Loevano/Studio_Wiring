#!/usr/bin/env python3
"""Safari smoke test for routing_matrix.html.

Checks that the page initializes, handlers are bound, and no runtime errors were captured
by the in-page debug collector.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8000/routing_matrix.html"


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def _wait_driver_status(base_url: str, timeout_s: float = 15.0) -> None:
    end = time.time() + timeout_s
    last_error = ""
    while time.time() < end:
        try:
            payload = _http_json("GET", f"{base_url}/status")
            value = payload.get("value", {}) if isinstance(payload, dict) else {}
            if isinstance(value, dict) and value.get("ready") is True:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"safaridriver did not become ready: {last_error}")


def _extract_session_id(create_session_response: dict[str, Any]) -> str:
    if "sessionId" in create_session_response and create_session_response["sessionId"]:
        return str(create_session_response["sessionId"])
    value = create_session_response.get("value")
    if isinstance(value, dict):
        if value.get("sessionId"):
            return str(value["sessionId"])
    raise RuntimeError(f"Unable to parse session id from response: {create_session_response}")


def _webdriver_execute(base_url: str, session_id: str, script: str) -> Any:
    payload = {
        "script": script,
        "args": [],
    }
    data = _http_json("POST", f"{base_url}/session/{session_id}/execute/sync", payload)
    value = data.get("value") if isinstance(data, dict) else None
    return value


def _webdriver_delete_session(base_url: str, session_id: str) -> None:
    try:
        _http_json("DELETE", f"{base_url}/session/{session_id}")
    except Exception:
        # Best effort cleanup.
        pass


def _start_safaridriver(port: int) -> subprocess.Popen[str]:
    cmd = ["safaridriver", "--port", str(port)]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_smoke(url: str, driver_port: int, timeout_s: float) -> int:
    base_url = f"http://127.0.0.1:{driver_port}"
    driver = _start_safaridriver(driver_port)

    try:
        _wait_driver_status(base_url)
    except Exception as exc:  # noqa: BLE001
        try:
            stderr = (driver.stderr.read() or "").strip()
        except Exception:
            stderr = ""
        driver.kill()
        if "Remote Automation" in stderr or "automation" in stderr.lower():
            print("Safari WebDriver is not fully enabled.", file=sys.stderr)
            print("Run once: safaridriver --enable", file=sys.stderr)
            print("Then enable Safari > Settings > Advanced > Show Develop menu", file=sys.stderr)
            print("And in Develop menu enable 'Allow Remote Automation'.", file=sys.stderr)
        print(f"Failed to start safaridriver: {exc}", file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        return 2

    session_id = ""
    try:
        create_session_payload = {
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "safari",
                }
            }
        }
        create_session_response = _http_json("POST", f"{base_url}/session", create_session_payload)
        session_id = _extract_session_id(create_session_response)

        _http_json("POST", f"{base_url}/session/{session_id}/url", {"url": url})

        probe_script = r"""
return (() => {
  const statusEl = document.getElementById('status');
  const projectSelect = document.getElementById('projectSelect');
  const modelSelect = document.getElementById('deviceConfigSelect');
  const patchSelect = document.getElementById('patchConfigSelect');
  const mainTab = document.getElementById('mainTabMatrix');
  const matrixRows = document.querySelectorAll('#matrixContainer table tbody tr').length;
  const debug = (window.__matrixDebug && typeof window.__matrixDebug === 'object') ? window.__matrixDebug : {};
  return {
    readyState: document.readyState,
    statusText: statusEl ? String(statusEl.textContent || '') : '',
    statusWarn: Boolean(statusEl && statusEl.classList.contains('warn')),
    matrixRows,
    projectOptions: projectSelect ? projectSelect.options.length : -1,
    modelOptions: modelSelect ? modelSelect.options.length : -1,
    patchOptions: patchSelect ? patchSelect.options.length : -1,
    hasMainTabOnclick: Boolean(mainTab && mainTab.onclick),
    runtimeErrors: Array.isArray(debug.runtime_errors) ? debug.runtime_errors.length : -1,
    unhandledRejections: Array.isArray(debug.unhandled_rejections) ? debug.unhandled_rejections.length : -1,
  };
})();
"""

        deadline = time.time() + timeout_s
        snapshot: dict[str, Any] = {}
        while time.time() < deadline:
            value = _webdriver_execute(base_url, session_id, probe_script)
            snapshot = value if isinstance(value, dict) else {}
            if (
                snapshot.get("readyState") in {"interactive", "complete"}
                and int(snapshot.get("matrixRows", 0)) > 0
                and int(snapshot.get("projectOptions", 0)) > 0
                and int(snapshot.get("modelOptions", 0)) > 0
                and int(snapshot.get("patchOptions", 0)) > 0
                and bool(snapshot.get("hasMainTabOnclick"))
            ):
                break
            time.sleep(0.35)

        print(json.dumps(snapshot, indent=2))

        failures: list[str] = []
        if int(snapshot.get("matrixRows", 0)) <= 0:
            failures.append("Matrix rows did not render")
        if int(snapshot.get("projectOptions", 0)) <= 0:
            failures.append("Project select did not populate")
        if int(snapshot.get("modelOptions", 0)) <= 0:
            failures.append("Device config select did not populate")
        if int(snapshot.get("patchOptions", 0)) <= 0:
            failures.append("Patch config select did not populate")
        if not bool(snapshot.get("hasMainTabOnclick")):
            failures.append("Main tab onclick handler is not bound")

        runtime_errors = int(snapshot.get("runtimeErrors", -1))
        rejections = int(snapshot.get("unhandledRejections", -1))
        if runtime_errors > 0:
            failures.append(f"Captured runtime errors: {runtime_errors}")
        if rejections > 0:
            failures.append(f"Captured unhandled rejections: {rejections}")

        if failures:
            print("\\nSmoke test failed:", file=sys.stderr)
            for issue in failures:
                print(f"- {issue}", file=sys.stderr)
            return 1

        print("\\nSmoke test passed.")
        return 0
    finally:
        if session_id:
            _webdriver_delete_session(base_url, session_id)
        try:
            driver.terminate()
            driver.wait(timeout=3)
        except Exception:
            driver.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safari smoke test for routing_matrix.html")
    parser.add_argument("--url", default=DEFAULT_URL, help="Target routing_matrix URL")
    parser.add_argument("--driver-port", type=int, default=5555, help="Local safaridriver port")
    parser.add_argument("--timeout", type=float, default=25.0, help="Initialization timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_smoke(url=args.url, driver_port=args.driver_port, timeout_s=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
