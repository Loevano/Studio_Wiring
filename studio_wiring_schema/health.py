"""Read-only project health scanning built on the versioned validators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .validation import Issue, validate_document


def _prefixed(issue: Issue, relative_file: str) -> Issue:
    return Issue(
        path=f"{relative_file}:{issue.path}",
        code=issue.code,
        message=issue.message,
        severity=issue.severity,
    )


def _load_json(path: Path, root: Path, issues: list[Issue]) -> dict[str, Any] | None:
    relative = path.relative_to(root).as_posix()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(Issue(relative, "file.missing", "configured file does not exist"))
        return None
    except (OSError, UnicodeError) as exc:
        issues.append(Issue(relative, "file.read", f"could not read file: {exc}"))
        return None
    except json.JSONDecodeError as exc:
        issues.append(
            Issue(
                f"{relative}:line {exc.lineno}, column {exc.colno}",
                "json.invalid",
                exc.msg,
            )
        )
        return None
    if not isinstance(payload, dict):
        issues.append(Issue(f"{relative}:$", "type.object", "must be a JSON object"))
        return None
    return payload


def _resolve_project_path(root: Path, value: Any, issue_path: str, issues: list[Issue]) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        issues.append(Issue(issue_path, "path.unsafe", "configured path escapes project directory"))
        return None
    return candidate


def _unique_paths(paths: Iterable[Path | None]) -> list[Path]:
    return sorted({path for path in paths if path is not None}, key=lambda item: item.as_posix())


def _model_endpoints(payload: dict[str, Any]) -> set[tuple[str, str]]:
    endpoints: set[tuple[str, str]] = set()
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return endpoints
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_name = device.get("name")
        ports = device.get("ports")
        if not isinstance(device_name, str) or not isinstance(ports, list):
            continue
        for port in ports:
            if isinstance(port, dict) and isinstance(port.get("name"), str):
                endpoints.add((device_name.strip(), port["name"].strip()))
    return endpoints


def _check_dangling_endpoints(
    patch_payload: dict[str, Any],
    model_payload: dict[str, Any],
    patch_relative: str,
) -> list[Issue]:
    issues: list[Issue] = []
    endpoints = _model_endpoints(model_payload)
    connections = patch_payload.get("connections")
    if not isinstance(connections, list):
        return issues
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            continue
        cable_id = str(connection.get("cable_id") or f"connection {index}")
        for role, device_key, port_key in (
            ("source", "source_device", "source_port"),
            ("destination", "dest_device", "dest_port"),
        ):
            device = connection.get(device_key)
            port = connection.get(port_key)
            if not isinstance(device, str) or not isinstance(port, str):
                continue
            if (device.strip(), port.strip()) not in endpoints:
                issues.append(
                    Issue(
                        f"{patch_relative}:$.connections[{index}].{device_key}",
                        "endpoint.dangling",
                        f"{cable_id} {role} endpoint {device}/{port} is not present in its model",
                    )
                )
    return issues


def check_project(project_directory: Path) -> list[Issue]:
    """Scan a project without changing any files and return all detected issues."""
    root = project_directory.resolve()
    issues: list[Issue] = []
    if not root.is_dir():
        return [Issue(str(root), "project.missing", "project directory does not exist")]

    metadata_path = root / "project.json"
    metadata = _load_json(metadata_path, root, issues)
    if metadata is None:
        return issues
    issues.extend(_prefixed(issue, "project.json") for issue in validate_document("project", metadata))

    paths = metadata.get("paths") if isinstance(metadata.get("paths"), dict) else {}
    patch_map = (
        metadata.get("device_patch_map")
        if isinstance(metadata.get("device_patch_map"), dict)
        else {}
    )

    default_model = _resolve_project_path(
        root, paths.get("device_model"), "project.json:$.paths.device_model", issues
    )
    default_patch = _resolve_project_path(
        root, paths.get("default_patch"), "project.json:$.paths.default_patch", issues
    )
    patch_directory = _resolve_project_path(
        root, paths.get("patch_directory"), "project.json:$.paths.patch_directory", issues
    )

    model_paths: list[Path | None] = [default_model]
    patch_paths: list[Path | None] = [default_patch]
    explicit_associations: dict[Path, Path] = {}
    for model_value, patch_values in patch_map.items():
        model_path = _resolve_project_path(
            root, model_value, f"project.json:$.device_patch_map[{model_value!r}]", issues
        )
        model_paths.append(model_path)
        if not isinstance(patch_values, list):
            continue
        for index, patch_value in enumerate(patch_values):
            patch_path = _resolve_project_path(
                root,
                patch_value,
                f"project.json:$.device_patch_map[{model_value!r}][{index}]",
                issues,
            )
            patch_paths.append(patch_path)
            if model_path is not None and patch_path is not None:
                explicit_associations[patch_path] = model_path

    device_directory = root / "device-configurations"
    if device_directory.is_dir():
        model_paths.extend(device_directory.glob("*.json"))
    elif default_model is not None:
        issues.append(Issue("device-configurations", "directory.missing", "directory does not exist"))

    if patch_directory is not None:
        if patch_directory.is_dir():
            patch_paths.extend(patch_directory.rglob("*.json"))
        else:
            relative = patch_directory.relative_to(root).as_posix()
            issues.append(Issue(relative, "directory.missing", "configured patch directory does not exist"))

    models: dict[Path, dict[str, Any]] = {}
    for model_path in _unique_paths(model_paths):
        payload = _load_json(model_path, root, issues)
        if payload is None:
            continue
        relative = model_path.relative_to(root).as_posix()
        models[model_path] = payload
        issues.extend(_prefixed(issue, relative) for issue in validate_document("model", payload))

    patches: dict[Path, dict[str, Any]] = {}
    for patch_path in _unique_paths(patch_paths):
        payload = _load_json(patch_path, root, issues)
        if payload is None:
            continue
        relative = patch_path.relative_to(root).as_posix()
        patches[patch_path] = payload
        issues.extend(_prefixed(issue, relative) for issue in validate_document("patch", payload))

    optional_documents = (
        ("routing_rules", "routing_rules"),
        ("device_templates", "device_templates"),
    )
    for path_key, kind in optional_documents:
        if path_key not in paths:
            continue
        optional_path = _resolve_project_path(
            root, paths[path_key], f"project.json:$.paths.{path_key}", issues
        )
        if optional_path is None:
            continue
        payload = _load_json(optional_path, root, issues)
        if payload is not None:
            relative = optional_path.relative_to(root).as_posix()
            issues.extend(_prefixed(issue, relative) for issue in validate_document(kind, payload))

    if default_model is not None and default_patch is not None:
        explicit_associations.setdefault(default_patch, default_model)
    model_by_stem = {path.stem.casefold(): path for path in models}
    for patch_path, patch_payload in patches.items():
        model_path = explicit_associations.get(patch_path)
        if model_path is None:
            parent_stem = patch_path.parent.name.casefold()
            model_path = model_by_stem.get(parent_stem)
        if model_path is None and len(models) == 1:
            model_path = next(iter(models))
        model_payload = models.get(model_path) if model_path is not None else None
        if model_payload is None:
            issues.append(
                Issue(
                    patch_path.relative_to(root).as_posix(),
                    "patch.model_unresolved",
                    "could not determine which device model owns this patch",
                    severity="warning",
                )
            )
            continue
        issues.extend(
            _check_dangling_endpoints(
                patch_payload,
                model_payload,
                patch_path.relative_to(root).as_posix(),
            )
        )

    return sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))
