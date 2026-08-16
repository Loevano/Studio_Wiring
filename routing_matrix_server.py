#!/usr/bin/env python3
"""Serve routing matrix files and provide save endpoints for model/connections JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from studio_wiring_schema.validation import (
    Issue,
    validate_document,
    validate_routing_against_model,
)


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def write_json_transaction(changes: list[tuple[Path, object]]) -> None:
    """Stage every payload, then replace destinations with rollback on failure."""
    staged: list[tuple[Path, Path]] = []
    originals: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    transaction_id = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        for path, payload in changes:
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            tmp_path = path.with_name(f".{path.name}.{transaction_id}.tmp")
            tmp_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            staged.append((path, tmp_path))
        for path, tmp_path in staged:
            os.replace(tmp_path, path)
            replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
    finally:
        for _path, tmp_path in staged:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


def canonical_json_hash(payload: object) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json_hash(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return canonical_json_hash(payload)


class SaveConflictError(Exception):
    def __init__(self, conflicts: dict[str, str]) -> None:
        super().__init__("Config changed on disk since load; reload and retry save.")
        self.conflicts = conflicts


class SchemaValidationError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("Save rejected because configuration validation failed.")
        self.issues = issues


def validate_save_transaction_changes(changes: dict[str, object]) -> None:
    """Reject invalid documents before conflict checks, staging, or disk writes."""
    document_kinds = {"model": "model", "connections": "patch"}
    issues: list[Issue] = []
    for change_name, document_kind in document_kinds.items():
        if change_name not in changes:
            continue
        value = changes[change_name]
        for issue in validate_document(document_kind, value):
            issues.append(
                Issue(
                    path=f"$.changes.{change_name}{issue.path[1:]}",
                    code=issue.code,
                    message=issue.message,
                    severity=issue.severity,
                )
            )
    if issues:
        raise SchemaValidationError(issues)


def validate_routing_save(routes: object, model: object) -> None:
    issues = validate_routing_against_model(routes, model)
    if issues:
        raise SchemaValidationError(issues)


def execute_json_save_transaction(
    *,
    requested: list[tuple[str, Path, object]],
    expected_hashes: dict[str, object],
    lock: threading.Lock,
    regenerate: bool,
    regenerate_callback: Callable[[], tuple[bool, str]],
    after_write: Callable[[], None] | None = None,
) -> tuple[dict[str, str], bool, str]:
    """Conflict-check, commit, and optionally regenerate under one lock."""
    with lock:
        conflicts: dict[str, str] = {}
        for label, path, _value in requested:
            if label not in expected_hashes:
                raise ValueError(f"expected_hashes.{label} is required")
            expected_hash = str(expected_hashes.get(label) or "").strip().lower()
            current_hash = read_json_hash(path) if path.exists() else ""
            if expected_hash != current_hash:
                conflicts[label] = current_hash
        if conflicts:
            raise SaveConflictError(conflicts)

        write_json_transaction([(path, value) for _label, path, value in requested])
        if callable(after_write):
            after_write()
        saved_hashes = {
            label: canonical_json_hash(value) for label, _path, value in requested
        }
        regenerate_ok = True
        regenerate_message = ""
        if regenerate:
            try:
                regenerate_ok, regenerate_message = regenerate_callback()
            except Exception as exc:
                regenerate_ok = False
                regenerate_message = f"Regenerate failed after save: {exc}"
    return saved_hashes, regenerate_ok, regenerate_message


def slugify_project_key(name: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return token or "project"


def make_unique_project_key(projects_root: Path, requested_name: str) -> str:
    base = slugify_project_key(requested_name)
    candidate = base
    index = 2
    while (projects_root / candidate).exists():
        candidate = f"{base}-{index:02d}"
        index += 1
    return candidate


def to_posix_rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace(os.sep, "/")


def resolve_path_within_root(root: Path, raw: object) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Path is required")
    path = Path(value)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(f"Path must be inside root: {value}")
    return resolved


def resolve_path_within_project(project_root: Path, raw: object) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Path is required")
    path = Path(value)
    resolved = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    if project_root not in resolved.parents and resolved != project_root:
        raise ValueError(f"Path must be inside project: {value}")
    return resolved


def discover_projects(
    root: Path,
    active_model: Path,
    active_connections: Path,
) -> dict[str, object]:
    projects_root = root / "projects"
    items: list[dict[str, object]] = []
    active_key = ""
    if not projects_root.exists():
        return {"projects": [], "active_project_key": active_key}

    for project_dir in sorted(
        [entry for entry in projects_root.iterdir() if entry.is_dir() and not entry.name.startswith(".")],
        key=lambda p: p.name.lower(),
    ):
        if project_dir.name.startswith("_"):
            continue
        project_meta_path = project_dir / "project.json"
        has_project_marker = (
            project_meta_path.exists()
            or (project_dir / "device-configurations").exists()
            or (project_dir / "patch-configurations").exists()
            or (project_dir / "routing-configurations").exists()
        )
        if not has_project_marker:
            continue
        project_meta: dict[str, object] = {}
        if project_meta_path.exists():
            try:
                loaded = json.loads(project_meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    project_meta = loaded
            except Exception:
                project_meta = {}

        project_name = str(project_meta.get("name") or project_dir.name).strip() or project_dir.name
        paths_meta = project_meta.get("paths")
        paths_meta = paths_meta if isinstance(paths_meta, dict) else {}
        device_patch_map_meta = project_meta.get("device_patch_map")
        device_patch_map_meta = device_patch_map_meta if isinstance(device_patch_map_meta, dict) else {}
        device_routing_map_meta = project_meta.get("device_routing_map")
        device_routing_map_meta = (
            device_routing_map_meta if isinstance(device_routing_map_meta, dict) else {}
        )

        device_dir = project_dir / "device-configurations"
        patch_dir = project_dir / "patch-configurations"
        routing_dir = project_dir / "routing-configurations"
        output_dir = project_dir / "outputs"
        output_html_dir = output_dir / "html"
        output_svg_dir = output_dir / "svgs"
        output_debug_dir = output_dir / "debug"

        device_files = sorted(
            [
                p for p in device_dir.glob("*.json")
                if p.is_file()
                and "routing-rules" not in p.name.lower()
                and "routing_rules" not in p.name.lower()
            ],
            key=lambda p: p.name.lower(),
        )
        patch_files = sorted(
            [p for p in patch_dir.rglob("*.json") if p.is_file()],
            key=lambda p: str(p.relative_to(patch_dir)).replace(os.sep, "/").lower(),
        )
        routing_files = sorted(
            [p for p in routing_dir.rglob("*.json") if p.is_file()],
            key=lambda p: str(p.relative_to(routing_dir)).replace(os.sep, "/").lower(),
        )

        default_model_path = None
        default_patch_path = None
        default_routing_path = None
        if paths_meta:
            if "device_model" in paths_meta:
                try:
                    default_model_path = resolve_path_within_root(project_dir, paths_meta.get("device_model"))
                except Exception:
                    default_model_path = None
            if "default_patch" in paths_meta:
                try:
                    default_patch_path = resolve_path_within_root(project_dir, paths_meta.get("default_patch"))
                except Exception:
                    default_patch_path = None
            if "default_routing" in paths_meta:
                try:
                    default_routing_path = resolve_path_within_root(
                        project_dir, paths_meta.get("default_routing")
                    )
                except Exception:
                    default_routing_path = None

        default_model_path = default_model_path if default_model_path and default_model_path.exists() else (device_files[0] if device_files else None)
        default_patch_path = default_patch_path if default_patch_path and default_patch_path.exists() else (patch_files[0] if patch_files else None)
        default_routing_path = (
            default_routing_path
            if default_routing_path and default_routing_path.exists()
            else (routing_files[0] if routing_files else None)
        )

        device_file_set = {path.resolve() for path in device_files}
        patch_file_set = {path.resolve() for path in patch_files}
        routing_file_set = {path.resolve() for path in routing_files}
        normalized_device_patch_map: dict[str, list[str]] = {}
        for raw_device_path, raw_patch_paths in device_patch_map_meta.items():
            try:
                resolved_device = resolve_path_within_project(project_dir, raw_device_path)
            except Exception:
                continue
            if resolved_device not in device_file_set:
                continue
            patch_values: list[str] = []
            for raw_patch_path in raw_patch_paths if isinstance(raw_patch_paths, list) else []:
                try:
                    resolved_patch = resolve_path_within_project(project_dir, raw_patch_path)
                except Exception:
                    continue
                if resolved_patch not in patch_file_set:
                    continue
                rel = to_posix_rel(resolved_patch, root)
                if rel not in patch_values:
                    patch_values.append(rel)
            normalized_device_patch_map[to_posix_rel(resolved_device, root)] = patch_values

        normalized_device_routing_map: dict[str, list[str]] = {}
        for raw_device_path, raw_routing_paths in device_routing_map_meta.items():
            try:
                resolved_device = resolve_path_within_project(project_dir, raw_device_path)
            except Exception:
                continue
            if resolved_device not in device_file_set:
                continue
            routing_values: list[str] = []
            for raw_routing_path in (
                raw_routing_paths if isinstance(raw_routing_paths, list) else []
            ):
                try:
                    resolved_routing = resolve_path_within_project(
                        project_dir, raw_routing_path
                    )
                except Exception:
                    continue
                if resolved_routing not in routing_file_set:
                    continue
                rel = to_posix_rel(resolved_routing, root)
                if rel not in routing_values:
                    routing_values.append(rel)
            normalized_device_routing_map[to_posix_rel(resolved_device, root)] = routing_values

        project_item = {
            "key": project_dir.name,
            "name": project_name,
            "base_path": str(project_dir.relative_to(root)).replace(os.sep, "/"),
            "project_file": to_posix_rel(project_meta_path, root),
            "device_configs": [str(path.relative_to(root)).replace(os.sep, "/") for path in device_files],
            "patch_configs": [str(path.relative_to(root)).replace(os.sep, "/") for path in patch_files],
            "routing_configs": [
                str(path.relative_to(root)).replace(os.sep, "/") for path in routing_files
            ],
            "default_device_config": str(default_model_path.relative_to(root)).replace(os.sep, "/")
            if default_model_path else "",
            "default_patch_config": str(default_patch_path.relative_to(root)).replace(os.sep, "/")
            if default_patch_path else "",
            "default_routing_config": str(default_routing_path.relative_to(root)).replace(os.sep, "/")
            if default_routing_path else "",
            "output_html_directory": str(output_html_dir.relative_to(root)).replace(os.sep, "/")
            if output_html_dir.exists() else "",
            "output_svg_directory": str(output_svg_dir.relative_to(root)).replace(os.sep, "/")
            if output_svg_dir.exists() else "",
            "output_debug_directory": str(output_debug_dir.relative_to(root)).replace(os.sep, "/")
            if output_debug_dir.exists() else "",
            "device_patch_map": normalized_device_patch_map,
            "device_routing_map": normalized_device_routing_map,
        }
        items.append(project_item)

        device_resolved = [(root / rel).resolve() for rel in project_item["device_configs"]]
        patch_resolved = [(root / rel).resolve() for rel in project_item["patch_configs"]]
        active_hit = (
            active_model.resolve() in device_resolved
            or active_connections.resolve() in patch_resolved
            or str(active_model).startswith(str(project_dir))
            or str(active_connections).startswith(str(project_dir))
        )
        if active_hit:
            active_key = project_dir.name

    return {"projects": items, "active_project_key": active_key}


def find_project_item(projects_payload: dict[str, object], project_key: str) -> dict[str, object] | None:
    key = str(project_key or "").strip()
    if not key:
        return None
    for item in projects_payload.get("projects", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("key") or "").strip() == key:
            return item
    return None


def ensure_project_from_template(root: Path, project_name: str) -> str:
    name = str(project_name or "").strip()
    if not name:
        raise ValueError("Project name is required")
    projects_root = root / "projects"
    template_root = projects_root / "_template"
    if not template_root.exists() or not template_root.is_dir():
        raise ValueError("Project template folder not found: projects/_template")

    project_key = make_unique_project_key(projects_root, name)
    project_root = projects_root / project_key
    shutil.copytree(template_root, project_root)

    project_meta_path = project_root / "project.json"
    project_meta: dict[str, object] = {}
    if project_meta_path.exists():
        try:
            loaded = json.loads(project_meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                project_meta = loaded
        except Exception:
            project_meta = {}
    project_meta["version"] = int(project_meta.get("version") or 1)
    project_meta["name"] = name
    if not str(project_meta.get("description") or "").strip():
        project_meta["description"] = "Studio project"
    project_meta.setdefault("paths", {})
    if not isinstance(project_meta["paths"], dict):
        project_meta["paths"] = {}
    project_meta.setdefault("device_patch_map", {})
    write_json_atomic(project_meta_path, project_meta)
    return project_key


def routing_endpoints_from_model(model: object) -> list[dict[str, object]]:
    """Return logical endpoints; physical `ports` are deliberately ignored."""
    endpoints: list[dict[str, object]] = []
    devices = model.get("devices") if isinstance(model, dict) else None
    if not isinstance(devices, list):
        return endpoints
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_name = str(device.get("name") or "").strip()
        routing_ports = device.get("routing_ports")
        if not device_name or not isinstance(routing_ports, list):
            continue
        for port in routing_ports:
            if not isinstance(port, dict):
                continue
            port_name = str(port.get("name") or "").strip()
            direction = str(port.get("direction") or "").strip().lower()
            if not port_name or direction not in {"in", "out", "io"}:
                continue
            endpoint: dict[str, object] = {
                "id": f"{device_name}::{port_name}",
                "device": device_name,
                "port": port_name,
                "direction": direction,
                "transport": str(port.get("transport") or "").strip(),
                "enabled": port.get("enabled") is not False,
            }
            for optional in ("channel", "group", "order"):
                if optional in port:
                    endpoint[optional] = port[optional]
            endpoints.append(endpoint)
    return endpoints


def ensure_global_routing_rules(root: Path, target_path: Path) -> None:
    if target_path.exists():
        return
    candidates = [
        root / "json/routing_rules.json",
        root / "json/routing-rules.json",
        root / "defaults/default_template/routing-rules.json",
        root / "projects/studio-sidecar/device-configurations/routing-rules.json",
        root / "projects/_template/device-configurations/routing-rules.json",
    ]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        try:
            if candidate.resolve() == target_path.resolve():
                continue
        except Exception:
            pass
        if candidate.exists() and candidate.is_file():
            shutil.copyfile(candidate, target_path)
            return
    write_json_atomic(
        target_path,
        {
            "version": 1,
            "labels": {
                "source_side": "above",
                "destination_side": "below",
                "font_size": 7.0,
            },
            "routing": {
                "fifo_forward_turns": True,
                "backward_out_to_in_wrap": "below",
                "video_early_turn": True,
                "video_vertical_rows_threshold": 6.0,
            },
        },
    )


class RoutingMatrixHandler(SimpleHTTPRequestHandler):
    model_path: Path
    connections_path: Path
    routing_rules_path: Path
    route_debug_path: Path
    root_path: Path
    generator_script: Path
    preview_html_path: Path
    preview_svg_dir: Path
    auto_regenerate: bool = True
    targets_selected: bool = False
    watch_interval_seconds: float = 1.0
    watch_debounce_seconds: float = 0.75
    _regenerate_lock = threading.Lock()
    _save_transaction_lock = threading.Lock()
    _watcher_lock = threading.Lock()
    _watcher_stop_event = threading.Event()
    _watcher_thread: threading.Thread | None = None
    _watch_baseline_signature: tuple[tuple[str, int, int], ...] | None = None

    @classmethod
    def _active_watch_paths(cls) -> tuple[Path, Path, Path]:
        with cls._watcher_lock:
            return (cls.model_path, cls.connections_path, cls.routing_rules_path)

    @classmethod
    def _watch_signature(cls) -> tuple[tuple[str, int, int], ...]:
        signature: list[tuple[str, int, int]] = []
        for path in cls._active_watch_paths():
            try:
                stat = path.stat()
                signature.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
            except Exception:
                signature.append((str(path), 0, -1))
        return tuple(signature)

    @classmethod
    def _run_regenerate_command_for_targets(
        cls,
        *,
        model_path: Path,
        connections_path: Path,
        routing_rules_path: Path,
        route_debug_path: Path,
        preview_html_path: Path,
        preview_svg_dir: Path,
    ) -> tuple[bool, str]:
        if not cls.generator_script.exists():
            return False, f"Generator script not found: {cls.generator_script}"
        # Guardrail: regenerate is allowed to update visual outputs, not the
        # active device/patch config JSON files.
        protected_before: dict[Path, bytes | None] = {}
        for protected_path in (model_path, connections_path):
            try:
                protected_before[protected_path] = protected_path.read_bytes()
            except Exception:
                protected_before[protected_path] = None

        command = [
            sys.executable,
            str(cls.generator_script),
            "--model",
            str(model_path),
            "--connections-json",
            str(connections_path),
            "--routing-rules",
            str(routing_rules_path),
            "--output",
            str(preview_html_path),
            "--svg-dir",
            str(preview_svg_dir),
            "--debug-routes-json",
            str(route_debug_path),
            "--show-power",
        ]
        completed = subprocess.run(
            command,
            cwd=str(cls.root_path),
            capture_output=True,
            text=True,
            check=False,
        )
        modified_protected: list[str] = []
        for protected_path, before_bytes in protected_before.items():
            try:
                after_bytes = protected_path.read_bytes()
            except Exception:
                after_bytes = None
            if before_bytes == after_bytes:
                continue
            modified_protected.append(str(protected_path))
            if before_bytes is None:
                try:
                    protected_path.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                try:
                    protected_path.parent.mkdir(parents=True, exist_ok=True)
                    protected_path.write_bytes(before_bytes)
                except Exception:
                    pass

        if modified_protected:
            return (
                False,
                "Regenerate attempted to modify protected config file(s); "
                f"changes were reverted: {', '.join(modified_protected)}",
            )

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            return False, f"Regenerate command failed ({completed.returncode}): {details}"
        return True, (completed.stdout or "").strip()

    @classmethod
    def _run_regenerate_command(cls) -> tuple[bool, str]:
        return cls._run_regenerate_command_for_targets(
            model_path=cls.model_path,
            connections_path=cls.connections_path,
            routing_rules_path=cls.routing_rules_path,
            route_debug_path=cls.route_debug_path,
            preview_html_path=cls.preview_html_path,
            preview_svg_dir=cls.preview_svg_dir,
        )

    @classmethod
    def trigger_regenerate_for_targets(
        cls,
        *,
        model_path: Path,
        connections_path: Path,
        routing_rules_path: Path,
        route_debug_path: Path,
        preview_html_path: Path,
        preview_svg_dir: Path,
        reason: str,
    ) -> tuple[bool, str]:
        with cls._regenerate_lock:
            ok, message = cls._run_regenerate_command_for_targets(
                model_path=model_path,
                connections_path=connections_path,
                routing_rules_path=routing_rules_path,
                route_debug_path=route_debug_path,
                preview_html_path=preview_html_path,
                preview_svg_dir=preview_svg_dir,
            )
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        target = to_posix_rel(model_path, cls.root_path)
        if ok:
            print(f"[{stamp}] Regenerated visuals ({reason}; {target}).")
        else:
            print(f"[{stamp}] Regenerate failed ({reason}; {target}): {message}")
        return ok, message

    @classmethod
    def refresh_watch_baseline_for_targets(
        cls,
        *,
        model_path: Path,
        connections_path: Path,
    ) -> None:
        active_model, active_connections, _routing_rules = cls._active_watch_paths()
        if model_path.resolve() != active_model.resolve():
            return
        if connections_path.resolve() != active_connections.resolve():
            return
        baseline = cls._watch_signature()
        with cls._watcher_lock:
            cls._watch_baseline_signature = baseline

    @classmethod
    def trigger_regenerate(cls, reason: str = "manual") -> tuple[bool, str]:
        with cls._regenerate_lock:
            ok, message = cls._run_regenerate_command()
        if ok:
            baseline = cls._watch_signature()
            with cls._watcher_lock:
                cls._watch_baseline_signature = baseline
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            print(f"[{stamp}] Regenerated visuals ({reason}).")
        else:
            print(f"[{stamp}] Regenerate failed ({reason}): {message}")
        return ok, message

    @classmethod
    def _watcher_loop(cls) -> None:
        pending_signature: tuple[tuple[str, int, int], ...] | None = None
        pending_started = 0.0
        while not cls._watcher_stop_event.wait(max(0.2, float(cls.watch_interval_seconds))):
            if not cls.auto_regenerate:
                continue
            with cls._watcher_lock:
                last_signature = cls._watch_baseline_signature
            current_signature = cls._watch_signature()
            if last_signature is None:
                with cls._watcher_lock:
                    cls._watch_baseline_signature = current_signature
                continue
            if current_signature == last_signature:
                pending_signature = None
                pending_started = 0.0
                continue
            now = time.monotonic()
            if pending_signature != current_signature:
                pending_signature = current_signature
                pending_started = now
                continue
            if (now - pending_started) < max(0.0, float(cls.watch_debounce_seconds)):
                continue
            cls.trigger_regenerate(reason="watch-file-change")
            with cls._watcher_lock:
                cls._watch_baseline_signature = current_signature
            pending_signature = None
            pending_started = 0.0

    @classmethod
    def start_auto_regen_watcher(cls) -> None:
        if not cls.auto_regenerate:
            return
        baseline = cls._watch_signature()
        with cls._watcher_lock:
            if cls._watcher_thread and cls._watcher_thread.is_alive():
                return
            cls._watcher_stop_event.clear()
            cls._watch_baseline_signature = baseline
            cls._watcher_thread = threading.Thread(
                target=cls._watcher_loop,
                name="routing-matrix-auto-regen",
                daemon=True,
            )
            cls._watcher_thread.start()

    @classmethod
    def stop_auto_regen_watcher(cls) -> None:
        with cls._watcher_lock:
            thread = cls._watcher_thread
            cls._watcher_stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def _resolve_target_path(self, raw: object, *, expect_dir: bool = False) -> Path:
        resolved = resolve_path_within_root(self.root_path, raw)
        if expect_dir:
            if resolved.exists() and not resolved.is_dir():
                raise ValueError(f"Expected directory path: {raw}")
        else:
            if resolved.exists() and resolved.is_dir():
                raise ValueError(f"Expected file path: {raw}")
        return resolved

    def _send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root_path)).replace(os.sep, "/")
        except Exception:
            return str(path)

    def _preview_paths_payload(self) -> dict[str, str]:
        svg_dir_rel = self._relative_path(self.preview_svg_dir).rstrip("/")
        return {
            "audioAnalog": f"{svg_dir_rel}/audio-analog.svg",
            "computerData": f"{svg_dir_rel}/computer-data.svg",
            "digitalAudio": f"{svg_dir_rel}/digital-audio.svg",
            "network": f"{svg_dir_rel}/network.svg",
            "power": f"{svg_dir_rel}/power.svg",
            "allConnections": f"{svg_dir_rel}/all-connections.svg",
        }

    def _projects_payload(self) -> dict[str, object]:
        payload = discover_projects(
            root=self.root_path,
            active_model=self.model_path,
            active_connections=self.connections_path,
        )
        if not bool(self.targets_selected):
            payload["active_project_key"] = ""
        return payload

    def _routing_targets(
        self,
        *,
        project_key: object,
        model_path: object = "",
        routing_path: object = "",
    ) -> tuple[dict[str, object], Path, Path]:
        projects_payload = self._projects_payload()
        project_item = find_project_item(projects_payload, str(project_key or ""))
        if project_item is None:
            raise ValueError(f"Project not found: {project_key}")
        project_dir = self._project_dir_from_key(project_key)

        model_rel = str(model_path or project_item.get("default_device_config") or "").strip()
        routing_rel = str(
            routing_path or project_item.get("default_routing_config") or ""
        ).strip()
        if not model_rel:
            raise ValueError("model_path is required")
        if not routing_rel:
            raise ValueError("routing_path is required")
        if Path(model_rel).is_absolute() or Path(routing_rel).is_absolute():
            raise ValueError("Routing targets must be root-relative paths")
        model_target = resolve_path_within_root(self.root_path, model_rel)
        routing_target = resolve_path_within_root(self.root_path, routing_rel)
        for label, target in (
            ("model_path", model_target),
            ("routing_path", routing_target),
        ):
            if project_dir not in target.parents:
                raise ValueError(f"{label} must be inside selected project")
            if target.suffix.lower() != ".json":
                raise ValueError(f"{label} must be a .json file")
            if target.exists() and not target.is_file():
                raise ValueError(f"{label} must be a file path")

        allowed_models = set(project_item.get("device_configs") or [])
        allowed_routing = set(project_item.get("routing_configs") or [])
        canonical_model_rel = self._relative_path(model_target)
        canonical_routing_rel = self._relative_path(routing_target)
        if canonical_model_rel not in allowed_models:
            raise ValueError("model_path is not a discovered device configuration")
        if canonical_routing_rel not in allowed_routing:
            raise ValueError("routing_path is not a discovered routing configuration")
        routing_map = project_item.get("device_routing_map")
        if isinstance(routing_map, dict):
            mapped = routing_map.get(canonical_model_rel)
            if isinstance(mapped, list) and canonical_routing_rel not in mapped:
                raise ValueError("routing_path is not assigned to the selected device model")
        return project_item, model_target, routing_target

    def _project_dir_from_key(self, project_key: object) -> Path:
        key = str(project_key or "").strip()
        if not key:
            raise ValueError("Project key is required")
        projects_root = (self.root_path / "projects").resolve()
        project_dir = (projects_root / key).resolve()
        if projects_root not in project_dir.parents:
            raise ValueError("Project path is outside projects directory")
        if not project_dir.exists() or not project_dir.is_dir():
            raise ValueError(f"Project not found: {key}")
        return project_dir

    def _load_project_meta(self, project_dir: Path) -> tuple[Path, dict[str, object]]:
        meta_path = project_dir / "project.json"
        payload: dict[str, object] = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = {}
        payload.setdefault("version", 1)
        payload.setdefault("name", project_dir.name)
        payload.setdefault("paths", {})
        if not isinstance(payload["paths"], dict):
            payload["paths"] = {}
        return meta_path, payload

    def _transaction_targets(self, payload: dict[str, object]) -> dict[str, Path]:
        project_dir = self._project_dir_from_key(payload.get("project_key"))
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, dict):
            raise ValueError("targets must be an object")

        def relative_target(key: str) -> object:
            raw = raw_targets.get(key)
            value = str(raw or "").strip()
            if not value:
                raise ValueError(f"targets.{key} is required")
            if Path(value).is_absolute() or "://" in value:
                raise ValueError(f"{key} must be a root-relative path")
            return raw

        def project_file(key: str) -> Path:
            resolved = resolve_path_within_root(self.root_path, relative_target(key))
            if project_dir not in resolved.parents:
                raise ValueError(f"{key} must be inside selected project")
            if resolved.exists() and resolved.is_dir():
                raise ValueError(f"{key} must be a file path")
            return resolved

        def project_dir_path(key: str) -> Path:
            resolved = resolve_path_within_root(self.root_path, relative_target(key))
            if project_dir not in resolved.parents and resolved != project_dir:
                raise ValueError(f"{key} must be inside selected project")
            if resolved.exists() and not resolved.is_dir():
                raise ValueError(f"{key} must be a directory path")
            return resolved

        model_path = project_file("model_path")
        connections_path = project_file("connections_path")
        route_debug_path = project_file("route_debug_path")
        preview_html_path = project_file("preview_html")
        preview_svg_dir = project_dir_path("preview_svg_dir")
        routing_rules_path = resolve_path_within_root(
            self.root_path,
            relative_target("routing_rules_path"),
        )
        if routing_rules_path.exists() and routing_rules_path.is_dir():
            raise ValueError("routing_rules_path must be a file path")
        for label, path in (("model_path", model_path), ("connections_path", connections_path)):
            if path.suffix.lower() != ".json":
                raise ValueError(f"{label} must be a .json file")
        return {
            "model_path": model_path,
            "connections_path": connections_path,
            "routing_rules_path": routing_rules_path,
            "route_debug_path": route_debug_path,
            "preview_html_path": preview_html_path,
            "preview_svg_dir": preview_svg_dir,
        }

    def _transaction_preview_payload(self, targets: dict[str, Path]) -> dict[str, object]:
        svg_dir_rel = self._relative_path(targets["preview_svg_dir"]).rstrip("/")
        return {
            "preview_html": self._relative_path(targets["preview_html_path"]),
            "preview_svg_dir": svg_dir_rel,
            "preview_paths": {
                "audioAnalog": f"{svg_dir_rel}/audio-analog.svg",
                "computerData": f"{svg_dir_rel}/computer-data.svg",
                "digitalAudio": f"{svg_dir_rel}/digital-audio.svg",
                "network": f"{svg_dir_rel}/network.svg",
                "power": f"{svg_dir_rel}/power.svg",
                "allConnections": f"{svg_dir_rel}/all-connections.svg",
            },
            "route_debug_path": self._relative_path(targets["route_debug_path"]),
        }

    def _config_payload(self) -> dict[str, object]:
        projects_payload = self._projects_payload()
        expose_targets = bool(self.targets_selected)
        payload: dict[str, object] = {
            "ok": True,
            "model_path": self._relative_path(self.model_path) if expose_targets else "",
            "connections_path": self._relative_path(self.connections_path) if expose_targets else "",
            "routing_rules_path": self._relative_path(self.routing_rules_path),
            "route_debug_path": self._relative_path(self.route_debug_path),
            "save_transaction_available": True,
            "regenerate_available": self.generator_script.exists(),
            "preview_html": self._relative_path(self.preview_html_path),
            "preview_svg_dir": self._relative_path(self.preview_svg_dir),
            "preview_paths": self._preview_paths_payload(),
            "auto_regenerate": bool(self.auto_regenerate),
            "watch_interval_seconds": float(self.watch_interval_seconds),
            "watch_debounce_seconds": float(self.watch_debounce_seconds),
            "projects": projects_payload.get("projects", []),
            "active_project_key": projects_payload.get("active_project_key", "") if expose_targets else "",
        }
        if expose_targets:
            active = find_project_item(
                projects_payload,
                str(projects_payload.get("active_project_key") or ""),
            )
            if active is not None:
                model_rel = self._relative_path(self.model_path)
                routing_rel = ""
                route_map = active.get("device_routing_map")
                if isinstance(route_map, dict):
                    candidates = route_map.get(model_rel)
                    if isinstance(candidates, list) and candidates:
                        routing_rel = str(candidates[0])
                if not routing_rel:
                    routing_rel = str(active.get("default_routing_config") or "")
                routing_target = (
                    resolve_path_within_root(self.root_path, routing_rel)
                    if routing_rel
                    else None
                )
                payload.update(
                    {
                        "routing_path": routing_rel,
                        "routing_hash": read_json_hash(routing_target)
                        if routing_target and routing_target.exists()
                        else "",
                        "model_hash": read_json_hash(self.model_path)
                        if self.model_path.exists()
                        else "",
                    }
                )
        return payload

    def _read_json_body(self) -> object:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0:
            raise ValueError("Missing request body")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("Invalid JSON body") from exc

    def _discard_request_body(self) -> None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path == "/api/config":
            self._send_json(self._config_payload())
            return
        if request.path == "/api/projects":
            self._send_json({"ok": True, **self._projects_payload()})
            return
        if request.path == "/api/routing":
            try:
                query = parse_qs(request.query, keep_blank_values=True)
                first = lambda key: query.get(key, [""])[0]
                project_item, model_path, routing_path = self._routing_targets(
                    project_key=first("project_key"),
                    model_path=first("model_path"),
                    routing_path=first("routing_path"),
                )
                model = json.loads(model_path.read_text(encoding="utf-8"))
                routes = json.loads(routing_path.read_text(encoding="utf-8"))
                try:
                    validate_routing_save(routes, model)
                except SchemaValidationError as validation_error:
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(validation_error),
                            "validation_issues": [
                                {
                                    "severity": issue.severity,
                                    "path": issue.path,
                                    "code": issue.code,
                                    "message": issue.message,
                                }
                                for issue in validation_error.issues
                            ],
                        },
                        status=HTTPStatus.UNPROCESSABLE_ENTITY,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "project_key": project_item.get("key", ""),
                        "model_path": self._relative_path(model_path),
                        "routing_path": self._relative_path(routing_path),
                        "model_hash": canonical_json_hash(model),
                        "routing_hash": canonical_json_hash(routes),
                        "endpoints": routing_endpoints_from_model(model),
                        "routes": routes,
                        "document": routes,
                    }
                )
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {
            "/api/save-transaction",
            "/api/save-model",
            "/api/save-connections",
            "/api/regenerate",
            "/api/set-targets",
            "/api/create-project",
            "/api/save-project",
            "/api/save-device-template",
            "/api/save-routing",
        }:
            self._send_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            if self.path == "/api/save-routing":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")
                _project_item, model_path, routing_path = self._routing_targets(
                    project_key=payload.get("project_key"),
                    model_path=payload.get("model_path"),
                    routing_path=payload.get("routing_path"),
                )
                routes = payload.get("routes")
                if not isinstance(routes, dict):
                    raise ValueError("routes must be a routing document object")
                expected_hash = str(payload.get("expected_hash") or "").strip().lower()
                if "expected_hash" not in payload:
                    raise ValueError("expected_hash is required")
                try:
                    model = json.loads(model_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise ValueError(f"Invalid model JSON: {self._relative_path(model_path)}") from exc
                try:
                    validate_routing_save(routes, model)
                except SchemaValidationError as validation_error:
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(validation_error),
                            "validation_issues": [
                                {
                                    "severity": issue.severity,
                                    "path": issue.path,
                                    "code": issue.code,
                                    "message": issue.message,
                                }
                                for issue in validation_error.issues
                            ],
                        },
                        status=HTTPStatus.UNPROCESSABLE_ENTITY,
                    )
                    return
                try:
                    saved_hashes, _regenerate_ok, _regenerate_message = (
                        execute_json_save_transaction(
                            requested=[("routing", routing_path, routes)],
                            expected_hashes={"routing": expected_hash},
                            lock=self.__class__._save_transaction_lock,
                            regenerate=False,
                            regenerate_callback=lambda: (True, ""),
                        )
                    )
                except SaveConflictError as conflict_error:
                    current_hash = conflict_error.conflicts.get("routing", "")
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(conflict_error),
                            "conflict": "routing",
                            "current_hash": current_hash,
                            "current_hashes": conflict_error.conflicts,
                        },
                        status=HTTPStatus.CONFLICT,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "saved": {
                            "path": self._relative_path(routing_path),
                            "hash": saved_hashes["routing"],
                        },
                        "regeneration": {"attempted": False},
                    }
                )
                return

            if self.path == "/api/save-transaction":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")
                targets = self._transaction_targets(payload)
                changes_raw = payload.get("changes")
                if not isinstance(changes_raw, dict):
                    raise ValueError("changes must be an object")
                expected_raw = payload.get("expected_hashes")
                if not isinstance(expected_raw, dict):
                    raise ValueError("expected_hashes must be an object")

                requested: list[tuple[str, Path, object]] = []
                if "model" in changes_raw:
                    if not isinstance(changes_raw.get("model"), dict):
                        raise ValueError("changes.model must be an object")
                    requested.append(("model", targets["model_path"], changes_raw["model"]))
                if "connections" in changes_raw:
                    if not isinstance(changes_raw.get("connections"), dict):
                        raise ValueError("changes.connections must be an object")
                    requested.append(
                        ("connections", targets["connections_path"], changes_raw["connections"])
                    )
                if not requested:
                    raise ValueError("At least one of changes.model or changes.connections is required")
                try:
                    validate_save_transaction_changes(changes_raw)
                except SchemaValidationError as validation_error:
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(validation_error),
                            "validation_issues": [
                                {
                                    "severity": issue.severity,
                                    "path": issue.path,
                                    "code": issue.code,
                                    "message": issue.message,
                                }
                                for issue in validation_error.issues
                            ],
                        },
                        status=HTTPStatus.UNPROCESSABLE_ENTITY,
                    )
                    return

                regenerate_requested = bool(
                    payload.get("regenerate", self.__class__.auto_regenerate)
                )

                def refresh_watcher() -> None:
                    self.__class__.refresh_watch_baseline_for_targets(
                        model_path=targets["model_path"],
                        connections_path=targets["connections_path"],
                    )

                def regenerate() -> tuple[bool, str]:
                    return self.__class__.trigger_regenerate_for_targets(
                        model_path=targets["model_path"],
                        connections_path=targets["connections_path"],
                        routing_rules_path=targets["routing_rules_path"],
                        route_debug_path=targets["route_debug_path"],
                        preview_html_path=targets["preview_html_path"],
                        preview_svg_dir=targets["preview_svg_dir"],
                        reason=str(payload.get("reason") or "save-transaction"),
                    )

                try:
                    saved_hashes, regenerate_ok, regenerate_message = execute_json_save_transaction(
                        requested=requested,
                        expected_hashes=expected_raw,
                        lock=self.__class__._save_transaction_lock,
                        regenerate=regenerate_requested,
                        regenerate_callback=regenerate,
                        after_write=refresh_watcher,
                    )
                except SaveConflictError as conflict_error:
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(conflict_error),
                            "conflict": sorted(conflict_error.conflicts),
                            "current_hashes": conflict_error.conflicts,
                        },
                        status=HTTPStatus.CONFLICT,
                    )
                    return

                self._send_json(
                    {
                        "ok": True,
                        "saved": {
                            "model": "model" in saved_hashes,
                            "connections": "connections" in saved_hashes,
                            "hashes": saved_hashes,
                            "paths": {
                                label: self._relative_path(path)
                                for label, path, _value in requested
                            },
                        },
                        "regeneration": {
                            "attempted": regenerate_requested,
                            "ok": regenerate_ok,
                            "error": "" if regenerate_ok else regenerate_message,
                            "output": regenerate_message if regenerate_ok else "",
                        },
                        **self._transaction_preview_payload(targets),
                    }
                )
                return

            if self.path == "/api/create-project":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")
                project_name = str(payload.get("name") or "").strip()
                project_key = ensure_project_from_template(self.root_path, project_name)
                self._send_json({"ok": True, "created_project_key": project_key, **self._config_payload()})
                return

            if self.path == "/api/save-project":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")
                project_key = str(payload.get("project_key") or "").strip()
                project_dir = self._project_dir_from_key(project_key)
                project_meta_path, project_meta = self._load_project_meta(project_dir)
                project_paths = project_meta.get("paths")
                if not isinstance(project_paths, dict):
                    project_paths = {}
                    project_meta["paths"] = project_paths

                project_name = str(payload.get("name") or "").strip()
                if project_name:
                    project_meta["name"] = project_name

                def to_project_rel(path_value: object) -> str:
                    resolved = resolve_path_within_root(self.root_path, path_value)
                    if project_dir not in resolved.parents and resolved != project_dir:
                        raise ValueError(f"Path must be inside project: {path_value}")
                    return str(resolved.relative_to(project_dir)).replace(os.sep, "/")

                if "default_device_config" in payload and str(payload.get("default_device_config") or "").strip():
                    project_paths["device_model"] = to_project_rel(payload.get("default_device_config"))
                if "default_patch_config" in payload and str(payload.get("default_patch_config") or "").strip():
                    project_paths["default_patch"] = to_project_rel(payload.get("default_patch_config"))
                if "default_routing_config" in payload and str(payload.get("default_routing_config") or "").strip():
                    project_paths["default_routing"] = to_project_rel(
                        payload.get("default_routing_config")
                    )

                device_patch_map_raw = payload.get("device_patch_map")
                if device_patch_map_raw is not None:
                    if not isinstance(device_patch_map_raw, dict):
                        raise ValueError("device_patch_map must be an object")
                    normalized_map: dict[str, list[str]] = {}
                    for raw_device_path, raw_patch_list in device_patch_map_raw.items():
                        device_rel = to_project_rel(raw_device_path)
                        if not isinstance(raw_patch_list, list):
                            continue
                        normalized_patch_list: list[str] = []
                        for raw_patch_path in raw_patch_list:
                            patch_rel = to_project_rel(raw_patch_path)
                            if patch_rel not in normalized_patch_list:
                                normalized_patch_list.append(patch_rel)
                        normalized_map[device_rel] = normalized_patch_list
                    project_meta["device_patch_map"] = normalized_map

                device_routing_map_raw = payload.get("device_routing_map")
                if device_routing_map_raw is not None:
                    if not isinstance(device_routing_map_raw, dict):
                        raise ValueError("device_routing_map must be an object")
                    normalized_routing_map: dict[str, list[str]] = {}
                    for raw_device_path, raw_routing_list in device_routing_map_raw.items():
                        device_rel = to_project_rel(raw_device_path)
                        if not isinstance(raw_routing_list, list):
                            continue
                        normalized_routing_list: list[str] = []
                        for raw_routing_path in raw_routing_list:
                            routing_rel = to_project_rel(raw_routing_path)
                            if routing_rel not in normalized_routing_list:
                                normalized_routing_list.append(routing_rel)
                        normalized_routing_map[device_rel] = normalized_routing_list
                    project_meta["device_routing_map"] = normalized_routing_map

                write_json_atomic(project_meta_path, project_meta)
                self._send_json({"ok": True, "saved": "project", **self._config_payload()})
                return

            if self.path == "/api/save-device-template":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")

                template_path_raw = str(payload.get("template_path") or "").strip()
                if not template_path_raw:
                    template_path_raw = "prototypes/sandbox/prototype-lab/device-templates/user-defined-devices.prototype.json"
                template_path = self._resolve_target_path(template_path_raw)
                if template_path.suffix.lower() != ".json":
                    raise ValueError("Template path must be a .json file")

                raw_template = payload.get("template")
                if not isinstance(raw_template, dict):
                    raise ValueError("template must be an object")

                template_name = str(raw_template.get("name") or raw_template.get("template_name") or "").strip()
                if not template_name:
                    raise ValueError("Template name is required")
                template_type = str(raw_template.get("device_type") or raw_template.get("type") or "Other").strip() or "Other"
                template_group = str(raw_template.get("layout_group") or template_type or "Other").strip() or "Other"
                manufacturer = str(raw_template.get("manufacturer") or raw_template.get("brand") or "Other").strip() or "Other"

                normalized_ports: list[dict[str, object]] = []
                raw_ports = raw_template.get("ports")
                if isinstance(raw_ports, list):
                    for idx, raw_port in enumerate(raw_ports):
                        if not isinstance(raw_port, dict):
                            continue
                        port_name = str(raw_port.get("name") or raw_port.get("port") or "").strip()
                        if not port_name:
                            continue
                        direction = str(raw_port.get("direction") or "io").strip().lower()
                        if direction not in {"in", "out", "io"}:
                            direction = "io"
                        families_raw = raw_port.get("families")
                        families: list[str] = []
                        if isinstance(families_raw, list):
                            for family in families_raw:
                                token = str(family or "").strip().upper()
                                if token and token not in families:
                                    families.append(token)
                        if not families:
                            fallback_family = str(raw_port.get("family") or "AUDIO").strip().upper() or "AUDIO"
                            families = [fallback_family]
                        port_payload: dict[str, object] = {
                            "name": port_name,
                            "direction": direction,
                            "families": families,
                            "transport": str(raw_port.get("transport") or "").strip(),
                            "order": idx,
                            "visible": bool(raw_port.get("visible", True)),
                            "enabled": bool(raw_port.get("enabled", True)),
                        }
                        group_raw = raw_port.get("group")
                        if isinstance(group_raw, dict):
                            group_name = str(group_raw.get("name") or "").strip()
                            if group_name:
                                group_payload: dict[str, object] = {"name": group_name}
                                member = str(group_raw.get("member") or "").strip()
                                if member:
                                    group_payload["member"] = member
                                index_value = group_raw.get("index")
                                size_value = group_raw.get("size")
                                if isinstance(index_value, (int, float)):
                                    group_payload["index"] = int(index_value)
                                if isinstance(size_value, (int, float)):
                                    group_payload["size"] = int(size_value)
                                port_payload["group"] = group_payload
                        normalized_ports.append(port_payload)

                normalized_template: dict[str, object] = {
                    "name": template_name,
                    "manufacturer": manufacturer,
                    "device_type": template_type,
                    "layout_group": template_group,
                    "ports": normalized_ports,
                }
                # Preserve optional per-device placement metadata exactly when
                # supplied. Missing fields stay missing so legacy/default Desk
                # and 1U interpretation never rewrites stored templates.
                for placement_key in ("rack_mountable", "location", "rack_units", "rack_position"):
                    if placement_key in raw_template:
                        normalized_template[placement_key] = raw_template[placement_key]
                placement_issues = validate_document(
                    "device_templates",
                    {
                        "version": 1,
                        "title": "Device template validation",
                        "templates": [normalized_template],
                    },
                )
                if placement_issues:
                    detail = "; ".join(issue.format() for issue in placement_issues)
                    raise ValueError(f"Invalid device template: {detail}")

                replace_existing = bool(payload.get("replace_existing"))
                if template_path.exists():
                    try:
                        root_payload = json.loads(template_path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        raise ValueError(f"Invalid template JSON: {template_path}") from exc
                    if not isinstance(root_payload, dict):
                        raise ValueError("Template JSON root must be an object")
                else:
                    root_payload = {}

                root_payload.setdefault("version", 1)
                if not str(root_payload.get("title") or "").strip():
                    root_payload["title"] = "User Defined Device Templates"
                if not str(root_payload.get("description") or "").strip():
                    root_payload["description"] = "Templates saved from the routing matrix UI."

                templates_raw = root_payload.get("templates")
                templates = templates_raw if isinstance(templates_raw, list) else []
                replaced = False
                template_key = (template_name.lower(), manufacturer.lower())
                for idx, existing in enumerate(templates):
                    if not isinstance(existing, dict):
                        continue
                    existing_name = str(existing.get("name") or "").strip().lower()
                    existing_manufacturer = str(existing.get("manufacturer") or existing.get("brand") or "Other").strip().lower()
                    if (existing_name, existing_manufacturer) != template_key:
                        continue
                    if not replace_existing:
                        raise ValueError(
                            f"Template already exists: {template_name} ({manufacturer}). "
                            "Set replace_existing=true to overwrite."
                        )
                    templates[idx] = normalized_template
                    replaced = True
                    break
                if not replaced:
                    templates.append(normalized_template)

                templates.sort(
                    key=lambda row: (
                        str((row or {}).get("manufacturer") or "Other").strip().lower(),
                        str((row or {}).get("name") or "").strip().lower(),
                    )
                )
                root_payload["templates"] = templates
                write_json_atomic(template_path, root_payload)
                self._send_json(
                    {
                        "ok": True,
                        "saved": "device-template",
                        "template_path": self._relative_path(template_path),
                        "template_name": template_name,
                        "manufacturer": manufacturer,
                        "replaced": replaced,
                        "template_count": len(templates),
                        **self._config_payload(),
                    }
                )
                return

            if self.path == "/api/set-targets":
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be a JSON object")

                if "model_path" in payload and str(payload.get("model_path") or "").strip():
                    self.__class__.model_path = self._resolve_target_path(payload.get("model_path"))
                    self.__class__.targets_selected = True
                if "connections_path" in payload and str(payload.get("connections_path") or "").strip():
                    self.__class__.connections_path = self._resolve_target_path(payload.get("connections_path"))
                    self.__class__.targets_selected = True
                if "route_debug_path" in payload and str(payload.get("route_debug_path") or "").strip():
                    self.__class__.route_debug_path = self._resolve_target_path(payload.get("route_debug_path"))
                if "preview_html" in payload and str(payload.get("preview_html") or "").strip():
                    self.__class__.preview_html_path = self._resolve_target_path(payload.get("preview_html"))
                if "preview_svg_dir" in payload and str(payload.get("preview_svg_dir") or "").strip():
                    self.__class__.preview_svg_dir = self._resolve_target_path(payload.get("preview_svg_dir"), expect_dir=True)

                self._send_json({"ok": True, "updated": True, **self._config_payload()})
                return

            if self.path == "/api/regenerate":
                # Always consume request bytes so keep-alive sockets remain in sync.
                self._discard_request_body()
                ok, message = self.__class__.trigger_regenerate(reason="api-regenerate")
                if not ok:
                    self._send_json(
                        {"ok": False, "error": message},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "regenerated": True,
                        "preview_html": self._relative_path(self.preview_html_path),
                        "preview_svg_dir": self._relative_path(self.preview_svg_dir),
                        "preview_paths": self._preview_paths_payload(),
                        "route_debug_path": self._relative_path(self.route_debug_path),
                        "output": message,
                    }
                )
                return

            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("Payload must be a JSON object")
            if self.path == "/api/save-model":
                expected_hash = str(self.headers.get("X-If-Unmodified-Model-Hash") or "").strip().lower()
                if expected_hash:
                    current_hash = read_json_hash(self.model_path) if self.model_path.exists() else ""
                    if current_hash and expected_hash != current_hash:
                        self._send_json(
                            {
                                "ok": False,
                                "error": (
                                    "Device config changed on disk since load; reload device config and retry save."
                                ),
                                "conflict": "model",
                                "current_hash": current_hash,
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                write_json_atomic(self.model_path, payload)
                if self.__class__.auto_regenerate:
                    ok, message = self.__class__.trigger_regenerate(reason="save-model")
                    if not ok:
                        # Persisted save succeeded; report regenerate failure as warning, not hard API failure.
                        self._send_json(
                            {
                                "ok": True,
                                "saved": "model",
                                "regenerate_ok": False,
                                "regenerate_error": message,
                            }
                        )
                        return
                self._send_json({"ok": True, "saved": "model", "regenerate_ok": True})
            else:
                expected_hash = str(self.headers.get("X-If-Unmodified-Connections-Hash") or "").strip().lower()
                if expected_hash:
                    current_hash = read_json_hash(self.connections_path) if self.connections_path.exists() else ""
                    if current_hash and expected_hash != current_hash:
                        self._send_json(
                            {
                                "ok": False,
                                "error": (
                                    "Patch config changed on disk since load; reload patch config and retry save."
                                ),
                                "conflict": "connections",
                                "current_hash": current_hash,
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                write_json_atomic(self.connections_path, payload)
                if self.__class__.auto_regenerate:
                    ok, message = self.__class__.trigger_regenerate(reason="save-connections")
                    if not ok:
                        # Persisted save succeeded; report regenerate failure as warning, not hard API failure.
                        self._send_json(
                            {
                                "ok": True,
                                "saved": "connections",
                                "regenerate_ok": False,
                                "regenerate_error": message,
                            }
                        )
                        return
                self._send_json({"ok": True, "saved": "connections", "regenerate_ok": True})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)


class ConfiguredRoutingMatrixHandler(RoutingMatrixHandler):
    """Per-server configured handler with concrete file targets."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve Studio Wiring files and enable save API for routing matrix."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Directory to serve static files from (default: current directory).",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("projects/studio-sidecar/device-configurations/basis.json"),
        help="Model JSON file to save (default: projects/studio-sidecar/device-configurations/basis.json).",
    )
    parser.add_argument(
        "--connections",
        type=Path,
        default=Path("projects/studio-sidecar/patch-configurations/basis/patch-default.json"),
        help="Connections JSON file to save (default: projects/studio-sidecar/patch-configurations/basis/patch-default.json).",
    )
    parser.add_argument(
        "--generator",
        type=Path,
        default=Path("generate_point_to_point.py"),
        help="Generator script used for visual regeneration (default: generate_point_to_point.py).",
    )
    parser.add_argument(
        "--routing-rules",
        type=Path,
        default=Path("json/routing_rules.json"),
        help="Global routing rules JSON used by generator (default: json/routing_rules.json).",
    )
    parser.add_argument(
        "--route-debug",
        type=Path,
        default=Path("projects/studio-sidecar/outputs/debug/route-debug.json"),
        help="Route debug JSON output path used when regenerating visuals (default: projects/studio-sidecar/outputs/debug/route-debug.json).",
    )
    parser.add_argument(
        "--preview-html",
        type=Path,
        default=Path("projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html"),
        help="Output HTML path used when regenerating visuals (default: projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html).",
    )
    parser.add_argument(
        "--preview-svg-dir",
        type=Path,
        default=Path("projects/studio-sidecar/outputs/svgs"),
        help="SVG directory used when regenerating visuals (default: projects/studio-sidecar/outputs/svgs).",
    )
    parser.add_argument(
        "--no-auto-regenerate",
        action="store_true",
        help="Disable automatic visual regeneration on watched JSON file changes.",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds for auto-regenerate watcher (default: 1.0).",
    )
    parser.add_argument(
        "--watch-debounce",
        type=float,
        default=0.75,
        help="Debounce in seconds before regenerating after file change (default: 0.75).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    model_path = (root / args.model).resolve() if not args.model.is_absolute() else args.model.resolve()
    connections_path = (
        (root / args.connections).resolve()
        if not args.connections.is_absolute()
        else args.connections.resolve()
    )
    generator_script = (root / args.generator).resolve() if not args.generator.is_absolute() else args.generator.resolve()
    routing_rules_path = (
        (root / args.routing_rules).resolve()
        if not args.routing_rules.is_absolute()
        else args.routing_rules.resolve()
    )
    route_debug_path = (
        (root / args.route_debug).resolve()
        if not args.route_debug.is_absolute()
        else args.route_debug.resolve()
    )
    preview_html_path = (root / args.preview_html).resolve() if not args.preview_html.is_absolute() else args.preview_html.resolve()
    preview_svg_dir = (root / args.preview_svg_dir).resolve() if not args.preview_svg_dir.is_absolute() else args.preview_svg_dir.resolve()

    if root not in model_path.parents and model_path != root:
        print(f"Error: model path must be inside root: {model_path}")
        return 1
    if root not in connections_path.parents and connections_path != root:
        print(f"Error: connections path must be inside root: {connections_path}")
        return 1
    if root not in preview_html_path.parents and preview_html_path != root:
        print(f"Error: preview-html path must be inside root: {preview_html_path}")
        return 1
    if root not in preview_svg_dir.parents and preview_svg_dir != root:
        print(f"Error: preview-svg-dir path must be inside root: {preview_svg_dir}")
        return 1
    if root not in routing_rules_path.parents and routing_rules_path != root:
        print(f"Error: routing-rules path must be inside root: {routing_rules_path}")
        return 1
    if root not in route_debug_path.parents and route_debug_path != root:
        print(f"Error: route-debug path must be inside root: {route_debug_path}")
        return 1

    ensure_global_routing_rules(root, routing_rules_path)

    ConfiguredRoutingMatrixHandler.model_path = model_path
    ConfiguredRoutingMatrixHandler.connections_path = connections_path
    ConfiguredRoutingMatrixHandler.routing_rules_path = routing_rules_path
    ConfiguredRoutingMatrixHandler.route_debug_path = route_debug_path
    ConfiguredRoutingMatrixHandler.root_path = root
    ConfiguredRoutingMatrixHandler.generator_script = generator_script
    ConfiguredRoutingMatrixHandler.preview_html_path = preview_html_path
    ConfiguredRoutingMatrixHandler.preview_svg_dir = preview_svg_dir
    ConfiguredRoutingMatrixHandler.auto_regenerate = not bool(args.no_auto_regenerate)
    # Startup defaults are kept internally as safe save targets, but not exposed
    # to the UI until the user explicitly selects targets in the app.
    ConfiguredRoutingMatrixHandler.targets_selected = False
    ConfiguredRoutingMatrixHandler.watch_interval_seconds = max(0.2, float(args.watch_interval))
    ConfiguredRoutingMatrixHandler.watch_debounce_seconds = max(0.0, float(args.watch_debounce))

    handler = partial(ConfiguredRoutingMatrixHandler, directory=str(root))

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    ConfiguredRoutingMatrixHandler.start_auto_regen_watcher()
    print(f"Serving {root} at http://{args.host}:{args.port}")
    print(f"Model save target: {model_path}")
    print(f"Connections save target: {connections_path}")
    print(f"Routing rules path: {routing_rules_path}")
    print(f"Route debug output: {route_debug_path}")
    print(f"Generator script: {generator_script}")
    print(f"Preview HTML: {preview_html_path}")
    print(f"Preview SVG dir: {preview_svg_dir}")
    print(
        "Auto regenerate: "
        f"{'on' if ConfiguredRoutingMatrixHandler.auto_regenerate else 'off'} "
        f"(interval={ConfiguredRoutingMatrixHandler.watch_interval_seconds:.2f}s, "
        f"debounce={ConfiguredRoutingMatrixHandler.watch_debounce_seconds:.2f}s)"
    )
    print(
        "Save API endpoints: /api/config, /api/projects, /api/set-targets, "
        "/api/create-project, /api/save-project, /api/save-model, "
        "/api/save-connections, /api/save-routing, /api/save-transaction, "
        "/api/routing, /api/regenerate"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ConfiguredRoutingMatrixHandler.stop_auto_regen_watcher()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
