"""Dependency-free, path-aware validators for Studio Wiring JSON documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable


SUPPORTED_VERSION = 1
DOCUMENT_KINDS = {"project", "model", "patch", "routing_rules", "device_templates"}


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str
    severity: str = "error"

    def format(self) -> str:
        return f"{self.severity.upper()} {self.path} [{self.code}] {self.message}"


def _issue(issues: list[Issue], path: str, code: str, message: str) -> None:
    issues.append(Issue(path=path, code=code, message=message))


def _object(value: Any, path: str, issues: list[Issue]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        _issue(issues, path, "type.object", "must be an object")
        return None
    return value


def _array(value: Any, path: str, issues: list[Issue]) -> list[Any] | None:
    if not isinstance(value, list):
        _issue(issues, path, "type.array", "must be an array")
        return None
    return value


def _required_string(obj: dict[str, Any], key: str, path: str, issues: list[Issue]) -> str:
    value = obj.get(key)
    field_path = f"{path}.{key}"
    if not isinstance(value, str) or not value.strip():
        _issue(issues, field_path, "value.non_empty_string", "must be a non-empty string")
        return ""
    return value.strip()


def _optional_string(obj: dict[str, Any], key: str, path: str, issues: list[Issue]) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        _issue(issues, f"{path}.{key}", "type.string", "must be a string")
        return ""
    return value.strip()


def _version(obj: dict[str, Any], issues: list[Issue]) -> None:
    value = obj.get("version")
    if not isinstance(value, int) or isinstance(value, bool):
        _issue(issues, "$.version", "version.type", "must be an integer")
    elif value != SUPPORTED_VERSION:
        _issue(
            issues,
            "$.version",
            "version.unsupported",
            f"unsupported version {value}; supported version is {SUPPORTED_VERSION}",
        )


def _relative_path(value: Any, path: str, issues: list[Issue]) -> str:
    if not isinstance(value, str) or not value.strip():
        _issue(issues, path, "path.non_empty", "must be a non-empty relative path")
        return ""
    normalized = value.replace("\\", "/").strip()
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        _issue(issues, path, "path.unsafe", "must stay inside the project directory")
    return normalized


def _validate_boolean_fields(obj: dict[str, Any], path: str, issues: list[Issue]) -> None:
    for key in ("visible", "hidden", "enabled", "disabled"):
        if key in obj and not isinstance(obj[key], bool):
            _issue(issues, f"{path}.{key}", "type.boolean", "must be a boolean")


def _validate_port(port: Any, path: str, issues: list[Issue]) -> str:
    item = _object(port, path, issues)
    if item is None:
        return ""
    name = _required_string(item, "name", path, issues)
    direction = _required_string(item, "direction", path, issues).lower()
    if direction and direction not in {"in", "out", "io"}:
        _issue(issues, f"{path}.direction", "port.direction", "must be one of: in, out, io")
    families = item.get("families")
    if families is not None:
        family_items = _array(families, f"{path}.families", issues)
        if family_items is not None:
            for index, family in enumerate(family_items):
                if not isinstance(family, str) or not family.strip():
                    _issue(
                        issues,
                        f"{path}.families[{index}]",
                        "value.non_empty_string",
                        "must be a non-empty string",
                    )
    _optional_string(item, "transport", path, issues)
    if "order" in item and (not isinstance(item["order"], int) or isinstance(item["order"], bool)):
        _issue(issues, f"{path}.order", "type.integer", "must be an integer")
    if "group" in item and not isinstance(item["group"], dict):
        _issue(issues, f"{path}.group", "type.object", "must be an object")
    _validate_boolean_fields(item, path, issues)
    return name


def _validate_rack_fields(
    item: dict[str, Any], path: str, issues: list[Issue]
) -> tuple[int, int, int] | None:
    """Validate optional rack metadata and return a valid placed span.

    Missing rack_mountable, location, and rack_units retain their compatibility
    defaults (false, Desk, and 1U) for interpretation only; the input document is
    never rewritten.
    """
    rack_mountable = item.get("rack_mountable", False)
    rack_mountable_valid = isinstance(rack_mountable, bool)
    if not rack_mountable_valid:
        _issue(
            issues,
            f"{path}.rack_mountable",
            "rack.mountable",
            "must be a boolean",
        )

    location = item.get("location", "Desk")
    location_valid = isinstance(location, str) and location in {"Desk", "Rack"}
    if not location_valid:
        _issue(
            issues,
            f"{path}.location",
            "rack.location",
            "must be one of: Desk, Rack",
        )
    elif (
        location == "Rack"
        and "rack_mountable" in item
        and not (rack_mountable_valid and rack_mountable is True)
    ):
        _issue(
            issues,
            f"{path}.location",
            "rack.not_mountable",
            "cannot be Rack unless rack_mountable is true",
        )

    rack_units = item.get("rack_units", 1)
    rack_units_valid = (
        isinstance(rack_units, int)
        and not isinstance(rack_units, bool)
        and 1 <= rack_units <= 16
    )
    if not rack_units_valid:
        _issue(
            issues,
            f"{path}.rack_units",
            "rack.units",
            "must be an integer from 1 through 16",
        )

    if "rack_position" not in item or item.get("rack_position") is None:
        return None
    position_path = f"{path}.rack_position"
    position = item.get("rack_position")
    if not isinstance(position, dict):
        _issue(issues, position_path, "rack.position", "must be null or an object")
        return None

    rack = position.get("rack")
    start_u = position.get("start_u")
    rack_valid = (
        isinstance(rack, int) and not isinstance(rack, bool) and 1 <= rack <= 4
    )
    start_valid = (
        isinstance(start_u, int)
        and not isinstance(start_u, bool)
        and 1 <= start_u <= 16
    )
    if not rack_valid:
        _issue(
            issues,
            f"{position_path}.rack",
            "rack.number",
            "must be an integer from 1 through 4",
        )
    if not start_valid:
        _issue(
            issues,
            f"{position_path}.start_u",
            "rack.start_u",
            "must be an integer from 1 through 16",
        )
    if location_valid and location == "Desk":
        _issue(
            issues,
            position_path,
            "rack.desk_position",
            "Desk devices cannot have a rack position",
        )
        return None
    if not (
        rack_mountable_valid
        and rack_mountable is True
        and location_valid
        and location == "Rack"
        and rack_units_valid
        and rack_valid
        and start_valid
    ):
        return None
    end_u = start_u + rack_units - 1
    if end_u > 16:
        _issue(
            issues,
            position_path,
            "rack.out_of_bounds",
            f"occupies U{start_u} through U{end_u}; rack positions must end at U16",
        )
        return None
    return rack, start_u, end_u


def _validate_device(
    device: Any,
    path: str,
    issues: list[Issue],
    *,
    validate_rack: bool = True,
) -> str:
    item = _object(device, path, issues)
    if item is None:
        return ""
    name = _required_string(item, "name", path, issues)
    _optional_string(item, "device_type", path, issues)
    _optional_string(item, "layout_group", path, issues)
    _validate_boolean_fields(item, path, issues)
    if validate_rack:
        _validate_rack_fields(item, path, issues)
    ports = _array(item.get("ports"), f"{path}.ports", issues)
    if ports is not None:
        seen: dict[str, int] = {}
        for index, port in enumerate(ports):
            port_name = _validate_port(port, f"{path}.ports[{index}]", issues)
            if port_name:
                key = port_name.casefold()
                if key in seen:
                    _issue(
                        issues,
                        f"{path}.ports[{index}].name",
                        "port.duplicate",
                        f"duplicates port name at index {seen[key]}",
                    )
                else:
                    seen[key] = index
    return name


def validate_project(payload: Any) -> list[Issue]:
    issues: list[Issue] = []
    obj = _object(payload, "$", issues)
    if obj is None:
        return issues
    _version(obj, issues)
    _required_string(obj, "name", "$", issues)
    _optional_string(obj, "description", "$", issues)
    paths = _object(obj.get("paths"), "$.paths", issues)
    if paths is not None:
        for key in ("device_model", "default_patch", "patch_directory"):
            _relative_path(paths.get(key), f"$.paths.{key}", issues)
        for key in (
            "output_html_directory",
            "output_svg_directory",
            "output_debug_directory",
            "routing_rules",
            "device_templates",
        ):
            if key in paths:
                _relative_path(paths[key], f"$.paths.{key}", issues)
    patch_map = _object(obj.get("device_patch_map"), "$.device_patch_map", issues)
    if patch_map is not None:
        for model_path, patch_paths in patch_map.items():
            map_path = f"$.device_patch_map[{model_path!r}]"
            _relative_path(model_path, map_path, issues)
            values = _array(patch_paths, map_path, issues)
            if values is not None:
                for index, patch_path in enumerate(values):
                    _relative_path(patch_path, f"{map_path}[{index}]", issues)
    return issues


def validate_model(payload: Any) -> list[Issue]:
    issues: list[Issue] = []
    obj = _object(payload, "$", issues)
    if obj is None:
        return issues
    _version(obj, issues)
    _required_string(obj, "title", "$", issues)
    families = obj.get("families")
    if families is not None:
        family_map = _object(families, "$.families", issues)
        if family_map is not None:
            for family_name, definition in family_map.items():
                path = f"$.families[{family_name!r}]"
                if not isinstance(family_name, str) or not family_name.strip():
                    _issue(issues, path, "family.name", "family name must be non-empty")
                _object(definition, path, issues)
    devices = _array(obj.get("devices"), "$.devices", issues)
    if devices is not None:
        seen: dict[str, int] = {}
        placements: list[tuple[int, int, int, int, str]] = []
        for index, device in enumerate(devices):
            device_path = f"$.devices[{index}]"
            name = _validate_device(device, device_path, issues, validate_rack=False)
            placement = (
                _validate_rack_fields(device, device_path, issues)
                if isinstance(device, dict)
                else None
            )
            if placement is not None:
                rack, start_u, end_u = placement
                conflicts = [
                    (other_index, other_name)
                    for other_rack, other_start, other_end, other_index, other_name in placements
                    if other_rack == rack
                    and start_u <= other_end
                    and other_start <= end_u
                ]
                if conflicts:
                    first_index, first_name = min(conflicts)
                    _issue(
                        issues,
                        f"{device_path}.rack_position",
                        "rack.overlap",
                        f"overlaps {first_name or 'device'} at index {first_index} in rack {rack}",
                    )
                placements.append((rack, start_u, end_u, index, name))
            if name:
                key = name.casefold()
                if key in seen:
                    _issue(
                        issues,
                        f"$.devices[{index}].name",
                        "device.duplicate",
                        f"duplicates device name at index {seen[key]}",
                    )
                else:
                    seen[key] = index
    if "ui_config" in obj and not isinstance(obj["ui_config"], dict):
        _issue(issues, "$.ui_config", "type.object", "must be an object")
    return issues


def _endpoint_fields(connection: dict[str, Any], index: int, issues: list[Issue]) -> None:
    base = f"$.connections[{index}]"
    for key in ("source_device", "source_port", "dest_device", "dest_port"):
        _required_string(connection, key, base, issues)


def validate_patch(payload: Any) -> list[Issue]:
    issues: list[Issue] = []
    obj = _object(payload, "$", issues)
    if obj is None:
        return issues
    _version(obj, issues)
    connections = _array(obj.get("connections"), "$.connections", issues)
    if connections is None:
        return issues
    seen_cables: dict[str, int] = {}
    strict_endpoints: dict[tuple[str, str], int] = {}
    for index, connection in enumerate(connections):
        path = f"$.connections[{index}]"
        item = _object(connection, path, issues)
        if item is None:
            continue
        cable_id = _required_string(item, "cable_id", path, issues)
        if cable_id:
            key = cable_id.casefold()
            if key in seen_cables:
                _issue(
                    issues,
                    f"{path}.cable_id",
                    "cable.duplicate",
                    f"duplicates cable ID at index {seen_cables[key]}",
                )
            else:
                seen_cables[key] = index
        _endpoint_fields(item, index, issues)
        if "override_1to1" in item and not isinstance(item["override_1to1"], bool):
            _issue(issues, f"{path}.override_1to1", "type.boolean", "must be a boolean")
        allow_override = item.get("override_1to1") is True
        if not allow_override:
            for device_key, port_key in (
                ("source_device", "source_port"),
                ("dest_device", "dest_port"),
            ):
                device = item.get(device_key)
                port = item.get(port_key)
                if not isinstance(device, str) or not isinstance(port, str):
                    continue
                endpoint = (device.strip(), port.strip())
                if not all(endpoint):
                    continue
                if endpoint in strict_endpoints:
                    _issue(
                        issues,
                        f"{path}.{device_key}",
                        "endpoint.duplicate_usage",
                        f"endpoint {endpoint[0]}/{endpoint[1]} is already used at "
                        f"connection index {strict_endpoints[endpoint]}; set override_1to1=true "
                        "to allow multiple uses",
                    )
                else:
                    strict_endpoints[endpoint] = index
    return issues


def validate_routing_rules(payload: Any) -> list[Issue]:
    issues: list[Issue] = []
    obj = _object(payload, "$", issues)
    if obj is None:
        return issues
    _version(obj, issues)
    labels = _object(obj.get("labels"), "$.labels", issues)
    if labels is not None:
        for key in ("source_side", "destination_side"):
            value = _required_string(labels, key, "$.labels", issues).lower()
            if value and value not in {"above", "below"}:
                _issue(issues, f"$.labels.{key}", "label.side", "must be 'above' or 'below'")
        font_size = labels.get("font_size")
        if not isinstance(font_size, (int, float)) or isinstance(font_size, bool) or font_size <= 0:
            _issue(issues, "$.labels.font_size", "number.positive", "must be a positive number")
    routing = _object(obj.get("routing"), "$.routing", issues)
    if routing is not None:
        for key in ("fifo_forward_turns", "video_early_turn"):
            if not isinstance(routing.get(key), bool):
                _issue(issues, f"$.routing.{key}", "type.boolean", "must be a boolean")
        wrap = _required_string(routing, "backward_out_to_in_wrap", "$.routing", issues).lower()
        if wrap and wrap not in {"above", "below"}:
            _issue(
                issues,
                "$.routing.backward_out_to_in_wrap",
                "routing.wrap",
                "must be 'above' or 'below'",
            )
        threshold = routing.get("video_vertical_rows_threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold < 0:
            _issue(
                issues,
                "$.routing.video_vertical_rows_threshold",
                "number.non_negative",
                "must be a non-negative number",
            )
    return issues


def validate_device_templates(payload: Any) -> list[Issue]:
    issues: list[Issue] = []
    obj = _object(payload, "$", issues)
    if obj is None:
        return issues
    _version(obj, issues)
    _required_string(obj, "title", "$", issues)
    templates = _array(obj.get("templates"), "$.templates", issues)
    if templates is not None:
        seen: dict[str, int] = {}
        for index, template in enumerate(templates):
            name = _validate_device(template, f"$.templates[{index}]", issues)
            if name:
                key = name.casefold()
                if key in seen:
                    _issue(
                        issues,
                        f"$.templates[{index}].name",
                        "template.duplicate",
                        f"duplicates template name at index {seen[key]}",
                    )
                else:
                    seen[key] = index
    return issues


VALIDATORS: dict[str, Callable[[Any], list[Issue]]] = {
    "project": validate_project,
    "model": validate_model,
    "patch": validate_patch,
    "routing_rules": validate_routing_rules,
    "device_templates": validate_device_templates,
}
VERSIONED_VALIDATORS: dict[str, dict[int, Callable[[Any], list[Issue]]]] = {
    kind: {SUPPORTED_VERSION: validator} for kind, validator in VALIDATORS.items()
}


def validate_document(kind: str, payload: Any) -> list[Issue]:
    try:
        fallback_validator = VALIDATORS[kind]
    except KeyError as exc:
        choices = ", ".join(sorted(DOCUMENT_KINDS))
        raise ValueError(f"Unknown document kind {kind!r}; expected one of: {choices}") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    validator = VERSIONED_VALIDATORS[kind].get(version, fallback_validator)
    return validator(payload)
