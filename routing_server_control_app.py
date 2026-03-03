#!/usr/bin/env python3
"""Simple desktop controller for routing_matrix_server.py."""

from __future__ import annotations

import socket
import subprocess
import sys
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


class RoutingServerControlApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Routing Server Control")
        self.root.resizable(False, False)

        self.project_root = Path(__file__).resolve().parent
        self.server_script = self.project_root / "routing_matrix_server.py"
        self.log_path = self.project_root / ".routing_matrix_server.log"

        self.process: subprocess.Popen[str] | None = None
        self.log_handle = None

        self.port_var = tk.StringVar(value="8000")
        self.status_var = tk.StringVar(value="Server stopped.")
        self.open_on_start_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._set_running_state(False)
        self._monitor_process()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frame, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_entry = ttk.Entry(frame, width=8, textvariable=self.port_var)
        self.port_entry.grid(row=0, column=1, sticky="w", padx=(6, 12))

        self.start_stop_button = ttk.Button(frame, text="Start Server", command=self._on_start_stop_clicked)
        self.start_stop_button.grid(row=0, column=2, sticky="w")

        self.open_button = ttk.Button(frame, text="Open Shell", command=self._open_shell_url)
        self.open_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.open_on_start_check = ttk.Checkbutton(
            frame,
            text="Open shell on start",
            variable=self.open_on_start_var,
        )
        self.open_on_start_check.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))

        self.status_label = ttk.Label(frame, textvariable=self.status_var)
        self.status_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _validate_port(self) -> int:
        raw = str(self.port_var.get() or "").strip()
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError("Port must be a number.") from exc
        if port < 1 or port > 65535:
            raise ValueError("Port must be between 1 and 65535.")
        return port

    @staticmethod
    def _is_port_listening(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            return sock.connect_ex((host, port)) == 0

    def _set_running_state(self, running: bool) -> None:
        self.start_stop_button.configure(text="Stop Server" if running else "Start Server")
        self.port_entry.configure(state="disabled" if running else "normal")

    def _shell_url(self, port: int | None = None) -> str:
        if port is None:
            try:
                port = self._validate_port()
            except ValueError:
                port = 8000
        return f"http://127.0.0.1:{port}/web/shell/index.html"

    def _open_shell_url(self) -> None:
        webbrowser.open(self._shell_url())

    def _start_server(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not self.server_script.exists():
            messagebox.showerror("Missing file", f"Could not find:\n{self.server_script}")
            return

        try:
            port = self._validate_port()
        except ValueError as error:
            messagebox.showerror("Invalid port", str(error))
            return

        if self._is_port_listening("127.0.0.1", port):
            self.status_var.set(f"Port {port} is already in use.")
            messagebox.showerror("Port in use", f"Port {port} is already in use.")
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("a", encoding="utf-8")
        self.log_handle.write("\n\n=== Routing server start ===\n")
        self.log_handle.flush()

        cmd = [
            sys.executable,
            str(self.server_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--root",
            str(self.project_root),
        ]
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._set_running_state(True)
        self.status_var.set(f"Server running on http://127.0.0.1:{port}")
        if self.open_on_start_var.get():
            self._open_shell_url()

    def _stop_server(self) -> None:
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        self._set_running_state(False)
        self.status_var.set("Server stopped.")

    def _on_start_stop_clicked(self) -> None:
        if self.process and self.process.poll() is None:
            self._stop_server()
        else:
            self._start_server()

    def _monitor_process(self) -> None:
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self._set_running_state(False)
            self.status_var.set(f"Server exited (code {code}).")
        self.root.after(400, self._monitor_process)

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            should_close = messagebox.askyesno(
                "Stop server?",
                "The server is still running. Stop server and close this app?",
            )
            if not should_close:
                return
            self._stop_server()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = RoutingServerControlApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

