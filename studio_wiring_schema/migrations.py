"""Explicit, deterministic migrations for safely recognizable legacy shapes."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from .validation import DOCUMENT_KINDS, SUPPORTED_VERSION


@dataclass(frozen=True)
class MigrationResult:
    data: dict[str, Any]
    changes: tuple[str, ...]
    source_version: int
    target_version: int


def _bool_like(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return None


def _rename(obj: dict[str, Any], old: str, new: str, path: str, changes: list[str]) -> None:
    if new not in obj and old in obj:
        obj[new] = obj.pop(old)
        changes.append(f"{path}.{old} -> {path}.{new}")


def _normalize_booleans(
    obj: dict[str, Any], fields: tuple[str, ...], path: str, changes: list[str]
) -> None:
    for field in fields:
        if field not in obj or isinstance(obj[field], bool):
            continue
        normalized = _bool_like(obj[field])
        if normalized is not None:
            obj[field] = normalized
            changes.append(f"{path}.{field}: boolean-like value -> boolean")


def _migrate_device(device: Any, path: str, changes: list[str]) -> None:
    if not isinstance(device, dict):
        return
    _rename(device, "device_name", "name", path, changes)
    _rename(device, "type", "device_type", path, changes)
    _normalize_booleans(device, ("visible", "hidden", "enabled", "disabled"), path, changes)
    ports = device.get("ports")
    if not isinstance(ports, list):
        return
    for index, port in enumerate(ports):
        if not isinstance(port, dict):
            continue
        port_path = f"{path}.ports[{index}]"
        _rename(port, "port_name", "name", port_path, changes)
        _normalize_booleans(port, ("visible", "hidden", "enabled", "disabled"), port_path, changes)


def _migrate_project_v0(payload: dict[str, Any], changes: list[str]) -> None:
    _rename(payload, "project_name", "name", "$", changes)


def _migrate_model_v0(payload: dict[str, Any], changes: list[str]) -> None:
    _rename(payload, "name", "title", "$", changes)
    devices = payload.get("devices")
    if isinstance(devices, list):
        for index, device in enumerate(devices):
            _migrate_device(device, f"$.devices[{index}]", changes)


def _migrate_patch_v0(payload: dict[str, Any], changes: list[str]) -> None:
    connections = payload.get("connections")
    if not isinstance(connections, list):
        return
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            continue
        path = f"$.connections[{index}]"
        source = connection.get("source")
        if isinstance(source, dict):
            source_conflict = False
            if "source_device" not in connection and isinstance(source.get("device"), str):
                connection["source_device"] = source["device"]
                changes.append(f"{path}.source.device -> {path}.source_device")
            elif isinstance(source.get("device"), str) and source["device"] != connection.get(
                "source_device"
            ):
                source_conflict = True
            if "source_port" not in connection and isinstance(source.get("port"), str):
                connection["source_port"] = source["port"]
                changes.append(f"{path}.source.port -> {path}.source_port")
            elif isinstance(source.get("port"), str) and source["port"] != connection.get(
                "source_port"
            ):
                source_conflict = True
            if not source_conflict:
                connection.pop("source", None)
        destination = connection.get("dest")
        destination_key = "dest"
        if not isinstance(destination, dict):
            destination = connection.get("destination")
            destination_key = "destination"
        if isinstance(destination, dict):
            destination_conflict = False
            if "dest_device" not in connection and isinstance(destination.get("device"), str):
                connection["dest_device"] = destination["device"]
                changes.append(f"{path}.destination.device -> {path}.dest_device")
            elif isinstance(destination.get("device"), str) and destination["device"] != connection.get(
                "dest_device"
            ):
                destination_conflict = True
            if "dest_port" not in connection and isinstance(destination.get("port"), str):
                connection["dest_port"] = destination["port"]
                changes.append(f"{path}.destination.port -> {path}.dest_port")
            elif isinstance(destination.get("port"), str) and destination["port"] != connection.get(
                "dest_port"
            ):
                destination_conflict = True
            if not destination_conflict:
                connection.pop(destination_key, None)
        _rename(connection, "source_jack", "source_port", path, changes)
        _rename(connection, "destination_device", "dest_device", path, changes)
        _rename(connection, "destination_port", "dest_port", path, changes)
        _rename(connection, "dest_jack", "dest_port", path, changes)
        _normalize_booleans(connection, ("override_1to1",), path, changes)


def _migrate_routing_rules_v0(payload: dict[str, Any], changes: list[str]) -> None:
    routing = payload.get("routing")
    if isinstance(routing, dict):
        _normalize_booleans(routing, ("fifo_forward_turns", "video_early_turn"), "$.routing", changes)


def _migrate_device_templates_v0(payload: dict[str, Any], changes: list[str]) -> None:
    templates = payload.get("templates")
    if isinstance(templates, list):
        for index, template in enumerate(templates):
            _migrate_device(template, f"$.templates[{index}]", changes)


MigrationFunction = Callable[[dict[str, Any], list[str]], None]
MIGRATIONS: dict[str, dict[int, MigrationFunction]] = {
    "project": {0: _migrate_project_v0},
    "model": {0: _migrate_model_v0},
    "patch": {0: _migrate_patch_v0},
    "routing_rules": {0: _migrate_routing_rules_v0},
    "device_templates": {0: _migrate_device_templates_v0},
}


def migrate_document(
    kind: str,
    payload: dict[str, Any],
    target_version: int = SUPPORTED_VERSION,
) -> MigrationResult:
    """Return a migrated deep copy; the caller's object is never modified."""
    if kind not in DOCUMENT_KINDS:
        choices = ", ".join(sorted(DOCUMENT_KINDS))
        raise ValueError(f"Unknown document kind {kind!r}; expected one of: {choices}")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")
    if target_version != SUPPORTED_VERSION:
        raise ValueError(f"Unsupported migration target: {target_version}")

    raw_version = payload.get("version", 0)
    if not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version < 0:
        raise ValueError("Document version must be a non-negative integer or omitted")
    if raw_version > target_version:
        raise ValueError(
            f"Cannot migrate {kind} version {raw_version} down to version {target_version}"
        )

    migrated = copy.deepcopy(payload)
    changes: list[str] = []
    current_version = raw_version
    while current_version < target_version:
        migration = MIGRATIONS.get(kind, {}).get(current_version)
        if migration is None:
            raise ValueError(
                f"No {kind} migration registered from version {current_version} "
                f"to version {current_version + 1}"
            )
        migration(migrated, changes)
        current_version += 1
        migrated["version"] = current_version
        changes.append(f"$.version: {current_version - 1} -> {current_version}")

    return MigrationResult(
        data=migrated,
        changes=tuple(changes),
        source_version=raw_version,
        target_version=current_version,
    )
