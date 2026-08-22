#!/usr/bin/env python3
"""Generate CAT-style point-to-point wiring diagrams as HTML/SVG (no Graphviz)."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# SVG layout geometry defaults
# ---------------------------------------------------------------------------
ROW_HEIGHT = 22
HEADER_HEIGHT = 36
BOX_GAP = 30
TYPE_GAP = 38
BOX_MIN_WIDTH = 270.0
BOX_MAX_WIDTH = 520.0
CONNECTION_LABEL_FONT_SIZE = 7.0
CONNECTION_LABEL_CHAR_PX = 5.4
GROUP_LABEL_TOP_OFFSET = 13.0
GROUP_ROUTE_CLEARANCE = 8.0
GROUP_FRAME_HORIZONTAL_PADDING = 10.0

# ---------------------------------------------------------------------------
# Matrix family defaults (shared by SVG generation + routing UI)
# ---------------------------------------------------------------------------
MATRIX_FAMILY_ORDER = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER"]

DEFAULT_MATRIX_FAMILY_DEFINITIONS: dict[str, dict[str, str]] = {
    "AUDIO": {
        "prefix": "AUDIO",
        "layer": "Audio Analog",
        "signal_type": "Analog Audio",
        "default_cable_type": "Analog",
    },
    "COMP": {
        "prefix": "COMP",
        "layer": "Computer/Data",
        "signal_type": "Computer Data",
        "default_cable_type": "Computer/Data",
    },
    "DIGI": {
        "prefix": "DIGI",
        "layer": "Digital Audio",
        "signal_type": "Digital Audio",
        "default_cable_type": "Digital Audio",
    },
    "NETWORK": {
        "prefix": "NETWORK",
        "layer": "Network",
        "signal_type": "Network Data",
        "default_cable_type": "Network",
    },
    "POWER": {
        "prefix": "POWER",
        "layer": "Power",
        "signal_type": "Mains Power",
        "default_cable_type": "AC Power",
    },
}


def build_empty_model_template(
    title: str = "Studio Sidecar",
    families: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    base_families = (
        families
        if isinstance(families, dict) and families
        else DEFAULT_MATRIX_FAMILY_DEFINITIONS
    )
    merged_families: dict[str, dict[str, str]] = {}
    for key, cfg in DEFAULT_MATRIX_FAMILY_DEFINITIONS.items():
        token = str(key).strip().upper()
        source_cfg = base_families.get(token, cfg) if isinstance(base_families, dict) else cfg
        source_obj = source_cfg if isinstance(source_cfg, dict) else {}
        merged_families[token] = {
            "prefix": str(source_obj.get("prefix", cfg.get("prefix", token))),
            "layer": str(source_obj.get("layer", cfg.get("layer", token.title()))),
            "signal_type": str(source_obj.get("signal_type", cfg.get("signal_type", token.title()))),
            "default_cable_type": str(source_obj.get("default_cable_type", cfg.get("default_cable_type", token.title()))),
        }

    return {
        "version": 1,
        "title": str(title or "Studio Sidecar"),
        "families": merged_families,
        "devices": [],
        "ui_config": {
            "version": 1,
            "theme": {
                "mode": "light",
                "explicit": False,
            },
            "matrix": {
                "type_tag": "",
                "allow_double_patching": False,
                "patch_mode": "single",
                "pair_count": 8,
                "sub_tab": "patch",
                "collapsed_source_groups": [],
                "collapsed_dest_groups": [],
                "collapsed_source_devices": [],
                "collapsed_dest_devices": [],
                "auto_collapsed_families": [],
            },
            "editor": {
                "selected_device_name": "",
                "selected_port_tab": "in",
            },
            "visibility": {
                "hidden_devices": [],
                "device_order": [],
            },
        },
    }

DEFAULT_ROUTING_RULES: dict[str, object] = {
    "version": 1,
    "labels": {
        "source_side": "above",
        "destination_side": "below",
        "font_size": CONNECTION_LABEL_FONT_SIZE,
        "wire_gap_px": 4.0,
        "offset_step_px": 6.0,
    },
    "routing": {
        "fifo_forward_turns": True,
        "backward_out_to_in_wrap": "below",
        "video_early_turn": True,
        "video_vertical_rows_threshold": 6.0,
        "forward_turn_edge_margin": 0.12,
        "wire_clearance_px": 12.0,
        "power_wire_clearance_px": 18.0,
        "power_lane_spacing_px": 18.0,
        "power_column_gap_px": 420.0,
        "left_route_gutter_px": 108.0,
    },
}

# ---------------------------------------------------------------------------
# Visual style defaults
# ---------------------------------------------------------------------------
DEFAULT_LAYER_COLORS = {
    "Audio Analog": "#2563eb",
    "Digital Audio": "#dc2626",
    "Network": "#0f766e",
    "Computer/Data": "#475569",
    "MIDI": "#9333ea",
    "Power": "#b45309",
    "Spare / Planned": "#64748b",
}

# Distinct, high-contrast circuit colours used only by the dedicated Power SVG.
# Additional circuit numbers wrap through the palette deterministically.
POWER_GROUP_COLORS = [
    "#1d4ed8",  # blue
    "#b45309",  # amber
    "#15803d",  # green
    "#7e22ce",  # purple
    "#b91c1c",  # red
    "#0e7490",  # cyan
]

POWER_BRANCH_COLORS = {
    "Low": "#0e7490",
    "High": "#be123c",
    "Digital": "#7e22ce",
}

POWER_BRANCH_LABELS = {
    "Digital": "Digital",
    "High": "Hi",
    "Low": "Lo",
}

FALLBACK_LAYER_COLORS = [
    "#0284c7",
    "#7c3aed",
    "#ea580c",
    "#0d9488",
    "#be123c",
    "#4f46e5",
]

# Common port label abbreviations used to reduce clutter in generated visuals.
PORT_ABBREVIATIONS: list[tuple[str, str]] = [
    (r"\bSpeaker\b", "Spk"),
    (r"\bInput\b", "In"),
    (r"\bOutput\b", "Out"),
    (r"\bAnalog\b", "AN"),
    (r"\bDigital\b", "DIG"),
    (r"\bOptical\b", "Opt"),
    (r"\bChannel\b", "Ch"),
    (r"\bPort\b", "P"),
    (r"\bReturn\b", "Ret"),
    (r"\bMonitor\b", "Mon"),
    (r"\bBalanced\b", "Bal"),
    (r"\bLeft\b", "L"),
    (r"\bRight\b", "R"),
]

DEVICE_TYPE_ORDER = [
    "Console / Mixer",
    "Tape Machine",
    "Interface / Converter",
    "Patchbay",
    "Preamps / Channel Strip",
    "Outboard / FX",
    "Amplifier / Monitor Control",
    "Speaker / Monitor",
    "Computer / Control",
    "Network",
    "Microphone / DI",
    "Other",
]

# Default device grouping used by the "All Connections" overview layout.
OVERVIEW_FUNCTIONAL_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Analog Front End",
        [
            "Allen & Heath GS3000",
            "Tascam MS-16",
            "Audient ASP 880",
            "Focusrite Platinum Voice Master",
            "MAO Preamp (confirm model)",
            "Future Preamp Slots",
            "Talkback Mic",
        ],
    ),
    (
        "Computer / Control",
        [
            "Mac mini",
            "Thunderbolt Dock",
            "TV Screen",
            "Avid S1 #1",
            "Avid S1 #2",
            "Streamdeck #1",
            "Streamdeck #2",
            "Netgear Unmanaged Switch",
        ],
    ),
    (
        "Digital Core",
        [
            "RME UFX III",
            "SSL AX MADI",
            "TC Electronic Finalizer 48K",
            "Sony DPS-R7 Reverb",
            "TC Electronic Clarity M Stereo",
        ],
    ),
    (
        "Monitoring",
        [
            "IMG STAGELINE PPA-100/SW",
            "Behringer A800 #1",
            "Behringer A800 #2",
            "Tannoy System 10",
            "ATC SCM 11",
            "Auratone 5C",
        ],
    ),
]


@dataclass(frozen=True)
class Connection:
    cable_id: str
    source_device: str
    source_jack: str
    dest_device: str
    dest_jack: str
    layer: str
    signal_type: str
    status: str
    cable_type: str
    connection_type: str
    notes: str


@dataclass
class DeviceBox:
    name: str
    device_type: str
    x: float
    y: float
    width: float
    height: float
    in_ports: list[str]
    out_ports: list[str]
    in_port_y: dict[str, float]
    out_port_y: dict[str, float]
    port_roles: dict[str, str]
    port_connected: dict[str, bool]


@dataclass
class GroupBlock:
    device_type: str
    x: float
    y: float
    width: float
    height: float


def slugify(value: str) -> str:
    lowered = value.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "item"


def natural_key(value: str) -> list[object]:
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", value)]


def relative_url_for_html(target: Path, html_output_path: Path) -> str:
    base_dir = html_output_path.parent
    try:
        rel = os.path.relpath(target.resolve(), base_dir.resolve())
    except Exception:
        rel = os.path.relpath(target, base_dir)
    return rel.replace(os.sep, "/")


def estimate_text_px(text: str, px_per_char: float = 6.7) -> float:
    return len(text) * px_per_char


def truncate_to_px(text: str, max_px: float) -> str:
    if estimate_text_px(text) <= max_px:
        return text
    shortened = text
    while len(shortened) > 3 and estimate_text_px(shortened + "...") > max_px:
        shortened = shortened[:-1]
    return f"{shortened}..."


def resolve_layer_color(layer: str) -> str:
    if layer in DEFAULT_LAYER_COLORS:
        return DEFAULT_LAYER_COLORS[layer]
    digest = hashlib.sha1(layer.encode("utf-8")).hexdigest()
    color_idx = int(digest[:8], 16) % len(FALLBACK_LAYER_COLORS)
    return FALLBACK_LAYER_COLORS[color_idx]


def role_to_flags(role: str) -> set[str]:
    role_l = role.lower().strip()
    if role_l == "in":
        return {"in"}
    if role_l == "out":
        return {"out"}
    if role_l == "io":
        return {"in", "out"}
    return set()


def flags_to_role(flags: set[str]) -> str:
    if flags == {"in"}:
        return "in"
    if flags == {"out"}:
        return "out"
    if flags == {"in", "out"}:
        return "io"
    return "unknown"


def parse_direction(direction: str) -> str:
    text = direction.lower().strip()
    if not text:
        return "unknown"
    has_in = bool(re.search(r"\bin\b|input|return|recv|receive", text))
    has_out = bool(re.search(r"\bout\b|output|send|tx|transmit", text))
    has_bi = "i/o" in text or "in/out" in text or "inout" in text or "bidirectional" in text

    if has_bi or (has_in and has_out):
        return "io"
    if has_in:
        return "in"
    if has_out:
        return "out"
    return "unknown"


def classify_port_family(port: str, device_type: str | None = None) -> str:
    text = abbreviate_port_label(port).lower()

    if "power" in text or "mains" in text or re.search(r"\bdc\s+in\b", text):
        return "power"
    if "midi" in text:
        return "midi"
    if any(
        token in text
        for token in (
            "madi",
            "adat",
            "aes",
            "spdif",
            "s/pdif",
            "word clock",
            "sync",
            "opt",
            "optical",
            "clock",
        )
    ):
        return "digital"
    if any(
        token in text
        for token in (
            "usb",
            "thunderbolt",
            "hdmi",
            "displayport",
            "host/upstream",
            "upstream",
            "host",
        )
    ):
        return "computer"
    if any(token in text for token in ("ethernet", "rj45", "network", "cat5", "cat6", "eucon")):
        return "network"
    if device_type == "Network" and re.search(r"\b(?:p|port)\s*\d+\b", text):
        return "network"
    if any(
        token in text
        for token in (
            "analog",
            "an in",
            "an out",
            "line in",
            "line out",
            "mic",
            "insert",
            "aux",
            "group",
            "master",
            "mon",
            "phones",
            "headphone",
            "speaker",
            "spk",
            "tape",
            "xlr",
            "trs",
            "db25",
        )
    ):
        return "analog"
    return "other"


def allowed_port_families_for_layer(layer: str, overview_mode: bool = False) -> set[str] | None:
    if overview_mode:
        return None
    layer_l = layer.lower().strip()
    if layer_l == "audio analog":
        return {"analog"}
    if layer_l == "digital audio":
        return {"digital"}
    if layer_l == "network":
        return {"network"}
    if layer_l == "computer/data":
        return {"computer"}
    if layer_l == "midi":
        return {"midi"}
    if layer_l == "power":
        return {"power"}
    return None


def filter_ports_for_layer(
    layer: str,
    device_port_roles: dict[str, dict[str, str]],
    device_port_connected: dict[str, dict[str, bool]],
    device_types: dict[str, str],
    overview_mode: bool = False,
) -> None:
    allowed_families = allowed_port_families_for_layer(layer, overview_mode=overview_mode)
    if allowed_families is None:
        return

    for device in list(device_port_roles.keys()):
        roles = device_port_roles.get(device, {})
        connected = device_port_connected.get(device, {})
        device_type = device_types.get(device, "Other")
        kept_roles: dict[str, str] = {}
        kept_connected: dict[str, bool] = {}

        for port, role in roles.items():
            if connected.get(port, False):
                # Always keep actually wired ports for the layer.
                kept_roles[port] = role
                kept_connected[port] = True
                continue
            family = classify_port_family(port, device_type=device_type)
            if family in allowed_families:
                kept_roles[port] = role
                kept_connected[port] = False

        device_port_roles[device] = kept_roles
        device_port_connected[device] = kept_connected


def classify_device_type(device: str) -> str:
    name = device.lower()
    if any(token in name for token in ("streamdeck", "avid s1", "dock", "mac mini", "computer", "tv screen")):
        return "Computer / Control"
    if "patchbay" in name or "desk patch" in name or "patch" in name:
        return "Patchbay"
    if "switch" in name or "router" in name or "ethernet" in name:
        return "Network"
    if any(token in name for token in ("gs3000", "allen & heath", "mixer", "console")):
        return "Console / Mixer"
    if any(token in name for token in ("ms-16", "tascam")):
        return "Tape Machine"
    if any(token in name for token in ("rme", "ssl", "madi", "interface", "converter")):
        return "Interface / Converter"
    if any(token in name for token in ("asp 880", "asp880", "voice master", "voicemaster", "preamp", "500 series")):
        return "Preamps / Channel Strip"
    if any(token in name for token in ("a800", "headphone amp", "monitor controller", "monitor control")):
        return "Amplifier / Monitor Control"
    if any(token in name for token in ("tannoy", "atc", "auratone", "speaker")):
        return "Speaker / Monitor"
    if any(token in name for token in ("fx", "finalizer", "vitalizer", "clarity", "sony r7", "rack")):
        return "Outboard / FX"
    if any(token in name for token in ("mic", "di", "talkback")):
        return "Microphone / DI"
    return "Other"


def is_patchbay_device(device: str) -> bool:
    return classify_device_type(device) == "Patchbay"


def maybe_merge_monitor_pair(device: str, port: str) -> tuple[str, str]:
    """Merge monitor pairs (Left/Right units) into one visual device."""
    if classify_device_type(device) != "Speaker / Monitor":
        return device, port

    match = re.match(r"^(.*?)[\s_-]+(left|right|l|r)$", device.strip(), flags=re.IGNORECASE)
    if not match:
        return device, port

    base = match.group(1).strip()
    side_raw = match.group(2).lower()
    side = "L" if side_raw in ("left", "l") else "R"

    if re.search(r"\b(l|r|left|right)\b", port, flags=re.IGNORECASE):
        return base, port
    return base, f"{port} {side}"


def abbreviate_port_label(label: str) -> str:
    text = label
    for pattern, replacement in PORT_ABBREVIATIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def resolve_connection_color(connection: Connection) -> str:
    return resolve_connection_family_and_color(connection)[1]


def resolve_connection_family_and_color(connection: Connection) -> tuple[str, str]:
    haystack = " ".join(
        [
            connection.layer,
            connection.signal_type,
            connection.cable_type,
            connection.connection_type,
            connection.source_jack,
            connection.dest_jack,
            connection.notes,
        ]
    ).lower()

    if "power" in haystack or "ac mains" in haystack or re.search(r"\bdc\s+in\b", haystack):
        return ("Power", "#a16207")
    if "madi" in haystack:
        return ("MADI", "#2563eb")
    if any(token in haystack for token in ("word clock", "clock", "sync", "wc")):
        return ("Clock/Sync", "#14b8a6")
    if "speaker" in haystack:
        return ("Speaker", "#f97316")
    if "midi" in haystack:
        return ("MIDI", "#9333ea")
    if any(token in haystack for token in ("network", "ethernet", "rj45", "cat6", "cat5", "eucon")):
        return ("Network", "#0f766e")
    if any(token in haystack for token in ("video", "hdmi", "display")):
        return ("Video", "#ca8a04")
    if any(token in haystack for token in ("thunderbolt", "usb", "computer data", "computer/data")):
        return ("Computer/Data", "#64748b")
    if any(token in haystack for token in ("adat", "aes", "spdif", "toslink", "digital")):
        return ("Digital Audio", "#06b6d4")
    if any(token in haystack for token in ("analog", "trs", "xlr", "db25", "line", "mic", "instrument")):
        return ("Analog Audio", "#dc2626")
    return (connection.layer, resolve_layer_color(connection.layer))


def explicit_power_group(connection: Connection) -> str | None:
    """Return a normalized electrical circuit label when one is documented."""
    group_text = " ".join(
        [
            connection.notes,
            connection.source_jack,
            connection.dest_jack,
        ]
    )
    match = re.search(r"\b(?:group|groep)\s*[-#:]*\s*(\d+)\b", group_text, flags=re.IGNORECASE)
    if not match:
        return None
    return f"Group {int(match.group(1))}"


def resolve_power_groups(connections: list[Connection]) -> dict[Connection, str]:
    """Resolve circuit groups and carry them through downstream power devices."""
    groups = {
        connection: group
        for connection in connections
        if (group := explicit_power_group(connection)) is not None
    }

    # A downstream supply (PDU, external PSU, switcher, etc.) belongs to the
    # single circuit feeding it. Repeat because a chain can contain several
    # distribution stages. Devices fed by multiple circuits remain unassigned.
    while True:
        incoming_groups: dict[str, set[str]] = defaultdict(set)
        for connection, group in groups.items():
            incoming_groups[connection.dest_device].add(group)

        added = False
        for connection in connections:
            if connection in groups:
                continue
            candidates = incoming_groups.get(connection.source_device, set())
            if len(candidates) == 1:
                groups[connection] = next(iter(candidates))
                added = True
        if not added:
            break

    return groups


def explicit_power_branch(connection: Connection) -> str | None:
    """Recognize the Low, High, and Digital banks on a power sequencer."""
    branch_text = " ".join([connection.source_jack, connection.notes]).lower()
    if "high current" in branch_text or re.search(r"(?:^|/)\s*(?:high|amp)\b", branch_text):
        return "High"
    if "digital outlet" in branch_text or re.search(r"(?:^|/)\s*(?:digital|dig)\b", branch_text):
        return "Digital"
    if "analog outlet" in branch_text or re.search(r"(?:^|/)\s*low\b", branch_text):
        return "Low"
    return None


def resolve_power_branches(connections: list[Connection]) -> dict[Connection, str]:
    """Carry sequencer-bank labels through downstream distribution stages."""
    branches = {
        connection: branch
        for connection in connections
        if (branch := explicit_power_branch(connection)) is not None
    }
    while True:
        incoming_branches: dict[str, set[str]] = defaultdict(set)
        for connection, branch in branches.items():
            incoming_branches[connection.dest_device].add(branch)

        added = False
        for connection in connections:
            if connection in branches:
                continue
            candidates = incoming_branches.get(connection.source_device, set())
            if len(candidates) == 1:
                branches[connection] = next(iter(candidates))
                added = True
        if not added:
            break
    return branches


def power_group_color(group: str) -> str:
    match = re.fullmatch(r"Group (\d+)", group)
    if not match:
        return DEFAULT_LAYER_COLORS["Power"]
    group_number = max(1, int(match.group(1)))
    return POWER_GROUP_COLORS[(group_number - 1) % len(POWER_GROUP_COLORS)]


def power_visual_styles(connections: list[Connection]) -> dict[Connection, tuple[str, str]]:
    """Return dedicated Power-SVG legend labels and colours for each route."""
    groups = resolve_power_groups(connections)
    branches = resolve_power_branches(connections)
    styles: dict[Connection, tuple[str, str]] = {}
    for connection, group in groups.items():
        branch = branches.get(connection)
        if group == "Group 1" and branch in POWER_BRANCH_COLORS:
            styles[connection] = (
                f"{group} · {POWER_BRANCH_LABELS[branch]}",
                POWER_BRANCH_COLORS[branch],
            )
        else:
            styles[connection] = (group, power_group_color(group))
    return styles


def power_groups_by_device(connections: list[Connection]) -> dict[str, str]:
    """Return the single inherited power circuit feeding each destination device."""
    power_connections = [
        connection
        for connection in connections
        if resolve_connection_family_and_color(connection)[0] == "Power"
    ]
    groups_by_connection = resolve_power_groups(power_connections)
    candidates_by_device: dict[str, set[str]] = defaultdict(set)
    for connection, group in groups_by_connection.items():
        candidates_by_device[connection.dest_device].add(group)
    return {
        device: next(iter(groups))
        for device, groups in candidates_by_device.items()
        if len(groups) == 1
    }


def normalize_matrix_family(value: str) -> str:
    token = value.strip().upper()
    if token in {"AUDIO", "COMP", "DIGI", "NETWORK"}:
        return token
    if token in {"ANALOG", "ANALOG AUDIO", "SPEAKER"}:
        return "AUDIO"
    if token in {"DIGITAL", "DIGITAL AUDIO", "MADI", "SPDIF", "AES", "ADAT", "CLOCK", "SYNC", "MIDI"}:
        return "DIGI"
    if token in {"COMPUTER", "DATA", "VIDEO", "USB", "THUNDERBOLT"}:
        return "COMP"
    if token in {"NET", "ETHERNET", "RJ45", "CAT", "CAT6", "CAT5"}:
        return "NETWORK"
    return token


def short_transport_tag(connection: Connection) -> str:
    primary = " ".join(
        [
            connection.cable_type,
            connection.signal_type,
            connection.source_jack,
            connection.dest_jack,
        ]
    ).lower()
    haystack = " ".join(
        [
            connection.cable_type,
            connection.signal_type,
            connection.layer,
            connection.source_jack,
            connection.dest_jack,
            connection.notes,
        ]
    ).lower()
    if "thunderbolt" in primary:
        return "TB4"
    if "hdmi" in primary:
        return "HDMI"
    if any(token in primary for token in ("ethernet", "rj45", "cat6", "cat5", "network")):
        return "CAT6"
    if "usb" in primary:
        return "USB"
    if "thunderbolt" in haystack:
        return "TB4"
    if "hdmi" in haystack:
        return "HDMI"
    if any(token in haystack for token in ("ethernet", "rj45", "cat6", "cat5", "network")):
        return "CAT6"
    if "usb" in haystack:
        return "USB"
    if "madi" in haystack and any(token in haystack for token in ("opt", "optical", "fiber")):
        return "MADI-OPT"
    if "madi" in haystack:
        return "MADI"
    if "spdif" in haystack or "s/pdif" in haystack:
        if any(token in haystack for token in ("optical", "toslink", "fiber")):
            return "SPDIF-OPT"
        return "SPDIF"
    if "aes" in haystack:
        return "AES"
    if "adat" in haystack:
        return "ADAT"
    if any(token in haystack for token in ("optical", "fiber", "toslink")):
        return "FIBER"
    if "digital" in haystack:
        return "DIG"
    if "midi" in haystack:
        return "MIDI"
    if "speaker" in haystack:
        return "SPK"
    if "analog" in haystack:
        return "ANLG"
    return ""


def detect_connection_type_tag(raw: str) -> str:
    lowered = raw.strip().lower()
    if not lowered:
        return ""
    mc_match = re.search(r"\bmc\s*[-_/ ]*\s*(\d+)\b", lowered)
    if not mc_match:
        mc_match = re.search(r"\b(\d+)\s*(?:ch|channel)\s*(?:multi|multicable|mc)\b", lowered)
    if not mc_match:
        mc_match = re.search(r"\b(\d+)\s*channel\s*multicable\b", lowered)
    if mc_match:
        return f"MC{int(mc_match.group(1))}"

    if re.search(r"\b(st|stereo)\b", lowered):
        return "ST"

    return ""


def normalize_connection_type(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    detected = detect_connection_type_tag(text)
    if detected:
        return detected

    compact = re.sub(r"[^A-Za-z0-9]+", "", text).upper()
    return compact


def short_power_connector_tag(connection: Connection) -> str:
    """Return a compact connector-only tag for POWER diagram labels."""
    compact = re.sub(r"[^A-Za-z0-9]+", "", connection.connection_type).upper()
    if not compact:
        return ""

    if any(token in compact for token in ("VERIFY", "UNKNOWN", "TBD")):
        return "TBD"

    iec_match = re.search(r"(?:IEC)?(C(?:5|7|13|14|15|19|20))", compact)
    if iec_match:
        return iec_match.group(1)

    if "RPS11" in compact:
        return "RPS11"
    if "ADAPTER" in compact:
        return "ADAPTER"
    if "SCHUKO" in compact:
        return "SCHUKO"
    return compact


def display_connection_type_tag(connection: Connection) -> str:
    cable_id = connection.cable_id.strip().upper()
    if cable_id.startswith("POWER-"):
        return short_power_connector_tag(connection)
    if connection.connection_type:
        return connection.connection_type
    if cable_id.startswith("COMP-") or cable_id.startswith("DIGI-") or cable_id.startswith("NETWORK-"):
        return short_transport_tag(connection)
    return ""


def render_cable_label(connection: Connection) -> str:
    cable_id = connection.cable_id.strip()
    tag = display_connection_type_tag(connection)
    if tag:
        return f"{cable_id} {tag}"
    return cable_id


def parse_cable_series(cable_id: str) -> tuple[str, int, int] | None:
    match = re.match(r"^([A-Za-z]+)-?(\d+)$", cable_id.strip())
    if not match:
        return None
    prefix = match.group(1).upper()
    number_text = match.group(2)
    number = int(number_text)
    return prefix, number, len(number_text)


def format_cable_span(prefix: str, start_num: int, end_num: int, width: int) -> str:
    return f"{prefix}-{start_num:0{width}d}-{end_num:0{width}d}"


def build_bundle_label_plan(
    ordered_connections: list[Connection],
    min_run: int = 3,
) -> tuple[dict[int, str], set[int]]:
    grouped: dict[tuple[str, str, str, int], list[tuple[int, int]]] = defaultdict(list)
    for idx, connection in enumerate(ordered_connections):
        parsed = parse_cable_series(connection.cable_id)
        if not parsed:
            continue
        prefix, number, width = parsed
        key = (connection.source_device, connection.dest_device, prefix, width)
        grouped[key].append((idx, number))

    label_overrides: dict[int, str] = {}
    suppress_labels: set[int] = set()

    def run_is_explicit_stereo(run_items: list[tuple[int, int]]) -> bool:
        tags = [
            display_connection_type_tag(ordered_connections[conn_idx]).upper()
            for conn_idx, _ in run_items
            if display_connection_type_tag(ordered_connections[conn_idx])
        ]
        return bool(tags) and all(tag == "ST" for tag in tags)

    def run_is_stack_like(run_items: list[tuple[int, int]]) -> bool:
        # Bundle only when the run represents a visually stacked trunk/fan
        # (shared source or shared destination). Keep labels per-wire for
        # 1:1 sequential maps so names don't disappear.
        src_ports = {
            ordered_connections[conn_idx].source_jack
            for conn_idx, _ in run_items
        }
        dst_ports = {
            ordered_connections[conn_idx].dest_jack
            for conn_idx, _ in run_items
        }
        return len(src_ports) == 1 or len(dst_ports) == 1

    for key, values in grouped.items():
        ordered = sorted(values, key=lambda item: item[1])
        run: list[tuple[int, int]] = []
        for item in ordered:
            if not run or item[1] == run[-1][1] + 1:
                run.append(item)
                continue

            if len(run) >= min_run and not run_is_explicit_stereo(run) and run_is_stack_like(run):
                prefix = key[2]
                width = key[3]
                start_num = run[0][1]
                end_num = run[-1][1]
                mid_idx = run[len(run) // 2][0]
                run_tags = {
                    display_connection_type_tag(ordered_connections[conn_idx])
                    for conn_idx, _ in run
                    if display_connection_type_tag(ordered_connections[conn_idx])
                }
                label = format_cable_span(prefix, start_num, end_num, width)
                if len(run_tags) == 1:
                    label = f"{label} {next(iter(run_tags))}"
                label_overrides[mid_idx] = label
                for conn_idx, _ in run:
                    if conn_idx != mid_idx:
                        suppress_labels.add(conn_idx)
            run = [item]

        if len(run) >= min_run and not run_is_explicit_stereo(run) and run_is_stack_like(run):
            prefix = key[2]
            width = key[3]
            start_num = run[0][1]
            end_num = run[-1][1]
            mid_idx = run[len(run) // 2][0]
            run_tags = {
                display_connection_type_tag(ordered_connections[conn_idx])
                for conn_idx, _ in run
                if display_connection_type_tag(ordered_connections[conn_idx])
            }
            label = format_cable_span(prefix, start_num, end_num, width)
            if len(run_tags) == 1:
                label = f"{label} {next(iter(run_tags))}"
            label_overrides[mid_idx] = label
            for conn_idx, _ in run:
                if conn_idx != mid_idx:
                    suppress_labels.add(conn_idx)

    return label_overrides, suppress_labels


def collapse_stereo_headphone_connections_for_render(
    connections: list[Connection],
) -> list[Connection]:
    """Collapse mono L/R pairs feeding HA stereo inputs into one rendered link.

    Keeps matrix data untouched while improving diagram readability for stereo
    destination ports such as "HA In 1..N".
    """

    grouped: dict[tuple[str, str, str, str], list[tuple[int, int, Connection]]] = defaultdict(list)
    passthrough: list[tuple[int, Connection]] = []

    for idx, connection in enumerate(connections):
        if connection.dest_device != "IMG STAGELINE PPA-100/SW":
            passthrough.append((idx, connection))
            continue
        if not re.match(r"^HA In\s+\d+$", connection.dest_jack.strip(), flags=re.IGNORECASE):
            passthrough.append((idx, connection))
            continue
        src_single = extract_port_single(abbreviate_port_label(connection.source_jack))
        if not src_single:
            passthrough.append((idx, connection))
            continue
        base_key, channel = src_single
        grouped[
            (
                connection.source_device,
                connection.dest_device,
                connection.dest_jack,
                base_key,
            )
        ].append((idx, channel, connection))

    consumed: set[int] = set()
    rendered: list[tuple[int, Connection]] = list(passthrough)

    for _, values in grouped.items():
        ordered = sorted(values, key=lambda item: (item[1], natural_key(item[2].cable_id)))
        i = 0
        while i < len(ordered):
            idx_a, ch_a, conn_a = ordered[i]
            if i + 1 >= len(ordered):
                rendered.append((idx_a, conn_a))
                i += 1
                continue

            idx_b, ch_b, conn_b = ordered[i + 1]
            # Stereo channels should collapse as odd/even pairs: 1+2, 3+4, ...
            if (ch_a % 2 == 0) or ch_b != ch_a + 1:
                rendered.append((idx_a, conn_a))
                i += 1
                continue

            if idx_a in consumed or idx_b in consumed:
                i += 2
                continue

            consumed.add(idx_a)
            consumed.add(idx_b)
            stereo_conn = Connection(
                cable_id=conn_a.cable_id,
                source_device=conn_a.source_device,
                source_jack=f"{conn_a.source_jack}+{conn_b.source_jack}",
                dest_device=conn_a.dest_device,
                dest_jack=conn_a.dest_jack,
                layer=conn_a.layer,
                signal_type=conn_a.signal_type,
                status=conn_a.status,
                cable_type=conn_a.cable_type,
                connection_type="ST",
                notes=conn_a.notes,
            )
            rendered.append((idx_a, stereo_conn))
            i += 2

    rendered.sort(key=lambda item: item[0])
    return [item[1] for item in rendered]


def reciprocal_single_link_protocol(connection: Connection) -> str:
    """Return a physical-link key for transports drawn as one duplex cable."""
    haystack = " ".join(
        [
            connection.connection_type,
            connection.cable_type,
            connection.signal_type,
            connection.source_jack,
            connection.dest_jack,
        ]
    ).lower()
    if "madi" in haystack:
        if any(token in haystack for token in ("optical", "fiber", "fibre", "madi-opt")):
            return "MADI-OPTICAL"
        if any(token in haystack for token in ("coax", "bnc")):
            return "MADI-COAX"
        return "MADI"
    if "thunderbolt" in haystack or re.search(r"\btb\s*[2345]\b", haystack):
        return "THUNDERBOLT"
    if "hdmi" in haystack:
        return "HDMI"
    if re.search(r"\busb(?:\s*[- ]?[abc]|\s*\d)?\b", haystack):
        return "USB"
    if any(token in haystack for token in ("ethernet", "rj45", "cat5", "cat6")):
        return "ETHERNET"
    return ""


def reciprocal_port_signature(port: str) -> str:
    """Remove direction words while retaining connector/medium identity."""
    normalized = port.lower().replace("s/pdif", "spdif")
    normalized = re.sub(r"\b(?:inputs?|outputs?|in|out|upstream|downstream)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def combined_reciprocal_cable_id(first: str, second: str) -> str:
    first_series = parse_cable_series(first)
    second_series = parse_cable_series(second)
    if first_series and second_series and first_series[0] == second_series[0]:
        prefix, first_number, width = first_series
        second_number = second_series[1]
        return f"{prefix}-{first_number:0{width}d}↔{second_number:0{width}d}"
    return f"{first}↔{second}"


def collapse_reciprocal_single_links_for_render(
    connections: list[Connection],
) -> tuple[list[Connection], set[str]]:
    """Collapse matching A→B/B→A rows into one bidirectional visual link.

    Patch data remains directional and untouched. The collapse applies only
    when the transport and direction-neutral port identities match at both
    devices, so two unrelated cables between the same hosts stay separate.
    """
    consumed: set[int] = set()
    collapsed_ids: set[str] = set()
    rendered: list[Connection] = []

    for index, first in enumerate(connections):
        if index in consumed:
            continue
        protocol = reciprocal_single_link_protocol(first)
        if not protocol or first.source_device == first.dest_device:
            rendered.append(first)
            continue

        first_source_signature = reciprocal_port_signature(first.source_jack)
        first_dest_signature = reciprocal_port_signature(first.dest_jack)
        partner_index: int | None = None
        for candidate_index in range(index + 1, len(connections)):
            if candidate_index in consumed:
                continue
            second = connections[candidate_index]
            if reciprocal_single_link_protocol(second) != protocol:
                continue
            if (
                second.source_device != first.dest_device
                or second.dest_device != first.source_device
            ):
                continue
            if reciprocal_port_signature(second.dest_jack) != first_source_signature:
                continue
            if reciprocal_port_signature(second.source_jack) != first_dest_signature:
                continue
            partner_index = candidate_index
            break

        if partner_index is None:
            rendered.append(first)
            continue

        second = connections[partner_index]
        consumed.add(partner_index)
        combined_id = combined_reciprocal_cable_id(first.cable_id, second.cable_id)
        collapsed_ids.add(combined_id)
        rendered.append(
            Connection(
                cable_id=combined_id,
                source_device=first.source_device,
                source_jack=first.source_jack,
                dest_device=first.dest_device,
                dest_jack=first.dest_jack,
                layer=first.layer,
                signal_type=first.signal_type,
                status=first.status,
                cable_type=first.cable_type,
                connection_type=first.connection_type,
                notes=first.notes,
            )
        )

    return rendered, collapsed_ids


def collapse_multichannel_connections_for_overview(
    connections: list[Connection],
    min_channels: int = 4,
) -> list[Connection]:
    """Collapse dense sequential 1:1 runs into one multichannel overview link."""

    candidates: dict[
        tuple[str, str, str, str, str, int, str, str, str, str],
        list[tuple[int, int, int, int, Connection]],
    ] = defaultdict(list)
    passthrough: list[tuple[int, Connection]] = []

    for idx, connection in enumerate(connections):
        src_single = extract_port_single(abbreviate_port_label(connection.source_jack))
        dst_single = extract_port_single(abbreviate_port_label(connection.dest_jack))
        cable_series = parse_cable_series(connection.cable_id)
        if not src_single or not dst_single or not cable_series:
            passthrough.append((idx, connection))
            continue

        src_base, src_idx = src_single
        dst_base, dst_idx = dst_single
        prefix, cable_num, width = cable_series
        key = (
            connection.source_device,
            connection.dest_device,
            src_base,
            dst_base,
            prefix,
            width,
            connection.layer,
            connection.signal_type,
            connection.cable_type,
            connection.status,
        )
        candidates[key].append((idx, src_idx, dst_idx, cable_num, connection))

    consumed: set[int] = set()
    rendered: list[tuple[int, Connection]] = list(passthrough)

    for key, values in candidates.items():
        ordered = sorted(values, key=lambda item: (item[1], item[2], item[3], item[0]))
        run: list[tuple[int, int, int, int, Connection]] = []
        for item in ordered:
            if not run:
                run = [item]
                continue

            prev = run[-1]
            src_is_next = item[1] == prev[1] + 1
            dst_is_next = item[2] == prev[2] + 1
            cable_is_next = item[3] == prev[3] + 1
            if src_is_next and dst_is_next and cable_is_next:
                run.append(item)
                continue

            if len(run) >= min_channels:
                first = run[0][4]
                prefix = key[4]
                width = key[5]
                start_num = run[0][3]
                end_num = run[-1][3]
                source_ports = "+".join(entry[4].source_jack for entry in run)
                dest_ports = "+".join(entry[4].dest_jack for entry in run)
                bundle = Connection(
                    cable_id=format_cable_span(prefix, start_num, end_num, width),
                    source_device=first.source_device,
                    source_jack=source_ports,
                    dest_device=first.dest_device,
                    dest_jack=dest_ports,
                    layer=first.layer,
                    signal_type=first.signal_type,
                    status=first.status,
                    cable_type=first.cable_type,
                    connection_type=f"MC{len(run)}",
                    notes=first.notes,
                )
                rendered.append((run[0][0], bundle))
                consumed.update(entry[0] for entry in run)
            else:
                for entry in run:
                    rendered.append((entry[0], entry[4]))
            run = [item]

        if run:
            if len(run) >= min_channels:
                first = run[0][4]
                prefix = key[4]
                width = key[5]
                start_num = run[0][3]
                end_num = run[-1][3]
                source_ports = "+".join(entry[4].source_jack for entry in run)
                dest_ports = "+".join(entry[4].dest_jack for entry in run)
                bundle = Connection(
                    cable_id=format_cable_span(prefix, start_num, end_num, width),
                    source_device=first.source_device,
                    source_jack=source_ports,
                    dest_device=first.dest_device,
                    dest_jack=dest_ports,
                    layer=first.layer,
                    signal_type=first.signal_type,
                    status=first.status,
                    cable_type=first.cable_type,
                    connection_type=f"MC{len(run)}",
                    notes=first.notes,
                )
                rendered.append((run[0][0], bundle))
                consumed.update(entry[0] for entry in run)
            else:
                for entry in run:
                    rendered.append((entry[0], entry[4]))

    deduped: list[tuple[int, Connection]] = []
    seen_single: set[int] = set()
    for idx, connection in rendered:
        if idx in consumed and not connection.connection_type.upper().startswith("MC"):
            continue
        if idx in seen_single and not connection.connection_type.upper().startswith("MC"):
            continue
        if not connection.connection_type.upper().startswith("MC"):
            seen_single.add(idx)
        deduped.append((idx, connection))

    deduped.sort(key=lambda item: item[0])
    return [item[1] for item in deduped]


def is_bidirectional_connection(connection: Connection) -> bool:
    haystack = " ".join(
        [
            connection.layer,
            connection.signal_type,
            connection.cable_type,
            connection.connection_type,
            connection.source_jack,
            connection.dest_jack,
            connection.notes,
        ]
    ).lower()
    bidirectional_patterns = [
        r"\busb\b",
        r"\bthunderbolt\b",
        r"\bethernet\b",
        r"\bnetwork control\b",
        r"\blan\b",
        r"\beucon\b",
        r"\brj45\b",
        r"\bcat5\b",
        r"\bcat6\b",
    ]
    return any(re.search(pattern, haystack) for pattern in bidirectional_patterns)


def compute_signal_stages(devices: list[str], connections: list[Connection]) -> dict[str, int]:
    adjacency: dict[str, set[str]] = {device: set() for device in devices}
    indegree: dict[str, int] = {device: 0 for device in devices}
    source_count: Counter[str] = Counter()
    dest_count: Counter[str] = Counter()

    for connection in connections:
        source_count[connection.source_device] += 1
        dest_count[connection.dest_device] += 1
        if is_bidirectional_connection(connection):
            continue
        src = connection.source_device
        dst = connection.dest_device
        if dst not in adjacency[src]:
            adjacency[src].add(dst)
            indegree[dst] += 1

    stage: dict[str, int] = {device: 0 for device in devices}
    queue = sorted([device for device in devices if indegree[device] == 0], key=natural_key)
    processed: set[str] = set()

    while queue:
        node = queue.pop(0)
        processed.add(node)
        for neighbor in sorted(adjacency[node], key=natural_key):
            stage[neighbor] = max(stage[neighbor], stage[node] + 1)
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)
                queue.sort(key=natural_key)

    if len(processed) != len(devices):
        for device in devices:
            if device in processed:
                continue
            flow = source_count[device] - dest_count[device]
            stage[device] = 0 if flow >= 0 else 1

    return stage


def device_type_sort_key(device_type: str) -> tuple[int, str]:
    try:
        return (DEVICE_TYPE_ORDER.index(device_type), device_type.lower())
    except ValueError:
        return (999, device_type.lower())


def port_signal_group(port: str) -> int:
    text = port.lower()
    analog_tokens = (
        "analog",
        "an in",
        "an out",
        "line",
        "mic",
        "instrument",
        "speaker",
        "spk",
        "tape",
        "insert",
        "aux",
        "group",
        "monitor",
        "main in",
        "main out",
        "headphone",
        "phones",
        "ha in",
        "db25",
        "xlr",
        "trs",
    )
    digital_tokens = (
        "adat",
        "aes",
        "madi",
        "spdif",
        "s/pdif",
        "optical",
        "toslink",
        "word clock",
        "clock",
        "sync",
        "midi",
        "digital",
    )
    computer_tokens = (
        "usb",
        "thunderbolt",
        "ethernet",
        "rj45",
        "hdmi",
        "network",
    )

    if any(token in text for token in analog_tokens):
        return 0
    if any(token in text for token in digital_tokens):
        return 1
    if any(token in text for token in computer_tokens):
        return 2
    return 0


def extract_port_range(port: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*-\s*(\d+)", port)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if end < start:
        start, end = end, start
    return start, end


def extract_port_single(port: str) -> tuple[str, int] | None:
    text = port.strip()
    # Allow numeric parsing for labels with trailing qualifiers, e.g.
    # "Line Out 1 (XLR)" or "Port 2 [RJ45]".
    while True:
        cleaned = re.sub(r"\s*(\([^)]*\)|\[[^\]]*\])\s*$", "", text).strip()
        if cleaned == text:
            break
        text = cleaned
    match = re.match(r"^(.*?)(\d+)\s*$", text)
    if not match:
        return None
    base = re.sub(r"\s+", " ", match.group(1).strip()).lower()
    if not base:
        return None
    return base, int(match.group(2))


def extract_port_range_with_base(port: str) -> tuple[str, int, int] | None:
    match = re.search(r"(\d+)\s*-\s*(\d+)", port)
    if not match:
        return None
    base = re.sub(r"\s+", " ", port[: match.start()].strip()).lower()
    if not base:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    if end < start:
        start, end = end, start
    return base, start, end


def suppress_redundant_range_ports(ports: list[str]) -> set[str]:
    # If a broad range port (e.g. "AN Out 1-8") is fully represented by
    # individual ports ("AN Out 1"..."AN Out 8"), hide the broad duplicate.
    singles_by_base: dict[str, set[int]] = defaultdict(set)
    for port in ports:
        single = extract_port_single(abbreviate_port_label(port))
        if single:
            base, index = single
            singles_by_base[base].add(index)

    suppressed: set[str] = set()
    for port in ports:
        ranged = extract_port_range_with_base(abbreviate_port_label(port))
        if not ranged:
            continue
        base, start, end = ranged
        if end - start < 1:
            continue
        if all(idx in singles_by_base.get(base, set()) for idx in range(start, end + 1)):
            suppressed.add(port)
    return suppressed


def format_port_interval_label(prefix: str, start: int, end: int, width: int) -> str:
    start_text = f"{start:0{width}d}" if width > 1 else str(start)
    end_text = f"{end:0{width}d}" if width > 1 else str(end)
    clean_prefix = prefix.strip()
    if start == end:
        return f"{clean_prefix} {start_text}".strip()
    return f"{clean_prefix} {start_text}-{end_text}".strip()


def interval_remainder(start: int, end: int, covered: set[int]) -> list[tuple[int, int]]:
    remaining = [index for index in range(start, end + 1) if index not in covered]
    if not remaining:
        return []

    chunks: list[tuple[int, int]] = []
    run_start = remaining[0]
    run_end = remaining[0]
    for index in remaining[1:]:
        if index == run_end + 1:
            run_end = index
            continue
        chunks.append((run_start, run_end))
        run_start = index
        run_end = index
    chunks.append((run_start, run_end))
    return chunks


def normalize_range_availability(
    port_roles: dict[str, str],
    port_connected: dict[str, bool],
) -> tuple[dict[str, str], dict[str, bool]]:
    """Remove/trim aggregate ranges when narrower ports are present.

    Example: if a device has both "AN Out 1-24" and "AN Out 1-2", the broad
    range is reduced to "AN Out 3-24" (or removed if fully covered).
    """

    range_ports: list[tuple[str, str, int, int, str, int]] = []
    single_ports: dict[str, set[int]] = defaultdict(set)

    for port in port_roles.keys():
        abbreviated = abbreviate_port_label(port)
        ranged = extract_port_range_with_base(abbreviated)
        if ranged:
            base_key, start, end = ranged
            raw_match = re.search(r"(\d+)\s*-\s*(\d+)", port)
            if raw_match:
                prefix_raw = port[: raw_match.start()].strip() or port.strip()
                n1 = raw_match.group(1)
                n2 = raw_match.group(2)
                width = max(len(n1), len(n2)) if (n1.startswith("0") or n2.startswith("0")) else 1
            else:
                prefix_raw = port.strip()
                width = 1
            range_ports.append((port, base_key, start, end, prefix_raw, width))
            continue

        single = extract_port_single(abbreviated)
        if single:
            base_key, index = single
            single_ports[base_key].add(index)

    if not range_ports:
        return dict(port_roles), dict(port_connected)

    normalized_roles = dict(port_roles)
    normalized_connected = dict(port_connected)

    for port, base_key, start, end, prefix_raw, width in range_ports:
        # Keep explicitly wired aggregate ports intact (e.g. patchbay trunks).
        # The de-duplication target here is duplicate "available" ranges.
        if port_connected.get(port, False):
            continue

        covered: set[int] = set()

        for index in single_ports.get(base_key, set()):
            if start <= index <= end:
                covered.add(index)

        for other_port, other_base, other_start, other_end, _, _ in range_ports:
            if other_port == port or other_base != base_key:
                continue
            if other_start < start or other_end > end:
                continue
            if other_start == start and other_end == end:
                continue
            covered.update(range(other_start, other_end + 1))

        if not covered:
            continue

        normalized_roles.pop(port, None)
        normalized_connected.pop(port, None)

        for rem_start, rem_end in interval_remainder(start, end, covered):
            rem_label = format_port_interval_label(prefix_raw, rem_start, rem_end, width)
            if rem_label in normalized_roles:
                continue
            normalized_roles[rem_label] = port_roles[port]
            normalized_connected[rem_label] = False

    return normalized_roles, normalized_connected


def port_range_sort_parts(port: str) -> tuple[int, int, int]:
    ranged = extract_port_range(port)
    if not ranged:
        return (0, 10_000, 10_000)
    start, end = ranged
    span = end - start + 1
    # Put broad aggregate ranges below narrower channel pairs/singles.
    broad_flag = 1 if span > 4 else 0
    return (broad_flag, start, end)


def port_numeric_anchor(port: str) -> int:
    ranged = extract_port_range(port)
    if ranged:
        return ranged[0]
    single = extract_port_single(abbreviate_port_label(port))
    if single:
        return single[1]
    return 10_000


def desk_signal_flow_rank(device: str, port: str, side: str) -> tuple[int, int] | None:
    device_l = device.lower()
    if "allen & heath gs3000" not in device_l and "gs3000" not in device_l:
        return None

    text = abbreviate_port_label(port).lower()
    numeric = port_numeric_anchor(port)

    if side == "in":
        if "mic in" in text:
            return (0, numeric)
        if "line in" in text:
            return (1, numeric)
        if "insert ret" in text or "insert return" in text:
            return (2, numeric)
        if "st aux in" in text or "stereo in" in text:
            return (3, numeric)
        if "tape in" in text:
            # Keep tape returns later on the desk so they can visually align
            # with Tape Out rows on the right side.
            return (4, numeric)
        if "2-track in" in text or "2 track in" in text:
            return (5, numeric)
        if "monitor in" in text or "mon in" in text:
            return (6, numeric)
        return (7, numeric)

    if "direct out" in text:
        return (0, numeric)
    if "insert send" in text:
        return (1, numeric)
    if "aux out" in text or ("aux" in text and "out" in text):
        return (2, numeric)
    if "group out" in text:
        return (3, numeric)
    if "tube out" in text:
        return (4, numeric)
    if "tape out" in text:
        return (5, numeric)
    if "master out" in text:
        return (6, numeric)
    if "monitor out" in text or "mon out" in text:
        return (7, numeric)
    if "phones out" in text or "headphone out" in text:
        return (8, numeric)
    if "2-track out" in text or "2 track out" in text:
        return (9, numeric)
    return (10, numeric)


def generic_signal_flow_rank(port: str, side: str) -> tuple[int, int]:
    """Default signal-flow rank for non-desk devices."""

    text = abbreviate_port_label(port).lower()
    numeric = port_numeric_anchor(port)

    digital_tokens = (
        "adat",
        "aes",
        "madi",
        "spdif",
        "s/pdif",
        "optical",
        "toslink",
        "word clock",
        "clock",
        "sync",
        "midi",
        "digital",
    )
    computer_tokens = (
        "usb",
        "thunderbolt",
        "ethernet",
        "rj45",
        "hdmi",
        "network",
    )

    if side == "in":
        combo_mic_line = (
            "mic/line in" in text
            or "mic line in" in text
            or ("combo" in text and "mic" in text and "line in" in text)
        )
        if combo_mic_line:
            # Keep combo sockets grouped with line-level inputs, but after
            # dedicated line inputs for interface readability (e.g. RME UFX).
            return (2.5, numeric)
        if "mic" in text:
            return (0, numeric)
        # "di" must be matched as a complete token: a substring check also
        # classifies MADI and MIDI ports as instrument/DI inputs.
        if (
            any(token in text for token in ("inst", "instrument", "hi-z", "hiz"))
            or re.search(r"\bdi\b", text)
        ):
            return (1, numeric)
        if "line in" in text or "an in" in text or "analog in" in text:
            return (2, numeric)
        if "tape in" in text:
            return (3, numeric)
        if "insert ret" in text or "insert return" in text:
            return (4, numeric)
        if "aux in" in text or "st aux in" in text or "return" in text:
            return (5, numeric)
        if (
            "monitor in" in text
            or "mon in" in text
            or "main in" in text
            or "speaker input" in text
            or "spk in" in text
            or "headphone in" in text
            or "phones in" in text
            or "ha in" in text
        ):
            return (6, numeric)
        if any(token in text for token in digital_tokens):
            return (7, numeric)
        if any(token in text for token in computer_tokens):
            return (8, numeric)
        return (9, numeric)

    if "mic out" in text:
        return (0, numeric)
    if "line out" in text or "an out" in text or "analog out" in text:
        return (1, numeric)
    if "direct out" in text:
        return (2, numeric)
    if "insert send" in text:
        return (3, numeric)
    if "group out" in text or "tube out" in text or ("aux" in text and "out" in text):
        return (4, numeric)
    if "tape out" in text:
        return (5, numeric)
    if "master out" in text:
        return (6, numeric)
    if "monitor out" in text or "mon out" in text:
        return (7, numeric)
    if (
        "headphone out" in text
        or "phones out" in text
        or "speaker out" in text
        or "spk out" in text
        or "hp out" in text
    ):
        return (8, numeric)
    if any(token in text for token in digital_tokens):
        return (9, numeric)
    if any(token in text for token in computer_tokens):
        return (10, numeric)
    return (11, numeric)


def device_port_group_rank(device: str, port: str, side: str) -> tuple[int, int] | None:
    """Keep numbered output banks together when the device exposes named groups."""
    if side != "out" or "black lion" not in device.lower():
        return None

    text = port.lower()
    bank_order = (
        "analog outlet",
        "digital outlet",
        "high current outlet",
        "front unswitched outlet",
    )
    for bank_index, bank_name in enumerate(bank_order):
        if bank_name in text:
            return (bank_index, port_numeric_anchor(port))
    return None


def sort_ports(device: str, ports: list[str], port_roles: dict[str, str], side: str) -> list[str]:
    role_rank = {"in": 0, "io": 1, "out": 2, "unknown": 3}

    def port_sort_key(port: str) -> tuple[object, ...]:
        grouped_rank = device_port_group_rank(device, port, side)
        if grouped_rank is not None:
            return (0, grouped_rank[0], grouped_rank[1], natural_key(port))

        desk_rank = desk_signal_flow_rank(device, port, side)
        flow_key = desk_rank if desk_rank is not None else generic_signal_flow_rank(port, side)
        range_parts = port_range_sort_parts(port)
        return (
            1,
            flow_key[0],
            flow_key[1],
            port_signal_group(port),
            role_rank.get(port_roles.get(port, "unknown"), 3),
            range_parts[0],
            range_parts[1],
            range_parts[2],
            natural_key(port),
        )

    return sorted(
        ports,
        key=port_sort_key,
    )


def split_ports_for_device(device: str, port_roles: dict[str, str]) -> tuple[list[str], list[str]]:
    in_ports: list[str] = []
    out_ports: list[str] = []
    all_ports = sorted(port_roles.keys(), key=natural_key)
    suppressed = suppress_redundant_range_ports(all_ports)
    for port in all_ports:
        if port in suppressed:
            continue
        role = port_roles.get(port, "unknown")
        if role == "in":
            in_ports.append(port)
        elif role == "out":
            out_ports.append(port)
        elif role == "io":
            in_ports.append(port)
            out_ports.append(port)
        else:
            parsed = parse_direction(port)
            if parsed == "in":
                in_ports.append(port)
            elif parsed == "out":
                out_ports.append(port)
            else:
                in_ports.append(port)
                out_ports.append(port)
    return (
        sort_ports(device, in_ports, port_roles, side="in"),
        sort_ports(device, out_ports, port_roles, side="out"),
    )


def assign_bidirectional_port_sides(
    columns: list[list[str]],
    connections: list[Connection],
    device_port_roles: dict[str, dict[str, str]],
) -> set[str]:
    """Render each physical ``io`` socket once, on the side facing its peer.

    The matrix role ``io`` means that a socket can carry traffic in both
    directions; it does not mean that the device owns two physical sockets.
    Device boxes historically placed those ports in both their IN and OUT
    lists. Besides duplicating the label and terminal dot, that could force a
    right-to-left connection onto the long return route.

    Returns the cable IDs whose two endpoints are bidirectional. The caller
    uses that set to make their drawing direction independent of the stored
    source/destination order.
    """
    column_by_device = {
        device: column_index
        for column_index, column in enumerate(columns)
        for device in column
    }
    side_score: defaultdict[tuple[str, str], float] = defaultdict(float)
    bidirectional_connection_ids: set[str] = set()

    for connection in connections:
        source_key = (connection.source_device, connection.source_jack)
        dest_key = (connection.dest_device, connection.dest_jack)
        source_is_io = (
            device_port_roles.get(connection.source_device, {}).get(connection.source_jack)
            == "io"
        )
        dest_is_io = (
            device_port_roles.get(connection.dest_device, {}).get(connection.dest_jack)
            == "io"
        )
        if source_is_io and dest_is_io:
            bidirectional_connection_ids.add(connection.cable_id)

        source_col = column_by_device.get(connection.source_device, 0)
        dest_col = column_by_device.get(connection.dest_device, 0)
        delta = dest_col - source_col
        if delta > 0:
            # Peer is to the right: use this device's right edge and the
            # peer's left edge. Distance weights make the choice stable if a
            # (normally one-to-one) socket appears in more than one route.
            weight = float(delta)
            if source_is_io:
                side_score[source_key] += weight
            if dest_is_io:
                side_score[dest_key] -= weight
        elif delta < 0:
            weight = float(-delta)
            if source_is_io:
                side_score[source_key] -= weight
            if dest_is_io:
                side_score[dest_key] += weight
        else:
            # Vertically stacked devices share an X column. Put both ends on
            # the same outer edge so the route can run straight alongside the
            # boxes instead of crossing their full widths.
            if source_is_io:
                side_score[source_key] += 1.0
            if dest_is_io:
                side_score[dest_key] += 1.0

    for device, port_roles in device_port_roles.items():
        for port, role in list(port_roles.items()):
            if role != "io":
                continue
            # Positive/right becomes an OUT-side visual slot; negative becomes
            # an IN-side slot. Unwired and perfectly tied ports default right.
            port_roles[port] = "out" if side_score[(device, port)] >= 0.0 else "in"

    return bidirectional_connection_ids


def orient_bidirectional_connections_for_layout(
    connections: list[Connection],
    boxes: dict[str, DeviceBox],
    bidirectional_connection_ids: set[str],
) -> list[Connection]:
    """Orient physical bidirectional links left-to-right for compact routing."""
    oriented: list[Connection] = []
    for connection in connections:
        if connection.cable_id not in bidirectional_connection_ids:
            oriented.append(connection)
            continue

        source_box = boxes.get(connection.source_device)
        dest_box = boxes.get(connection.dest_device)
        if source_box is None or dest_box is None or source_box.x <= dest_box.x:
            oriented.append(connection)
            continue

        oriented.append(
            Connection(
                cable_id=connection.cable_id,
                source_device=connection.dest_device,
                source_jack=connection.dest_jack,
                dest_device=connection.source_device,
                dest_jack=connection.source_jack,
                layer=connection.layer,
                signal_type=connection.signal_type,
                status=connection.status,
                cable_type=connection.cable_type,
                connection_type=connection.connection_type,
                notes=connection.notes,
            )
        )
    return oriented


def pairing_base_for_side(text: str, side: str) -> str:
    normalized = re.sub(r"\([^)]*\)", "", text.lower())
    normalized = re.sub(r"[/_]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if side == "in":
        normalized = re.sub(r"\binputs?\b", " dir ", normalized)
        normalized = re.sub(r"\bin\b", " dir ", normalized)
        normalized = re.sub(r"\breturns?\b", " dir ", normalized)
        normalized = re.sub(r"\bret\b", " dir ", normalized)
    else:
        normalized = re.sub(r"\boutputs?\b", " dir ", normalized)
        normalized = re.sub(r"\bout\b", " dir ", normalized)
        normalized = re.sub(r"\bsends?\b", " dir ", normalized)

    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def port_pairing_key(port: str, side: str) -> str | None:
    text = abbreviate_port_label(port).strip()
    if not text:
        return None

    ranged = extract_port_range_with_base(text)
    if ranged:
        base, start, end = ranged
        paired_base = pairing_base_for_side(base, side)
        return f"{paired_base}#{start}-{end}"

    single = extract_port_single(text)
    if single:
        base, index = single
        paired_base = pairing_base_for_side(base, side)
        return f"{paired_base}#{index}"

    return pairing_base_for_side(text, side)


def next_matching_index(keys: list[str | None], start_idx: int, key: str | None) -> int | None:
    if key is None:
        return None
    for idx in range(start_idx, len(keys)):
        if keys[idx] == key:
            return idx
    return None


def align_paired_rows(in_ports: list[str], out_ports: list[str]) -> tuple[list[str], list[str]]:
    """
    Align IN/OUT rows by generalized counterpart keys.

    Keeps each side's order intact and inserts placeholders on the opposite
    side so equivalent ports (Line/MADI/ADAT/Tape/etc.) share rows.
    """
    if not in_ports or not out_ports:
        return in_ports, out_ports

    in_keys = [port_pairing_key(port, "in") for port in in_ports]
    out_keys = [port_pairing_key(port, "out") for port in out_ports]
    shared = ({key for key in in_keys if key} & {key for key in out_keys if key})
    if not shared:
        return in_ports, out_ports

    remaining_in = Counter(key for key in in_keys if key is not None)
    remaining_out = Counter(key for key in out_keys if key is not None)

    aligned_in: list[str] = []
    aligned_out: list[str] = []
    i = 0
    j = 0

    while i < len(in_ports) or j < len(out_ports):
        if i >= len(in_ports):
            aligned_in.append("")
            aligned_out.append(out_ports[j])
            out_key = out_keys[j]
            if out_key is not None:
                remaining_out[out_key] -= 1
            j += 1
            continue
        if j >= len(out_ports):
            aligned_in.append(in_ports[i])
            aligned_out.append("")
            in_key = in_keys[i]
            if in_key is not None:
                remaining_in[in_key] -= 1
            i += 1
            continue

        in_key = in_keys[i]
        out_key = out_keys[j]

        if in_key is not None and in_key == out_key:
            aligned_in.append(in_ports[i])
            aligned_out.append(out_ports[j])
            remaining_in[in_key] -= 1
            remaining_out[out_key] -= 1
            i += 1
            j += 1
            continue

        in_has_future_match = in_key is not None and remaining_out.get(in_key, 0) > 0
        out_has_future_match = out_key is not None and remaining_in.get(out_key, 0) > 0

        if in_has_future_match and not out_has_future_match:
            aligned_in.append("")
            aligned_out.append(out_ports[j])
            if out_key is not None:
                remaining_out[out_key] -= 1
            j += 1
            continue

        if out_has_future_match and not in_has_future_match:
            aligned_in.append(in_ports[i])
            aligned_out.append("")
            if in_key is not None:
                remaining_in[in_key] -= 1
            i += 1
            continue

        if in_has_future_match and out_has_future_match:
            in_match_idx = next_matching_index(out_keys, j, in_key)
            out_match_idx = next_matching_index(in_keys, i, out_key)
            in_gap = (in_match_idx - j) if in_match_idx is not None else 1_000_000
            out_gap = (out_match_idx - i) if out_match_idx is not None else 1_000_000
            if in_gap <= out_gap:
                aligned_in.append("")
                aligned_out.append(out_ports[j])
                if out_key is not None:
                    remaining_out[out_key] -= 1
                j += 1
            else:
                aligned_in.append(in_ports[i])
                aligned_out.append("")
                if in_key is not None:
                    remaining_in[in_key] -= 1
                i += 1
            continue

        aligned_in.append(in_ports[i])
        aligned_out.append(out_ports[j])
        if in_key is not None:
            remaining_in[in_key] -= 1
        if out_key is not None:
            remaining_out[out_key] -= 1
        i += 1
        j += 1

    return aligned_in, aligned_out


def deep_merge_dict(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge_dict(current, value)
        else:
            merged[key] = value
    return merged


def parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


DEVICE_VISIBILITY_TARGETS = (
    "wiring_matrix",
    "routing_matrix",
    "connection_overview",
    "visuals",
)


def is_model_device_visible(device: object, target: str = "visuals") -> bool:
    if not isinstance(device, dict):
        return False
    visibility = device.get("visibility")
    if isinstance(visibility, dict):
        target_value = visibility.get(target)
        if isinstance(target_value, bool):
            return target_value
    if parse_bool(device.get("hidden"), default=False):
        return False
    visible_value = device.get("visible")
    if visible_value is None:
        return True
    return parse_bool(visible_value, default=True)


def load_json_dict(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    text = path.read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return parsed


def load_routing_rules(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return dict(DEFAULT_ROUTING_RULES)
    user_rules = load_json_dict(path)
    return deep_merge_dict(DEFAULT_ROUTING_RULES, user_rules)


def matrix_family_definitions(model_data: dict[str, object]) -> dict[str, dict[str, str]]:
    merged = {
        family: dict(defn)
        for family, defn in DEFAULT_MATRIX_FAMILY_DEFINITIONS.items()
    }
    raw_families = model_data.get("families")
    if isinstance(raw_families, dict):
        for family_key, config in raw_families.items():
            token = normalize_matrix_family(str(family_key))
            if token not in merged:
                merged[token] = {}
            if isinstance(config, dict):
                for field in ("prefix", "layer", "signal_type", "default_cable_type"):
                    value = config.get(field)
                    if isinstance(value, str) and value.strip():
                        merged[token][field] = value.strip()
    return merged


def infer_matrix_families_from_port_text(
    port_name: str,
    port_type: str = "",
    notes: str = "",
    device_type: str = "",
) -> set[str]:
    family = classify_port_family(
        " ".join(filter(None, [port_name, port_type, notes])),
        device_type=device_type or None,
    )
    mapped: set[str] = set()
    if family == "analog":
        mapped.add("AUDIO")
    elif family == "digital":
        mapped.add("DIGI")
    elif family == "network":
        mapped.add("NETWORK")
    elif family == "computer":
        mapped.add("COMP")
    elif family == "midi":
        mapped.add("DIGI")
    elif family == "power":
        mapped.add("POWER")

    text = " ".join([port_name, port_type, notes]).lower()
    if any(token in text for token in ("hdmi", "video", "display", "thunderbolt", "usb")):
        mapped.add("COMP")
    if any(token in text for token in ("ethernet", "rj45", "cat5", "cat6", "network")):
        mapped.add("NETWORK")
    if any(token in text for token in ("madi", "spdif", "aes", "adat", "word clock", "sync", "digital")):
        mapped.add("DIGI")
    if any(
        token in text
        for token in (
            "mic",
            "line",
            "insert",
            "direct",
            "tube",
            "tape",
            "master",
            "monitor",
            "mon",
            "group out",
            "aux",
            "xlr",
            "trs",
            "speaker",
            "spk",
            "headphone",
            "phones",
        )
    ):
        mapped.add("AUDIO")
    return mapped


def infer_transport_hint(port_name: str, port_type: str = "", notes: str = "") -> str:
    text = " ".join([port_name, port_type, notes]).lower()
    if "thunderbolt" in text or "tb4" in text:
        return "TB4"
    if "hdmi" in text:
        return "HDMI"
    if "usb-c" in text:
        return "USB-C"
    if "usb" in text:
        return "USB"
    if "ethernet" in text or "rj45" in text:
        if "cat6" in text:
            return "CAT6"
        if "cat5" in text:
            return "CAT5"
        return "ETH"
    if "madi" in text and "opt" in text:
        return "MADI-OPT"
    if "madi" in text:
        return "MADI"
    if "spdif" in text and "opt" in text:
        return "SPDIF-OPT"
    if "spdif" in text or "s/pdif" in text:
        return "SPDIF"
    if "adat" in text:
        return "ADAT"
    if re.search(r"\baes\b", text):
        return "AES"
    if "word clock" in text or "clock" in text or "sync" in text:
        return "CLOCK"
    if "speaker" in text or "spk" in text:
        return "SPK"
    if "xlr" in text:
        return "XLR"
    if "trs" in text:
        return "TRS"
    return ""


def infer_port_group_metadata(port_name: str) -> dict[str, object]:
    cleaned = re.sub(r"\s+", " ", port_name).strip()
    if not cleaned:
        return {}

    lr_match = re.match(r"^(.*)\b([LR])$", cleaned, flags=re.IGNORECASE)
    if lr_match:
        base = lr_match.group(1).strip()
        side = lr_match.group(2).upper()
        return {
            "name": base,
            "member": side,
            "index": 1 if side == "L" else 2,
            "size": 2,
        }

    range_match = re.search(r"(\d+)\s*-\s*(\d+)", cleaned)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if end < start:
            start, end = end, start
        base = cleaned[: range_match.start()].strip()
        return {
            "name": base or cleaned,
            "member": f"{start}-{end}",
            "index": start,
            "size": (end - start + 1),
        }

    single_match = re.search(r"(\d+)$", cleaned)
    if single_match:
        index = int(single_match.group(1))
        base = cleaned[: single_match.start()].strip()
        return {
            "name": base or cleaned,
            "member": str(index),
            "index": index,
            "size": 1,
        }

    return {"name": cleaned}


def build_port_inventory_from_model(
    model_data: dict[str, object],
    include_power_ports: bool = False,
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, str],
    dict[str, dict[str, dict[str, object]]],
]:
    raw_devices = model_data.get("devices")
    if not isinstance(raw_devices, list):
        raise ValueError("Model JSON must contain a 'devices' array.")

    inventory: dict[str, dict[str, str]] = defaultdict(dict)
    device_types: dict[str, str] = {}
    port_meta: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)

    for device in raw_devices:
        if not isinstance(device, dict):
            continue
        if not is_model_device_visible(device):
            continue
        device_name = str(device.get("name") or "").strip()
        if not device_name:
            continue

        device_type = str(device.get("device_type") or "").strip() or classify_device_type(device_name)
        device_types[device_name] = device_type
        raw_ports = device.get("ports")
        if not isinstance(raw_ports, list):
            continue

        for order, port in enumerate(raw_ports):
            if not isinstance(port, dict):
                continue
            port_name = str(port.get("name") or "").strip()
            if not port_name:
                continue
            port_hidden = parse_bool(port.get("hidden"), default=False)
            port_visible = parse_bool(port.get("visible"), default=True)
            if port_hidden or not port_visible:
                continue

            direction = parse_direction(str(port.get("direction") or ""))
            if direction == "unknown":
                direction = parse_direction(port_name)
            if direction == "unknown":
                direction = "io"

            raw_families = port.get("families")
            if isinstance(raw_families, list):
                families = {
                    normalize_matrix_family(str(item))
                    for item in raw_families
                    if str(item).strip()
                }
            elif isinstance(raw_families, str) and raw_families.strip():
                families = {
                    normalize_matrix_family(token)
                    for token in re.split(r"[,/|]+", raw_families)
                    if token.strip()
                }
            else:
                families = set()

            if not families:
                families = infer_matrix_families_from_port_text(
                    port_name=port_name,
                    port_type=str(port.get("port_type") or ""),
                    notes=str(port.get("notes") or ""),
                    device_type=device_type,
                )
            families = {family for family in families if family}
            if not include_power_ports and "POWER" in families:
                continue
            if not families:
                families = {"COMP"}

            transport = str(port.get("transport") or "").strip()
            if not transport:
                transport = infer_transport_hint(
                    port_name=port_name,
                    port_type=str(port.get("port_type") or ""),
                    notes=str(port.get("notes") or ""),
                )

            inventory[device_name][port_name] = direction
            port_meta[device_name][port_name] = {
                "families": sorted(families, key=natural_key),
                "transport": transport,
                "order": int(port.get("order") or order),
                "group": port.get("group") if isinstance(port.get("group"), dict) else infer_port_group_metadata(port_name),
            }

    return dict(inventory), device_types, dict(port_meta)


def infer_connection_type_from_port_meta(
    family: str,
    source_meta: dict[str, object],
    dest_meta: dict[str, object],
) -> str:
    src_transport = str(source_meta.get("transport") or "").strip().upper()
    dst_transport = str(dest_meta.get("transport") or "").strip().upper()
    if src_transport and dst_transport and src_transport == dst_transport:
        return normalize_connection_type(src_transport)
    if family == "COMP":
        for tag in (src_transport, dst_transport):
            if any(token in tag for token in ("HDMI", "USB", "TB", "THUNDERBOLT")):
                return normalize_connection_type(tag)
    if family == "NETWORK":
        for tag in (src_transport, dst_transport):
            if any(token in tag for token in ("CAT", "ETH", "RJ45")):
                return normalize_connection_type(tag)
    if family == "DIGI":
        for tag in (src_transport, dst_transport):
            if any(token in tag for token in ("MADI", "SPDIF", "ADAT", "AES", "CLOCK")):
                return normalize_connection_type(tag)
    return ""


def signal_type_from_family_and_transport(family: str, transport: str) -> str:
    token = transport.lower()
    if family == "AUDIO":
        if "speaker" in token or "spk" in token:
            return "Speaker Feed"
        return "Analog Audio"
    if family == "DIGI":
        return "Digital Audio"
    if family == "NETWORK":
        return "Network Data"
    if family == "COMP":
        if "hdmi" in token:
            return "HDMI Video"
        if "thunderbolt" in token or "tb" in token:
            return "Thunderbolt Data"
        if "usb" in token:
            return "USB Data"
        return "Computer Data"
    return "Uncategorized"


def read_connections_from_model_matrix(
    model_data: dict[str, object],
    matrix_path: Path,
    selected_layers: set[str] | None,
    status_filter: set[str] | None,
    port_meta: dict[str, dict[str, dict[str, object]]],
) -> list[Connection]:
    payload = load_json_dict(matrix_path)
    raw_connections = payload.get("connections")
    if not isinstance(raw_connections, list):
        raise ValueError("Connections JSON must contain a 'connections' array.")

    family_defs = matrix_family_definitions(model_data)
    family_rank = {token: idx for idx, token in enumerate(MATRIX_FAMILY_ORDER)}
    id_width_by_prefix: dict[str, int] = defaultdict(lambda: 3)

    strict_endpoint_usage: dict[tuple[str, str], str] = {}
    loaded_rows: list[tuple[int, Connection, str, str]] = []

    for index, item in enumerate(raw_connections):
        if not isinstance(item, dict):
            continue

        source_dict = item.get("source") if isinstance(item.get("source"), dict) else {}
        dest_dict = item.get("dest") if isinstance(item.get("dest"), dict) else {}

        source_device = str(item.get("source_device") or source_dict.get("device") or "").strip()
        source_port = str(item.get("source_port") or source_dict.get("port") or "").strip()
        dest_device = str(item.get("dest_device") or dest_dict.get("device") or "").strip()
        dest_port = str(item.get("dest_port") or dest_dict.get("port") or "").strip()
        if not source_device or not source_port or not dest_device or not dest_port:
            continue

        source_device, source_port = maybe_merge_monitor_pair(source_device, source_port)
        dest_device, dest_port = maybe_merge_monitor_pair(dest_device, dest_port)

        source_meta = port_meta.get(source_device, {}).get(source_port, {})
        dest_meta = port_meta.get(dest_device, {}).get(dest_port, {})
        source_families = {
            normalize_matrix_family(str(token))
            for token in source_meta.get("families", [])
            if str(token).strip()
        }
        dest_families = {
            normalize_matrix_family(str(token))
            for token in dest_meta.get("families", [])
            if str(token).strip()
        }

        explicit_family = normalize_matrix_family(str(item.get("family") or ""))
        if explicit_family in family_defs:
            family = explicit_family
        else:
            shared = [fam for fam in MATRIX_FAMILY_ORDER if fam in source_families and fam in dest_families]
            if shared:
                family = shared[0]
            else:
                ranked = [fam for fam in MATRIX_FAMILY_ORDER if fam in source_families or fam in dest_families]
                family = ranked[0] if ranked else "COMP"

        family_def = family_defs.get(family, DEFAULT_MATRIX_FAMILY_DEFINITIONS["COMP"])
        prefix = str(family_def.get("prefix") or family).strip().upper() or family
        layer = family_def.get("layer", "Uncategorized")
        if selected_layers and layer not in selected_layers:
            continue

        status = str(item.get("status") or "Connected").strip() or "Connected"
        if status_filter and status.lower() not in status_filter:
            continue

        allow_override = parse_bool(
            item.get("override_1to1", item.get("allow_override", item.get("allow_multi"))),
            default=False,
        )
        if not allow_override:
            endpoint_keys = [(source_device, source_port), (dest_device, dest_port)]
            for endpoint in endpoint_keys:
                existing = strict_endpoint_usage.get(endpoint)
                if existing:
                    raise ValueError(
                        f"1:1 rule violation at row {index + 1}: {endpoint[0]} [{endpoint[1]}] "
                        f"is already used by {existing}. Set override_1to1=true to allow this."
                    )

        transport_hint = infer_connection_type_from_port_meta(family, source_meta, dest_meta)
        raw_connection_type = (
            str(item.get("connection_type") or item.get("type") or item.get("tag") or "").strip()
            or transport_hint
        )
        connection_type = normalize_connection_type(raw_connection_type)

        cable_id = str(item.get("cable_id") or "").strip()
        parsed = parse_cable_series(cable_id)
        if parsed and parsed[0].upper() == prefix:
            id_width_by_prefix[prefix] = max(id_width_by_prefix[prefix], max(3, parsed[2]))

        transport_for_signal = connection_type or infer_connection_type_from_port_meta(
            family,
            source_meta,
            dest_meta,
        )
        signal_type = str(item.get("signal_type") or "").strip() or signal_type_from_family_and_transport(
            family, transport_for_signal
        )
        cable_type = str(item.get("cable_type") or "").strip() or str(
            family_def.get("default_cable_type", signal_type)
        )

        connection_row = Connection(
            cable_id=cable_id,
            source_device=source_device,
            source_jack=source_port,
            dest_device=dest_device,
            dest_jack=dest_port,
            layer=layer,
            signal_type=signal_type,
            status=status,
            cable_type=cable_type,
            connection_type=connection_type,
            notes=str(item.get("notes") or "").strip(),
        )
        loaded_rows.append((index, connection_row, family, prefix))

        if not allow_override:
            strict_endpoint_usage[(source_device, source_port)] = cable_id
            strict_endpoint_usage[(dest_device, dest_port)] = cable_id

    def endpoint_sort_key(device: str, port: str, side: str) -> tuple[object, ...]:
        desk_rank = desk_signal_flow_rank(device, port, side)
        flow_key = desk_rank if desk_rank is not None else generic_signal_flow_rank(port, side)
        range_parts = port_range_sort_parts(port)
        return (
            flow_key[0],
            flow_key[1],
            port_signal_group(port),
            range_parts[0],
            range_parts[1],
            range_parts[2],
            natural_key(port),
        )

    loaded_rows.sort(
        key=lambda row: (
            family_rank.get(row[2], len(family_rank) + 1),
            row[2],
            natural_key(row[1].source_device),
            endpoint_sort_key(row[1].source_device, row[1].source_jack, "out"),
            natural_key(row[1].dest_device),
            endpoint_sort_key(row[1].dest_device, row[1].dest_jack, "in"),
            row[0],
        )
    )

    next_by_prefix: dict[str, int] = defaultdict(int)
    normalized: list[Connection] = []
    for _, connection_row, _, prefix in loaded_rows:
        next_by_prefix[prefix] += 1
        width = max(3, id_width_by_prefix.get(prefix, 3))
        new_cable_id = f"{prefix}-{next_by_prefix[prefix]:0{width}d}"
        normalized.append(
            Connection(
                cable_id=new_cable_id,
                source_device=connection_row.source_device,
                source_jack=connection_row.source_jack,
                dest_device=connection_row.dest_device,
                dest_jack=connection_row.dest_jack,
                layer=connection_row.layer,
                signal_type=connection_row.signal_type,
                status=connection_row.status,
                cable_type=connection_row.cable_type,
                connection_type=connection_row.connection_type,
                notes=connection_row.notes,
            )
        )

    return normalized


def assign_columns(
    devices: list[str],
    source_count: Counter[str],
    dest_count: Counter[str],
    connections: list[Connection],
    device_types: dict[str, str],
    layer: str = "",
) -> tuple[list[list[str]], dict[str, int]]:
    def preferred_column_for_type(device_type: str, num_cols: int) -> int | None:
        if num_cols <= 1:
            return 0
        # Default flow lanes used when a device has no visible directional edges.
        ratios: dict[str, float] = {
            "Microphone / DI": 0.00,
            "Preamps / Channel Strip": 0.22,
            "Outboard / FX": 0.35,
            "Console / Mixer": 0.40,
            "Tape Machine": 0.45,
            "Interface / Converter": 0.55,
            "Computer / Control": 0.20,
            "Network": 0.70,
            "Amplifier / Monitor Control": 0.82,
            "Speaker / Monitor": 1.00,
        }
        ratio = ratios.get(device_type)
        if ratio is None:
            return None
        return int(round(ratio * (num_cols - 1)))

    stage_map = compute_signal_stages(devices, connections)
    max_stage = max(stage_map.values(), default=0)
    if max_stage == 0:
        num_columns = 2
    else:
        num_columns = min(5, max(3, max_stage + 1))

    columns: list[list[str]] = [[] for _ in range(num_columns)]
    for device in sorted(devices, key=natural_key):
        if max_stage > 0:
            mapped = round((stage_map[device] / max_stage) * (num_columns - 1))
        else:
            mapped = 0 if source_count[device] >= dest_count[device] else num_columns - 1

        device_type = device_types.get(device, "Other")
        visible_degree = source_count[device] + dest_count[device]

        # If a device is present only through hidden patchbay relationships, place by type lane.
        if visible_degree == 0:
            preferred = preferred_column_for_type(device_type, num_columns)
            if preferred is not None:
                mapped = preferred

        # Device-type lanes are only a fallback when the visible graph has no
        # directed topology. Applying them to connected devices after stage
        # assignment can collapse consecutive stages into one column (for
        # example converter -> monitor amp). That turns an ordinary forward
        # link into a same-column right-to-left return which must wrap around
        # the destination box.
        use_type_lane_hint = max_stage == 0 or visible_degree == 0
        if layer.strip().lower() != "power" and use_type_lane_hint:
            if device_type == "Microphone / DI":
                mapped = 0
            elif "streamdeck" in device.lower():
                mapped = num_columns - 1
            elif device_type == "Preamps / Channel Strip" and num_columns >= 4:
                mapped = min(mapped, 1)
            elif device_type == "Interface / Converter" and num_columns >= 4:
                mapped = max(mapped, 2)
            elif device_type == "Speaker / Monitor":
                mapped = num_columns - 1
            elif device_type == "Amplifier / Monitor Control" and num_columns >= 2:
                mapped = min(max(mapped, num_columns - 2), num_columns - 2)

        mapped = max(0, min(mapped, num_columns - 1))
        columns[mapped].append(device)

    non_empty = [sorted(column, key=natural_key) for column in columns if column]
    if len(non_empty) == 1 and len(devices) > 1:
        ordered = sorted(devices, key=natural_key)
        midpoint = (len(ordered) + 1) // 2
        non_empty = [ordered[:midpoint], ordered[midpoint:]]

    return non_empty, stage_map


def apply_layer_column_overrides(
    layer: str,
    columns: list[list[str]],
    device_types: dict[str, str] | None = None,
) -> list[list[str]]:
    def move_device(cols: list[list[str]], device: str, target_col: int) -> None:
        present = any(device in col for col in cols)
        if not present:
            return
        for col in cols:
            if device in col:
                col.remove(device)
        while len(cols) <= target_col:
            cols.append([])
        cols[target_col].append(device)

    updated = [list(col) for col in columns]
    layer_l = layer.lower()

    if layer_l == "computer/data":
        move_device(updated, "Mac mini", 0)
        move_device(updated, "Thunderbolt Dock", 1)
        # HDMI is a direct Mac -> display link, so the TV belongs in the same
        # next-stage column as the dock rather than behind it. Keeping the TV
        # first aligns its HDMI row with the Mac and avoids a needless outer
        # route around the dock.
        move_device(updated, "TV Screen", 1)
        for endpoint in [
            "Streamdeck #1",
            "Streamdeck #2",
            "RME UFX III",
        ]:
            move_device(updated, endpoint, 2)

    if layer_l == "digital audio":
        # Keep the digital signal chain in real stage order. Putting Audient
        # and RME in one column forced the ADAT cable around the full diagram.
        move_device(updated, "Audient ASP 880", 0)
        move_device(updated, "RME UFX III", 1)
        move_device(updated, "SSL AX MADI", 2)
        move_device(updated, "TC Electronic Clarity M Stereo", 2)

    if layer_l == "network":
        # Keep network links visually top-to-bottom: dock uplink first, then surfaces.
        move_device(updated, "Thunderbolt Dock", 0)
        move_device(updated, "Avid S1 #1", 0)
        move_device(updated, "Avid S1 #2", 0)
        move_device(updated, "Netgear Unmanaged Switch", 1)

    if layer_l in {"audio analog", "all audio"}:
        # Enforce analog flow left->right:
        # mics -> outboard/preamp/fx -> AD conversion + tape -> amps -> speakers/headphones.
        all_devices = sorted({device for col in updated for device in col}, key=natural_key)
        for device in all_devices:
            configured_type = str((device_types or {}).get(device) or "").strip()
            device_type = (
                configured_type
                if configured_type in DEVICE_TYPE_ORDER
                else classify_device_type(device)
            )
            name_l = device.lower()
            if device_type == "Microphone / DI":
                move_device(updated, device, 0)
            elif device_type in {"Preamps / Channel Strip", "Outboard / FX", "Console / Mixer"}:
                move_device(updated, device, 1)
            elif device_type in {"Interface / Converter", "Tape Machine"}:
                move_device(updated, device, 2)
            elif device_type == "Amplifier / Monitor Control":
                move_device(updated, device, 3)
            elif device_type == "Speaker / Monitor" or ("headphone" in name_l and "amp" not in name_l):
                move_device(updated, device, 4)

    if layer_l == "all connections":
        # Coherent end-to-end overview flow:
        # sources/control -> dock -> network/peripherals -> interface -> converters -> amps -> speakers
        for source in [
            "Talkback Mic",
            "Audient ASP 880",
            "Focusrite Platinum Voice Master",
            "MAO Preamp (confirm model)",
            "Allen & Heath GS3000",
            "Tascam MS-16",
        ]:
            move_device(updated, source, 0)

        for core in [
            "Thunderbolt Dock",
            "Mac mini",
            "Avid S1 #1",
            "Avid S1 #2",
        ]:
            move_device(updated, core, 1)

        for endpoint in [
            "Netgear Unmanaged Switch",
            "TV Screen",
            "Streamdeck #1",
            "Streamdeck #2",
        ]:
            move_device(updated, endpoint, 2)

        for interface in [
            "RME UFX III",
        ]:
            move_device(updated, interface, 3)

        for converter in [
            "SSL AX MADI",
            "TC Electronic Clarity M Stereo",
        ]:
            move_device(updated, converter, 4)

        for amp in [
            "IMG STAGELINE PPA-100/SW",
            "Behringer A800 #1",
            "Behringer A800 #2",
        ]:
            move_device(updated, amp, 5)

        for speaker in [
            "Tannoy System 10",
            "ATC SCM 11",
            "Auratone 5C",
        ]:
            move_device(updated, speaker, 6)

    cleaned: list[list[str]] = []
    analog_priority = {
        # Col 0: microphones
        "Talkback Mic": 0,
        # Col 1: outboard / preamps / desk
        "Audient ASP 880": 10,
        "Focusrite Platinum Voice Master": 11,
        "MAO Preamp (confirm model)": 12,
        "Allen & Heath GS3000": 13,
        # Col 2: AD conversion + tape
        "RME UFX III": 20,
        "SSL AX MADI": 21,
        "Tascam MS-16": 22,
        # Col 3: amps (power amps first, then headphone amp)
        "Behringer A800 #1": 30,
        "Behringer A800 #2": 31,
        "IMG STAGELINE PPA-100/SW": 32,
        # Col 4: speakers/monitors
        "Tannoy System 10": 40,
        "ATC SCM 11": 41,
        "TC Electronic Clarity M Stereo": 42,
        "Auratone 5C": 43,
    }
    all_connections_priority = {
        "Talkback Mic": 0,
        "Audient ASP 880": 1,
        "Focusrite Platinum Voice Master": 2,
        "MAO Preamp (confirm model)": 3,
        "Allen & Heath GS3000": 4,
        "Tascam MS-16": 5,
        "Thunderbolt Dock": 6,
        "Mac mini": 7,
        "Avid S1 #1": 8,
        "Avid S1 #2": 9,
        "TV Screen": 10,
        "Streamdeck #1": 11,
        "Streamdeck #2": 12,
        "Netgear Unmanaged Switch": 13,
        "RME UFX III": 14,
        "SSL AX MADI": 15,
        "TC Electronic Clarity M Stereo": 16,
        "Behringer A800 #1": 17,
        "Behringer A800 #2": 18,
        "IMG STAGELINE PPA-100/SW": 19,
        "Tannoy System 10": 20,
        "ATC SCM 11": 21,
        "Auratone 5C": 22,
    }
    computer_data_priority = {
        "Mac mini": 0,
        "TV Screen": 1,
        "Thunderbolt Dock": 2,
        # Match the Dock's USB port order in the destination column. Keeping
        # RME (USB 1) above the Streamdecks (USB 2/3) prevents the three related
        # routes from crossing one another just outside the Dock.
        "RME UFX III": 3,
        "Streamdeck #1": 4,
        "Streamdeck #2": 5,
    }
    for col in updated:
        if col:
            if layer_l in {"audio analog", "all audio"}:
                cleaned.append(
                    sorted(
                        col,
                        key=lambda name: (analog_priority.get(name, 999), natural_key(name)),
                    )
                )
            elif layer_l == "all connections":
                cleaned.append(
                    sorted(
                        col,
                        key=lambda name: (
                            all_connections_priority.get(name, 999),
                            natural_key(name),
                        ),
                    )
                )
            elif layer_l == "computer/data":
                cleaned.append(
                    sorted(
                        col,
                        key=lambda name: (
                            computer_data_priority.get(name, 999),
                            natural_key(name),
                        ),
                    )
                )
            else:
                cleaned.append(sorted(col, key=natural_key))
    return cleaned


def measure_device(device: str, in_ports: list[str], out_ports: list[str]) -> tuple[float, float]:
    in_labels = [abbreviate_port_label(port) for port in in_ports] or [""]
    out_labels = [abbreviate_port_label(port) for port in out_ports] or [""]
    left_len = max(len(label) for label in in_labels)
    right_len = max(len(label) for label in out_labels)
    title_len = len(device)

    content_width = 96.0 + (left_len * 6.2) + (right_len * 6.2)
    title_width = 84.0 + (title_len * 6.5)
    width = min(BOX_MAX_WIDTH, max(BOX_MIN_WIDTH, content_width, title_width))
    row_count = max(len(in_ports), len(out_ports), 1)
    height = HEADER_HEIGHT + (row_count * ROW_HEIGHT) + 6.0
    return width, height


def build_boxes(
    column_devices: list[str],
    device_port_roles: dict[str, dict[str, str]],
    device_port_connected: dict[str, dict[str, bool]],
    device_types: dict[str, str],
    device_stage: dict[str, int],
    start_x: float,
    start_y: float,
    preserve_column_order: bool = False,
) -> tuple[dict[str, DeviceBox], float, list[GroupBlock]]:
    if not column_devices:
        return {}, BOX_MIN_WIDTH, []

    widths = []
    for device in column_devices:
        in_ports, out_ports = split_ports_for_device(device, device_port_roles[device])
        in_ports, out_ports = align_paired_rows(in_ports, out_ports)
        width, _ = measure_device(device, in_ports, out_ports)
        widths.append(width)
    column_width = max(widths)

    grouped_devices: dict[str, list[str]] = defaultdict(list)
    for device in column_devices:
        grouped_devices[device_types[device]].append(device)
    device_order = {device: index for index, device in enumerate(column_devices)}

    if preserve_column_order:
        # In strict flow layouts (Audio Analog), keep device-type group order
        # aligned to the explicit column device ordering.
        ordered_types = sorted(
            grouped_devices.keys(),
            key=lambda dtype: (
                min(device_order.get(name, 9999) for name in grouped_devices[dtype]),
                device_type_sort_key(dtype),
            ),
        )
    else:
        ordered_types = sorted(grouped_devices.keys(), key=device_type_sort_key)

    boxes: dict[str, DeviceBox] = {}
    groups: list[GroupBlock] = []
    y = start_y

    for group_index, device_type in enumerate(ordered_types):
        if group_index > 0:
            y += TYPE_GAP

        group_top = y - 8
        if preserve_column_order:
            group_sort_key = lambda name: (device_order.get(name, 9999), device_stage.get(name, 0), natural_key(name))
        else:
            group_sort_key = lambda name: (device_stage.get(name, 0), device_order.get(name, 9999), natural_key(name))
        for device in sorted(grouped_devices[device_type], key=group_sort_key):
            in_ports, out_ports = split_ports_for_device(device, device_port_roles[device])
            in_ports, out_ports = align_paired_rows(in_ports, out_ports)
            _, height = measure_device(device, in_ports, out_ports)

            in_port_y = {}
            for index, port in enumerate(in_ports):
                if not port:
                    continue
                in_port_y[port] = y + HEADER_HEIGHT + index * ROW_HEIGHT + (ROW_HEIGHT / 2.0)
            out_port_y = {}
            for index, port in enumerate(out_ports):
                if not port:
                    continue
                out_port_y[port] = y + HEADER_HEIGHT + index * ROW_HEIGHT + (ROW_HEIGHT / 2.0)
            boxes[device] = DeviceBox(
                name=device,
                device_type=device_type,
                x=start_x,
                y=y,
                width=column_width,
                height=height,
                in_ports=in_ports,
                out_ports=out_ports,
                in_port_y=in_port_y,
                out_port_y=out_port_y,
                port_roles=device_port_roles[device],
                port_connected=device_port_connected[device],
            )
            y += height + BOX_GAP

        group_bottom = y - BOX_GAP + 8
        groups.append(
            GroupBlock(
                device_type=device_type,
                x=start_x - GROUP_FRAME_HORIZONTAL_PADDING,
                y=group_top,
                width=column_width + (GROUP_FRAME_HORIZONTAL_PADDING * 2.0),
                height=max(30.0, group_bottom - group_top),
            )
        )

    return boxes, column_width, groups


def place_device_below_intervening_columns(
    boxes: dict[str, DeviceBox],
    groups: list[GroupBlock],
    device_column: dict[str, int],
    source_device: str,
    destination_device: str,
) -> None:
    """Lower a destination so its incoming route can use a clear bottom rail."""
    source_col = device_column.get(source_device)
    destination_col = device_column.get(destination_device)
    destination_box = boxes.get(destination_device)
    if source_col is None or destination_col is None or destination_box is None:
        return

    low_col, high_col = sorted((source_col, destination_col))
    blockers = [
        box
        for name, box in boxes.items()
        if low_col < device_column.get(name, low_col) < high_col
    ]
    if not blockers:
        return

    minimum_y = max(box.y + box.height for box in blockers) + BOX_GAP
    if destination_box.y >= minimum_y:
        return

    move_device_box_down(
        boxes,
        groups,
        destination_device,
        minimum_y,
    )


def move_device_box_down(
    boxes: dict[str, DeviceBox],
    groups: list[GroupBlock],
    device_name: str,
    minimum_y: float,
) -> None:
    """Move one laid-out device down and keep its anchors/group in sync."""
    destination_box = boxes.get(device_name)
    if destination_box is None or destination_box.y >= minimum_y:
        return

    old_bottom = destination_box.y + destination_box.height
    delta_y = minimum_y - destination_box.y
    destination_box.y += delta_y
    destination_box.in_port_y = {
        port: y + delta_y for port, y in destination_box.in_port_y.items()
    }
    destination_box.out_port_y = {
        port: y + delta_y for port, y in destination_box.out_port_y.items()
    }

    for group in groups:
        group_right = group.x + group.width
        group_bottom = group.y + group.height
        if group.device_type != destination_box.device_type:
            continue
        if not (group.x <= destination_box.x <= group_right):
            continue
        if old_bottom > group_bottom + 0.5:
            continue
        group.height = max(
            group.height,
            destination_box.y + destination_box.height + 8.0 - group.y,
        )
        break


def merge_port_maps_for_layer(
    connections: list[Connection],
    port_inventory: dict[str, dict[str, str]],
    extra_devices: set[str] | None = None,
    hidden_patch_connections: list[Connection] | None = None,
    device_type_overrides: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, dict[str, str]], dict[str, dict[str, bool]], Counter[str], Counter[str], dict[str, str]]:
    source_count: Counter[str] = Counter()
    dest_count: Counter[str] = Counter()
    port_flags: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    connected_flags: dict[str, dict[str, bool]] = defaultdict(lambda: defaultdict(bool))
    known_ports: dict[str, set[str]] = defaultdict(set)

    for connection in connections:
        source_count[connection.source_device] += 1
        dest_count[connection.dest_device] += 1

        known_ports[connection.source_device].add(connection.source_jack)
        known_ports[connection.dest_device].add(connection.dest_jack)

        port_flags[connection.source_device][connection.source_jack].add("out")
        port_flags[connection.dest_device][connection.dest_jack].add("in")

        connected_flags[connection.source_device][connection.source_jack] = True
        connected_flags[connection.dest_device][connection.dest_jack] = True

    # When patchbays are hidden, still mark non-patch endpoints as wired so
    # ports like Tascam/500-series stay visibly connected.
    visible_link_devices = set(source_count.keys()) | set(dest_count.keys())
    for connection in hidden_patch_connections or []:
        src_is_patch = is_patchbay_device(connection.source_device)
        dst_is_patch = is_patchbay_device(connection.dest_device)
        if src_is_patch == dst_is_patch:
            continue

        if (not src_is_patch) and (connection.source_device not in visible_link_devices):
            known_ports[connection.source_device].add(connection.source_jack)
            port_flags[connection.source_device][connection.source_jack].add("out")
            connected_flags[connection.source_device][connection.source_jack] = True
        if (not dst_is_patch) and (connection.dest_device not in visible_link_devices):
            known_ports[connection.dest_device].add(connection.dest_jack)
            port_flags[connection.dest_device][connection.dest_jack].add("in")
            connected_flags[connection.dest_device][connection.dest_jack] = True

    layer_devices = {connection.source_device for connection in connections} | {
        connection.dest_device for connection in connections
    }
    for connection in hidden_patch_connections or []:
        if (
            (not is_patchbay_device(connection.source_device))
            and (connection.source_device not in visible_link_devices)
        ):
            layer_devices.add(connection.source_device)
        if (
            (not is_patchbay_device(connection.dest_device))
            and (connection.dest_device not in visible_link_devices)
        ):
            layer_devices.add(connection.dest_device)
    if extra_devices:
        layer_devices |= set(extra_devices)

    for device in sorted(layer_devices, key=natural_key):
        for port, role in port_inventory.get(device, {}).items():
            known_ports[device].add(port)
            port_flags[device][port].update(role_to_flags(role))

    devices = sorted(known_ports.keys(), key=natural_key)
    device_port_roles: dict[str, dict[str, str]] = defaultdict(dict)
    device_port_connected: dict[str, dict[str, bool]] = defaultdict(dict)

    for device in devices:
        for port in sorted(known_ports[device], key=natural_key):
            merged_flags = set(port_flags[device][port])
            inventory_role = port_inventory.get(device, {}).get(port, "unknown")
            merged_flags.update(role_to_flags(inventory_role))

            role = flags_to_role(merged_flags)
            if role == "unknown":
                role = inventory_role

            device_port_roles[device][port] = role
            device_port_connected[device][port] = connected_flags[device][port]

        normalized_roles, normalized_connected = normalize_range_availability(
            device_port_roles[device],
            device_port_connected[device],
        )
        device_port_roles[device] = normalized_roles
        device_port_connected[device] = normalized_connected

    device_types = {device: classify_device_type(device) for device in devices}
    for device, device_type in (device_type_overrides or {}).items():
        if device in device_types and device_type:
            device_types[device] = device_type
    return devices, device_port_roles, device_port_connected, source_count, dest_count, device_types


def draw_group_blocks(
    svg_lines: list[str],
    group_blocks: list[GroupBlock],
    *,
    draw_boxes: bool = True,
    draw_labels: bool = True,
) -> None:
    for block in group_blocks:
        label = html.escape(block.device_type)
        fill = "#f8fafc"
        stroke = "#cbd5e1"
        text = "#475569"
        dash = "4 4"
        if block.device_type == "Analog Front End":
            fill, stroke, text, dash = "#fffbeb", "#f59e0b", "#92400e", "6 4"
        elif block.device_type == "Computer / Control":
            fill, stroke, text, dash = "#ecfeff", "#0891b2", "#0e7490", "6 4"
        elif block.device_type == "Digital Core":
            fill, stroke, text, dash = "#eff6ff", "#3b82f6", "#1d4ed8", "6 4"
        elif block.device_type == "Monitoring":
            fill, stroke, text, dash = "#fff7ed", "#ea580c", "#9a3412", "6 4"
        if draw_boxes:
            svg_lines.append(
                f'  <rect x="{block.x:.1f}" y="{block.y:.1f}" width="{block.width:.1f}" height="{block.height:.1f}" rx="8" ry="8" fill="{fill}" fill-opacity="0.55" stroke="{stroke}" stroke-width="1" stroke-dasharray="{dash}"/>'
            )
        if draw_labels:
            label_x = block.x + 8.0
            label_y = max(12.0, block.y - 4.0)
            label_rect_y = block.y - GROUP_LABEL_TOP_OFFSET
            label_w = max(42.0, len(block.device_type) * 5.8)
            svg_lines.append(
                f'  <rect x="{label_x - 2.0:.1f}" y="{label_rect_y:.1f}" width="{label_w:.1f}" height="12.5" rx="2" ry="2" fill="#ffffff" fill-opacity="0.9" stroke="{stroke}" stroke-width="0.6"/>'
            )
            svg_lines.append(
                f'  <text x="{label_x:.1f}" y="{label_y:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="{text}">{label}</text>'
            )


def compute_route_top_base(
    boxes: dict[str, DeviceBox],
    group_blocks: list[GroupBlock],
    margin_y: float,
) -> float:
    """Return a top routing lane that clears boxes and group captions."""
    device_lane = min(
        (box.y for box in boxes.values()),
        default=(margin_y + 40.0),
    ) - 14.0
    if not group_blocks:
        return max(margin_y + 8.0, device_lane)

    # A group caption sits above its frame: its background starts 13 px above
    # the frame's top edge. Reserve that full band plus breathing room so an
    # outer wire cannot run through the caption or hug the dashed border.
    caption_lane = min(
        block.y - GROUP_LABEL_TOP_OFFSET - GROUP_ROUTE_CLEARANCE
        for block in group_blocks
    )
    return max(margin_y + 8.0, min(device_lane, caption_lane))


def stacked_top_route_lane(
    route_top_base: float,
    route_position: int,
    lane_spacing: float,
    margin_y: float,
) -> float:
    """Stack outer top routes upward, away from group frames and captions."""
    return max(
        margin_y + 8.0,
        route_top_base - (max(0, route_position) * lane_spacing),
    )


def build_overview_functional_groups(boxes: dict[str, DeviceBox]) -> list[GroupBlock]:
    groups: list[GroupBlock] = []
    grouped_devices: set[str] = set()
    for label, device_names in OVERVIEW_FUNCTIONAL_GROUPS:
        present = [boxes[name] for name in device_names if name in boxes]
        if not present:
            continue
        grouped_devices.update(name for name in device_names if name in boxes)
        left = min(box.x for box in present) - 12.0
        top = min(box.y for box in present) - 10.0
        right = max(box.x + box.width for box in present) + 12.0
        bottom = max(box.y + box.height for box in present) + 10.0
        groups.append(
            GroupBlock(
                device_type=label,
                x=left,
                y=top,
                width=max(40.0, right - left),
                height=max(30.0, bottom - top),
            )
        )

    leftovers = [name for name in boxes.keys() if name not in grouped_devices]
    if leftovers:
        present = [boxes[name] for name in leftovers]
        left = min(box.x for box in present) - 12.0
        top = min(box.y for box in present) - 10.0
        right = max(box.x + box.width for box in present) + 12.0
        bottom = max(box.y + box.height for box in present) + 10.0
        groups.append(
            GroupBlock(
                device_type="Other Devices",
                x=left,
                y=top,
                width=max(40.0, right - left),
                height=max(30.0, bottom - top),
            )
        )
    return groups


def port_anchor(box: DeviceBox, port: str, prefer: str) -> tuple[float, float]:
    if prefer == "in":
        if port in box.in_port_y:
            return box.x, box.in_port_y[port]
        if port in box.out_port_y:
            return box.x + box.width, box.out_port_y[port]
    else:
        if port in box.out_port_y:
            return box.x + box.width, box.out_port_y[port]
        if port in box.in_port_y:
            return box.x, box.in_port_y[port]

    # Composite visual anchors (e.g. "Line Out 1+Line Out 2" for stereo bundles).
    if "+" in port:
        parts = [item.strip() for item in port.split("+") if item.strip()]
        if parts:
            ys: list[float] = []
            for part in parts:
                if prefer == "in":
                    if part in box.in_port_y:
                        ys.append(box.in_port_y[part])
                    elif part in box.out_port_y:
                        ys.append(box.out_port_y[part])
                else:
                    if part in box.out_port_y:
                        ys.append(box.out_port_y[part])
                    elif part in box.in_port_y:
                        ys.append(box.in_port_y[part])
            if ys:
                y = sum(ys) / float(len(ys))
                x = box.x if prefer == "in" else (box.x + box.width)
                return x, y

    return box.x + (box.width / 2.0), box.y + HEADER_HEIGHT + (ROW_HEIGHT / 2.0)


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[float, float]] = []
    cur_start, cur_end = sorted_intervals[0]
    for start, end in sorted_intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
            continue
        merged.append((cur_start, cur_end))
        cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def compute_clear_x_intervals(
    boxes: dict[str, DeviceBox],
    width: float,
    min_x: float = 12.0,
    max_x: float | None = None,
    padding: float = 2.0,
) -> list[tuple[float, float]]:
    right_limit = (width - 12.0) if max_x is None else max_x
    blocked: list[tuple[float, float]] = []
    for box in boxes.values():
        start = max(min_x, box.x - padding)
        end = min(right_limit, box.x + box.width + padding)
        if end > start:
            blocked.append((start, end))

    if not blocked:
        return [(min_x, right_limit)]

    clear: list[tuple[float, float]] = []
    cursor = min_x
    for start, end in merge_intervals(blocked):
        if start > cursor:
            clear.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < right_limit:
        clear.append((cursor, right_limit))
    return clear


def containing_group_bottom(
    box: DeviceBox,
    group_blocks: list[GroupBlock],
) -> float:
    """Return the bottom of the smallest displayed group containing a box."""
    candidates = [
        group
        for group in group_blocks
        if group.device_type == box.device_type
        and group.x <= box.x + 0.5
        and (box.x + box.width) <= (group.x + group.width + 0.5)
        and group.y <= box.y + 0.5
        and (box.y + box.height) <= (group.y + group.height + 0.5)
    ]
    if not candidates:
        return box.y + box.height
    containing = min(candidates, key=lambda group: group.width * group.height)
    return containing.y + containing.height


def pick_clear_x(
    desired_x: float,
    low_x: float,
    high_x: float,
    clear_intervals: list[tuple[float, float]],
) -> float | None:
    if high_x < low_x:
        low_x, high_x = high_x, low_x

    candidates: list[tuple[float, float]] = []
    for start, end in clear_intervals:
        c_start = max(start, low_x)
        c_end = min(end, high_x)
        if c_end - c_start > 1.0:
            candidates.append((c_start, c_end))
    if not candidates:
        return None

    for start, end in candidates:
        if start <= desired_x <= end:
            return desired_x

    best_x = None
    best_dist = float("inf")
    for start, end in candidates:
        probe = min(max(desired_x, start), end)
        dist = abs(probe - desired_x)
        if dist < best_dist:
            best_dist = dist
            best_x = probe
    return best_x


def render_svg(
    layer: str,
    connections: list[Connection],
    title: str,
    port_inventory: dict[str, dict[str, str]],
    generated_on: dt.date,
    extra_devices: set[str] | None = None,
    hidden_patch_connections: list[Connection] | None = None,
    overview_mode: bool = False,
    device_type_overrides: dict[str, str] | None = None,
    drawing_rules: dict[str, object] | None = None,
    route_debug_records: list[dict[str, object]] | None = None,
    overview_power_groups: dict[str, str] | None = None,
) -> str:
    routing_rules = drawing_rules.get("routing", {}) if isinstance(drawing_rules, dict) else {}
    power_group_by_connection = resolve_power_groups(connections) if layer.strip().lower() == "power" else {}
    power_style_by_connection = power_visual_styles(connections) if layer.strip().lower() == "power" else {}
    margin_x = max(30.0, min(180.0, float(routing_rules.get("left_route_gutter_px", 72.0))))
    if overview_mode:
        margin_x = max(margin_x, 160.0)
    margin_y = 34
    planned_family_counts: Counter[str] = Counter()
    for connection in connections:
        family, _ = resolve_connection_family_and_color(connection)
        legend_family = power_style_by_connection.get(
            connection,
            (power_group_by_connection.get(connection, family), ""),
        )[0]
        planned_family_counts[legend_family] += 1
    planned_legend_items = sorted(planned_family_counts.items(), key=lambda item: natural_key(item[0]))
    planned_legend_height = 28.0 + (len(planned_legend_items) * 14.0) + 34.0
    top_reserved = planned_legend_height + 10.0

    (
        devices,
        device_port_roles,
        device_port_connected,
        source_count,
        dest_count,
        device_types,
    ) = merge_port_maps_for_layer(
        connections,
        port_inventory,
        extra_devices=extra_devices,
        hidden_patch_connections=hidden_patch_connections,
        device_type_overrides=device_type_overrides,
    )

    labels_rules = drawing_rules.get("labels", {}) if isinstance(drawing_rules, dict) else {}
    source_label_side = str(labels_rules.get("source_side", "above")).strip().lower()
    dest_label_side = str(labels_rules.get("destination_side", "below")).strip().lower()
    label_font_size = float(labels_rules.get("font_size", CONNECTION_LABEL_FONT_SIZE))
    label_wire_gap = float(labels_rules.get("wire_gap_px", 4.0))
    label_wire_gap = max(1.0, min(16.0, label_wire_gap))
    label_offset_step = float(labels_rules.get("offset_step_px", 6.0))
    label_offset_step = max(2.0, min(16.0, label_offset_step))
    fifo_forward_turns = parse_bool(routing_rules.get("fifo_forward_turns"), default=True)
    backward_out_to_in_wrap = str(routing_rules.get("backward_out_to_in_wrap", "below")).strip().lower()
    video_early_turn = parse_bool(routing_rules.get("video_early_turn"), default=True)
    video_vertical_rows_threshold = float(routing_rules.get("video_vertical_rows_threshold", 6.0))
    forward_turn_edge_margin = float(routing_rules.get("forward_turn_edge_margin", 0.12))
    forward_turn_edge_margin = max(0.0, min(0.35, forward_turn_edge_margin))
    wire_clearance = max(4.0, min(40.0, float(routing_rules.get("wire_clearance_px", 12.0))))
    power_wire_clearance = max(
        wire_clearance,
        min(56.0, float(routing_rules.get("power_wire_clearance_px", 18.0))),
    )
    power_lane_spacing = max(
        power_wire_clearance,
        min(64.0, float(routing_rules.get("power_lane_spacing_px", 18.0))),
    )
    power_column_gap = max(
        300.0,
        min(700.0, float(routing_rules.get("power_column_gap_px", 420.0))),
    )

    filter_ports_for_layer(
        layer=layer,
        device_port_roles=device_port_roles,
        device_port_connected=device_port_connected,
        device_types=device_types,
        overview_mode=overview_mode,
    )
    devices = [device for device in devices if device_port_roles.get(device)]

    if overview_mode:
        # Keep the "all connections" overview concise: show only wired ports.
        for device in list(device_port_roles.keys()):
            connected_ports = {
                port: role
                for port, role in device_port_roles[device].items()
                if device_port_connected[device].get(port, False)
            }
            if connected_ports:
                device_port_roles[device] = connected_ports
                device_port_connected[device] = {port: True for port in connected_ports}

    columns, stage_map = assign_columns(
        devices,
        source_count,
        dest_count,
        connections,
        device_types,
        layer,
    )
    columns = apply_layer_column_overrides(layer, columns, device_types)
    bidirectional_connection_ids = assign_bidirectional_port_sides(
        columns,
        connections,
        device_port_roles,
    )

    if layer.lower() == "power":
        column_gap = power_column_gap
    elif overview_mode:
        if len(columns) >= 6:
            column_gap = 300
        elif len(columns) == 5:
            column_gap = 300
        else:
            column_gap = 340
    elif len(columns) >= 4:
        column_gap = 300
    elif len(columns) == 3:
        column_gap = 340
    else:
        column_gap = 380

    column_gap_by_boundary: dict[int, float] = {}
    if not overview_mode:
        preliminary_column = {
            device: column_index
            for column_index, column_devices in enumerate(columns)
            for device in column_devices
        }
        forward_fanout_counts: Counter[tuple[str, int, int]] = Counter()
        forward_fanout_clearance: dict[tuple[str, int, int], float] = {}
        for connection in connections:
            source_col = preliminary_column.get(connection.source_device, 0)
            dest_col = preliminary_column.get(connection.dest_device, 0)
            if dest_col != source_col + 1:
                continue
            key = (connection.source_device, source_col, dest_col)
            forward_fanout_counts[key] += 1
            family, _ = resolve_connection_family_and_color(connection)
            target = power_lane_spacing if family == "Power" else wire_clearance
            forward_fanout_clearance[key] = max(
                target,
                forward_fanout_clearance.get(key, 0.0),
            )
        required_fanout_gaps = [
            (
                key[1],
                (2.0 * 108.0)
                + ((count - 1) * forward_fanout_clearance[key])
                + 24.0,
            )
            for key, count in forward_fanout_counts.items()
            if count > 1
        ]
        for boundary, required_gap in required_fanout_gaps:
            column_gap_by_boundary[boundary] = max(
                column_gap,
                required_gap,
                column_gap_by_boundary.get(boundary, 0.0),
            )

    boxes: dict[str, DeviceBox] = {}
    all_groups: list[GroupBlock] = []
    device_column: dict[str, int] = {}
    cursor_x = margin_x
    rightmost_x = margin_x
    for column_index, column_devices in enumerate(columns):
        column_boxes, column_width, column_groups = build_boxes(
            column_devices,
            device_port_roles,
            device_port_connected,
            device_types,
            stage_map,
            start_x=cursor_x,
            start_y=margin_y + 50 + top_reserved,
            preserve_column_order=(
                layer.lower()
                in {"audio analog", "all audio", "all connections", "computer/data"}
            ),
        )
        boxes.update(column_boxes)
        all_groups.extend(column_groups)
        for device in column_devices:
            device_column[device] = column_index
        rightmost_x = max(rightmost_x, cursor_x + column_width)
        cursor_x += column_width + column_gap_by_boundary.get(column_index, column_gap)

    if layer.lower() == "all audio":
        place_device_below_intervening_columns(
            boxes,
            all_groups,
            device_column,
            "RME UFX III",
            "TC Electronic Clarity M Stereo",
        )
    elif layer.lower() == "all connections":
        power_amp_bottom = max(
            (
                boxes[name].y + boxes[name].height
                for name in ("Behringer A800 #1", "Behringer A800 #2")
                if name in boxes
            ),
            default=0.0,
        )
        if power_amp_bottom:
            move_device_box_down(
                boxes,
                all_groups,
                "SSL AX MADI",
                power_amp_bottom + BOX_GAP,
            )
            ssl_box = boxes.get("SSL AX MADI")
            tc_minimum_y = (
                ssl_box.y + ssl_box.height + BOX_GAP
                if ssl_box is not None
                else power_amp_bottom + BOX_GAP
            )
            move_device_box_down(
                boxes,
                all_groups,
                "TC Electronic Clarity M Stereo",
                tc_minimum_y,
            )

    display_groups = all_groups
    if overview_mode and layer.lower() == "all connections":
        display_groups = build_overview_functional_groups(boxes)

    max_box_bottom = max((box.y + box.height for box in boxes.values()), default=margin_y)
    max_group_bottom = max((group.y + group.height for group in display_groups), default=margin_y)
    max_bottom = max(max_box_bottom, max_group_bottom)

    width = max(1200.0, rightmost_x + margin_x)
    # Extra bottom room allows backward/outer routes to stay in distinct lanes.
    # The overview carries every family, so it needs a larger shared gutter.
    route_gutter = 180.0 if overview_mode else 70.0
    if layer.lower() == "power":
        route_gutter = max(route_gutter, power_lane_spacing * (len(connections) + 2))
    height = max_bottom + margin_y + route_gutter

    total_ports = sum(len(box.port_roles) for box in boxes.values())
    wired_ports = sum(
        1
        for box in boxes.values()
        for port in box.port_roles.keys()
        if box.port_connected.get(port, False)
    )
    render_connections = collapse_stereo_headphone_connections_for_render(connections)
    render_connections, collapsed_bidirectional_ids = collapse_reciprocal_single_links_for_render(
        render_connections
    )
    bidirectional_connection_ids.update(collapsed_bidirectional_ids)
    if overview_mode and layer.lower() in {"all connections", "all audio"}:
        render_connections = collapse_multichannel_connections_for_overview(
            render_connections,
            min_channels=4,
        )
    render_connections = orient_bidirectional_connections_for_layout(
        render_connections,
        boxes,
        bidirectional_connection_ids,
    )

    generated_iso = generated_on.isoformat()

    svg_lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(width)}" height="{int(height)}" viewBox="0 0 {int(width)} {int(height)}">',
        '  <defs>',
        '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" orient="auto-start-reverse">',
        '      <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/>',
        "    </marker>",
        "  </defs>",
        '  <rect x="0" y="0" width="100%" height="100%" fill="#f8fafc"/>',
        f'  <text x="{margin_x}" y="{margin_y}" font-family="Helvetica, Arial, sans-serif" font-size="20" fill="#0f172a">{html.escape(title)} | {html.escape(layer)} | {generated_iso}</text>',
        f'  <text x="{margin_x}" y="{margin_y + 20}" font-family="Helvetica, Arial, sans-serif" font-size="12" fill="#475569">Devices: {len(devices)} | Cables: {len(render_connections)} | Ports shown: {wired_ports}/{total_ports} wired</text>',
    ]

    # Draw group frames first (behind everything else).
    draw_group_blocks(svg_lines, display_groups, draw_boxes=True, draw_labels=False)

    family_counts: Counter[str] = Counter()
    family_colors: dict[str, str] = {}
    route_bottom_base = max_bottom + 26.0
    route_top_base = compute_route_top_base(boxes, display_groups, margin_y)
    clear_x_padding = GROUP_FRAME_HORIZONTAL_PADDING + GROUP_ROUTE_CLEARANCE
    clear_x_intervals = compute_clear_x_intervals(
        boxes,
        width,
        min_x=12.0,
        max_x=width - 12.0,
        padding=clear_x_padding,
    )
    group_bottom_by_device = {
        name: containing_group_bottom(box, display_groups)
        for name, box in boxes.items()
    }

    preordered_connections = sorted(render_connections, key=lambda item: natural_key(item.cable_id))

    def connection_layout_sort_key(item: Connection) -> tuple[object, ...]:
        src_box = boxes[item.source_device]
        dst_box = boxes[item.dest_device]
        sx, sy = port_anchor(src_box, item.source_jack, prefer="out")
        dx, dy = port_anchor(dst_box, item.dest_jack, prefer="in")
        source_col = device_column.get(item.source_device, 0)
        dest_col = device_column.get(item.dest_device, 0)
        backward = dx < sx - 1.0
        span = abs(dest_col - source_col)
        avg_y = (sy + dy) / 2.0
        # Draw long/backward routes first so shorter forward links stay visible.
        # Forward FIFO bundles must be processed in source-row order; sorting
        # them by average Y lets a low destination consume a late turn lane
        # before earlier source rows and collapses the remaining fan-out.
        return (
            0 if backward else 1,
            -span if backward else span,
            min(source_col, dest_col),
            max(source_col, dest_col),
            avg_y if backward else sy,
            sy if backward else dy,
            dy if backward else avg_y,
            natural_key(item.cable_id),
        )

    ordered_connections = sorted(preordered_connections, key=connection_layout_sort_key)

    anchor_cache: dict[int, tuple[float, float, float, float]] = {}
    for idx, connection in enumerate(ordered_connections):
        src_box = boxes[connection.source_device]
        dst_box = boxes[connection.dest_device]
        sx, sy = port_anchor(src_box, connection.source_jack, prefer="out")
        dx, dy = port_anchor(dst_box, connection.dest_jack, prefer="in")
        anchor_cache[idx] = (sx, sy, dx, dy)

    label_overrides, suppress_labels = build_bundle_label_plan(
        ordered_connections,
        min_run=2 if overview_mode else 3,
    )
    raw_label_by_index: dict[int, str] = {}
    label_width_by_index: dict[int, float] = {}
    for idx, connection in enumerate(ordered_connections):
        raw_label = "" if idx in suppress_labels else label_overrides.get(idx, render_cable_label(connection))
        raw_label_by_index[idx] = raw_label
        label_width_by_index[idx] = max(26.0, len(raw_label) * CONNECTION_LABEL_CHAR_PX)
    route_groups: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    route_key_by_index: dict[int, tuple[int, int, int]] = {}
    pair_route_groups: dict[tuple[int, int, int], list[tuple[str, str]]] = defaultdict(list)
    pair_route_seen: dict[tuple[int, int, int], set[tuple[str, str]]] = defaultdict(set)
    for idx, conn in enumerate(ordered_connections):
        source_col = device_column.get(conn.source_device, 0)
        dest_col = device_column.get(conn.dest_device, 0)
        sx, _, dx, _ = anchor_cache[idx]
        direction = 1 if dest_col > source_col or (dest_col == source_col and dx >= sx - 1.0) else -1
        route_key = (source_col, dest_col, direction)
        route_groups[route_key].append(idx)
        route_key_by_index[idx] = route_key
        pair_key = (conn.source_device, conn.dest_device)
        if pair_key not in pair_route_seen[route_key]:
            pair_route_seen[route_key].add(pair_key)
            pair_route_groups[route_key].append(pair_key)

    route_slot: dict[int, tuple[int, int]] = {}
    for _, idx_list in route_groups.items():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                (anchor_cache[idx][1] + anchor_cache[idx][3]) / 2.0,
                anchor_cache[idx][1],
                anchor_cache[idx][3],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            route_slot[idx] = (slot, total)

    pair_route_slot: dict[tuple[tuple[int, int, int], tuple[str, str]], tuple[int, int]] = {}
    for route_key, pair_list in pair_route_groups.items():
        total = len(pair_list)
        for slot, pair_key in enumerate(pair_list):
            pair_route_slot[(route_key, pair_key)] = (slot, total)
    route_used_mx: dict[tuple[object, ...], list[float]] = defaultdict(list)
    fifo_last_mx: dict[tuple[str, str, int, int], float] = {}

    source_groups: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    dest_groups: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for idx, conn in enumerate(ordered_connections):
        source_col = device_column.get(conn.source_device, 0)
        dest_col = device_column.get(conn.dest_device, 0)
        source_groups[(conn.source_device, source_col, dest_col)].append(idx)
        dest_groups[(conn.dest_device, source_col, dest_col)].append(idx)

    # A descending route that spans a later sibling's source row needs the
    # upper cable to turn farther right. Otherwise that later horizontal cable
    # must cross the earlier vertical drop (for example AUDIO-035/036).
    fifo_reverse_groups: set[tuple[str, int, int]] = set()
    for group_key, idx_list in source_groups.items():
        source_rows = sorted(anchor_cache[idx][1] for idx in idx_list)
        for idx in idx_list:
            _sx, sy, _dx, dy = anchor_cache[idx]
            if dy <= sy + 0.5:
                continue
            if any((sy + 0.5) < sibling_y < (dy - 0.5) for sibling_y in source_rows):
                fifo_reverse_groups.add(group_key)
                break

    source_slot: dict[int, tuple[int, int]] = {}
    dest_slot: dict[int, tuple[int, int]] = {}
    source_device_slot: dict[int, tuple[int, int]] = {}
    dest_device_slot: dict[int, tuple[int, int]] = {}
    for _, idx_list in source_groups.items():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                anchor_cache[idx][1],
                anchor_cache[idx][3],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            source_slot[idx] = (slot, total)
    for _, idx_list in dest_groups.items():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (anchor_cache[idx][1], natural_key(ordered_connections[idx].cable_id)),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            dest_slot[idx] = (slot, total)

    # If a route's destination row equals a sibling route's source row
    # (same source fanout group), strict FIFO turn ordering can force
    # temporary shared horizontal rails. Mark those routes so they use
    # a non-overlap turn strategy instead of FIFO turn locking.
    source_row_conflict: dict[int, bool] = {}
    for _, idx_list in source_groups.items():
        source_rows = [anchor_cache[idx][1] for idx in idx_list]
        for idx in idx_list:
            _, _, _, dy = anchor_cache[idx]
            conflict = any(abs(sy_other - dy) <= 0.5 for sy_other in source_rows)
            # Ignore self-match in case a source and destination row are identical
            # on the same cable while no sibling occupies that row.
            if conflict:
                sy_self = anchor_cache[idx][1]
                sibling_match = any(
                    other_idx != idx and abs(anchor_cache[other_idx][1] - dy) <= 0.5
                    for other_idx in idx_list
                )
                conflict = sibling_match
            source_row_conflict[idx] = conflict

    fifo_group_lead_len: dict[tuple[str, int, int], float] = {}
    if fifo_forward_turns:
        for idx, conn in enumerate(ordered_connections):
            sx, _, dx, _ = anchor_cache[idx]
            if dx < sx - 1.0:
                continue
            source_col = device_column.get(conn.source_device, 0)
            dest_col = device_column.get(conn.dest_device, 0)
            src_slot_total = source_slot.get(idx, (0, 1))[1]
            if src_slot_total <= 1:
                continue
            route_total = route_slot.get(idx, (0, 1))[1]
            raw_label = raw_label_by_index.get(idx, "")
            label_width = label_width_by_index.get(idx, 26.0)
            lead_len = max(108.0, label_width + 34.0) if raw_label else 68.0
            forward_span = max(0.0, dx - sx)
            desired_mid_corridor = 26.0 + min(46.0, max(route_total - 1, 0) * 7.0)
            max_lead_by_span = (forward_span - desired_mid_corridor) / 2.0
            if max_lead_by_span > 0.0:
                if max_lead_by_span >= 52.0:
                    lead_len = min(lead_len, max_lead_by_span)
                else:
                    lead_len = max(30.0, max_lead_by_span)
            fifo_key = (conn.source_device, source_col, dest_col)
            fifo_group_lead_len[fifo_key] = max(fifo_group_lead_len.get(fifo_key, 0.0), lead_len)

    source_device_groups: dict[str, list[int]] = defaultdict(list)
    for idx, conn in enumerate(ordered_connections):
        source_device_groups[conn.source_device].append(idx)
    for idx_list in source_device_groups.values():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                anchor_cache[idx][1],
                anchor_cache[idx][3],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            source_device_slot[idx] = (slot, total)

    dest_device_groups: dict[str, list[int]] = defaultdict(list)
    for idx, conn in enumerate(ordered_connections):
        dest_device_groups[conn.dest_device].append(idx)
    for idx_list in dest_device_groups.values():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                anchor_cache[idx][3],
                anchor_cache[idx][1],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            dest_device_slot[idx] = (slot, total)

    pair_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, conn in enumerate(ordered_connections):
        pair_groups[(conn.source_device, conn.dest_device)].append(idx)

    pair_slot: dict[int, tuple[int, int]] = {}
    for _, idx_list in pair_groups.items():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                anchor_cache[idx][1],
                anchor_cache[idx][3],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        count = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            pair_slot[idx] = (slot, count)

    backward_groups: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, conn in enumerate(ordered_connections):
        sx, _, dx, _ = anchor_cache[idx]
        if dx < sx - 1.0:
            source_col = device_column.get(conn.source_device, 0)
            dest_col = device_column.get(conn.dest_device, 0)
            backward_groups[(source_col, dest_col)].append(idx)

    backward_slot: dict[int, tuple[int, int]] = {}
    for _, idx_list in backward_groups.items():
        sorted_idx_list = sorted(
            idx_list,
            key=lambda idx: (
                anchor_cache[idx][1],
                anchor_cache[idx][3],
                natural_key(ordered_connections[idx].cable_id),
            ),
        )
        total = len(sorted_idx_list)
        for slot, idx in enumerate(sorted_idx_list):
            backward_slot[idx] = (slot, total)

    placed_label_rects: list[tuple[float, float, float, float]] = []
    pending_connection_wire_lines: list[tuple[bool, list[str]]] = []
    pending_connection_label_lines: list[str] = []

    def centered_slot_offset(
        slot_idx: int,
        slot_total: int,
        step: float = 10.0,
        max_abs: float = 34.0,
    ) -> float:
        if slot_total <= 1:
            return 0.0
        center = (slot_total - 1) / 2.0
        offset = (slot_idx - center) * step
        return max(-max_abs, min(max_abs, offset))

    def find_label_position(
        desired_x: float,
        desired_y: float,
        label_width_px: float,
        y_offsets: list[float] | None = None,
        blocked_lines: list[float] | None = None,
        min_allowed_y: float | None = None,
        max_allowed_y: float | None = None,
    ) -> tuple[float, float, float]:
        def clamp_y_bounds(value: float) -> float:
            clamped = max(margin_y + 20.0, min(value, height - 16.0))
            if min_allowed_y is not None:
                clamped = max(clamped, min_allowed_y)
            if max_allowed_y is not None:
                clamped = min(clamped, max_allowed_y)
            return clamped

        half_label = (label_width_px / 2.0) + 4.0
        min_x = margin_x + half_label + 8.0
        max_x = width - margin_x - half_label - 8.0
        x = max(min_x, min(desired_x, max_x))
        candidates: list[float] = (
            y_offsets
            if y_offsets is not None
            else [0.0, -10.0, 10.0, -20.0, 20.0, -30.0, 30.0, -40.0, 40.0]
        )
        rect_h = 14.0
        for offset in candidates:
            y = clamp_y_bounds(desired_y + offset)
            if min_allowed_y is not None and y < min_allowed_y:
                continue
            if max_allowed_y is not None and y > max_allowed_y:
                continue
            rect_x = x - (label_width_px / 2.0) - 3.0
            rect_y = y - 10.0
            rect_w = label_width_px + 6.0
            if blocked_lines:
                # Treat edge-touching as acceptable so endpoint labels can stay
                # bound to their own row in dense port stacks.
                line_clearance = 0.5
                if any(
                    (rect_y + line_clearance) < line_y < (rect_y + rect_h - line_clearance)
                    for line_y in blocked_lines
                ):
                    continue
            collision = False
            for px, py, pw, ph in placed_label_rects:
                if not (
                    (rect_x + rect_w + 2.0) < px
                    or (px + pw + 2.0) < rect_x
                    or (rect_y + rect_h + 2.0) < py
                    or (py + ph + 2.0) < rect_y
                ):
                    collision = True
                    break
            if not collision:
                placed_label_rects.append((rect_x, rect_y, rect_w, rect_h))
                return x, y, rect_x

        y = clamp_y_bounds(desired_y)
        if blocked_lines:
            for line_y in blocked_lines:
                if (y - 11.0) <= line_y <= (y + 5.0):
                    if line_y >= y:
                        y = clamp_y_bounds(line_y - 12.0)
                    else:
                        y = clamp_y_bounds(line_y + 16.0)
        rect_x = x - (label_width_px / 2.0) - 3.0
        placed_label_rects.append((rect_x, y - 10.0, label_width_px + 6.0, rect_h))
        return x, y, rect_x

    def horizontal_crosses_other_boxes(
        x1: float,
        x2: float,
        y: float,
        source_device: str,
        dest_device: str,
    ) -> bool:
        low = min(x1, x2)
        high = max(x1, x2)
        if high - low < 1.0:
            return False
        for device_name, box in boxes.items():
            if device_name == source_device or device_name == dest_device:
                continue
            if y < (box.y + 1.0) or y > (box.y + box.height - 1.0):
                continue
            if high <= (box.x + 1.0) or low >= (box.x + box.width - 1.0):
                continue
            return True
        return False

    def vertical_crosses_other_boxes(
        x: float,
        y1: float,
        y2: float,
        source_device: str,
        dest_device: str,
    ) -> bool:
        low = min(y1, y2)
        high = max(y1, y2)
        if high - low < 1.0:
            return False
        for device_name, box in boxes.items():
            if device_name == source_device or device_name == dest_device:
                continue
            if x < (box.x + 1.0) or x > (box.x + box.width - 1.0):
                continue
            if high <= (box.y + 1.0) or low >= (box.y + box.height - 1.0):
                continue
            return True
        return False

    def route_to_path(points: list[tuple[float, float]]) -> str:
        if not points:
            return ""
        compact: list[tuple[float, float]] = [points[0]]
        for px, py in points[1:]:
            lx, ly = compact[-1]
            if abs(px - lx) < 0.05 and abs(py - ly) < 0.05:
                continue
            compact.append((px, py))
        if len(compact) == 1:
            x, y = compact[0]
            return f"M {x:.1f} {y:.1f}"
        commands: list[str] = [f"M {compact[0][0]:.1f} {compact[0][1]:.1f}"]
        for idx in range(1, len(compact)):
            px, py = compact[idx - 1]
            x, y = compact[idx]
            if abs(y - py) < 0.05:
                commands.append(f"H {x:.1f}")
            elif abs(x - px) < 0.05:
                commands.append(f"V {y:.1f}")
            else:
                commands.append(f"L {x:.1f} {y:.1f}")
        return " ".join(commands)

    def simplify_orthogonal_route(
        points: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Remove duplicate, collinear, and out-and-back route vertices."""
        simplified: list[tuple[float, float]] = []
        for point in points:
            if simplified and abs(point[0] - simplified[-1][0]) < 0.05 and abs(point[1] - simplified[-1][1]) < 0.05:
                continue
            simplified.append(point)
            while len(simplified) >= 3:
                first, middle, last = simplified[-3:]
                same_x = abs(first[0] - middle[0]) < 0.05 and abs(middle[0] - last[0]) < 0.05
                same_y = abs(first[1] - middle[1]) < 0.05 and abs(middle[1] - last[1]) < 0.05
                if not (same_x or same_y):
                    break
                simplified.pop(-2)
        return simplified

    def route_cross_count(
        points: list[tuple[float, float]],
        source_device: str,
        dest_device: str,
    ) -> int:
        count = 0
        if len(points) < 2:
            return count
        for idx in range(1, len(points)):
            x1, y1 = points[idx - 1]
            x2, y2 = points[idx]
            for device_name, box in boxes.items():
                if device_name == source_device or device_name == dest_device:
                    continue
                if abs(x1 - x2) < 0.05:
                    x = x1
                    if x < (box.x + 1.0) or x > (box.x + box.width - 1.0):
                        continue
                    low = min(y1, y2)
                    high = max(y1, y2)
                    if high <= (box.y + 1.0) or low >= (box.y + box.height - 1.0):
                        continue
                    count += 1
                elif abs(y1 - y2) < 0.05:
                    y = y1
                    if y < (box.y + 1.0) or y > (box.y + box.height - 1.0):
                        continue
                    low = min(x1, x2)
                    high = max(x1, x2)
                    if high <= (box.x + 1.0) or low >= (box.x + box.width - 1.0):
                        continue
                    count += 1
        return count

    def route_group_cross_count(
        points: list[tuple[float, float]],
        source_box: DeviceBox,
        dest_box: DeviceBox,
    ) -> int:
        """Count unrelated functional groups whose interior a route enters."""
        if len(points) < 2:
            return 0

        def group_contains_box(group: GroupBlock, box: DeviceBox) -> bool:
            center_x = box.x + (box.width / 2.0)
            center_y = box.y + (box.height / 2.0)
            return (
                group.x <= center_x <= (group.x + group.width)
                and group.y <= center_y <= (group.y + group.height)
            )

        crossed_groups = 0
        for group in display_groups:
            # Let a cable leave its source group and enter its destination
            # group. Only unrelated groups are routing obstacles.
            if group_contains_box(group, source_box) or group_contains_box(group, dest_box):
                continue

            left = group.x + 1.0
            right = group.x + group.width - 1.0
            top = group.y + 1.0
            bottom = group.y + group.height - 1.0
            crosses = False
            for point_index in range(1, len(points)):
                x1, y1 = points[point_index - 1]
                x2, y2 = points[point_index]
                if abs(x1 - x2) < 0.05:
                    low_y, high_y = sorted((y1, y2))
                    crosses = (
                        left < x1 < right
                        and min(high_y, bottom) - max(low_y, top) > 0.5
                    )
                elif abs(y1 - y2) < 0.05:
                    low_x, high_x = sorted((x1, x2))
                    crosses = (
                        top < y1 < bottom
                        and min(high_x, right) - max(low_x, left) > 0.5
                    )
                if crosses:
                    break
            if crosses:
                crossed_groups += 1
        return crossed_groups

    def route_manhattan_length(points: list[tuple[float, float]]) -> float:
        if len(points) < 2:
            return 0.0
        total = 0.0
        for idx in range(1, len(points)):
            x1, y1 = points[idx - 1]
            x2, y2 = points[idx]
            total += abs(x2 - x1) + abs(y2 - y1)
        return total

    def route_bend_count(points: list[tuple[float, float]]) -> int:
        if len(points) < 3:
            return 0
        bends = 0
        for idx in range(2, len(points)):
            x1, y1 = points[idx - 2]
            x2, y2 = points[idx - 1]
            x3, y3 = points[idx]
            dx1, dy1 = x2 - x1, y2 - y1
            dx2, dy2 = x3 - x2, y3 - y2
            if abs(dx1) < 0.05 and abs(dx2) < 0.05:
                continue
            if abs(dy1) < 0.05 and abs(dy2) < 0.05:
                continue
            bends += 1
        return bends

    def route_outside_band_distance(
        points: list[tuple[float, float]],
        y_start: float,
        y_end: float,
    ) -> float:
        if not points:
            return 0.0
        low = min(y_start, y_end)
        high = max(y_start, y_end)
        extra = 0.0
        for _, y in points:
            if y < low:
                extra = max(extra, low - y)
            elif y > high:
                extra = max(extra, y - high)
        return extra

    def route_endpoint_cross_count(
        points: list[tuple[float, float]],
        source_box: DeviceBox,
        dest_box: DeviceBox,
    ) -> int:
        # Count crossings through source/destination bodies on interior segments only.
        # First/last segments intentionally touch endpoint rows near ports.
        if len(points) < 4:
            return 0
        count = 0
        endpoint_boxes = (source_box, dest_box)
        for idx in range(1, len(points)):
            if idx == 1 or idx == (len(points) - 1):
                continue
            x1, y1 = points[idx - 1]
            x2, y2 = points[idx]
            for box in endpoint_boxes:
                if abs(x1 - x2) < 0.05:
                    x = x1
                    if x < (box.x + 1.0) or x > (box.x + box.width - 1.0):
                        continue
                    low = min(y1, y2)
                    high = max(y1, y2)
                    if high <= (box.y + 1.0) or low >= (box.y + box.height - 1.0):
                        continue
                    count += 1
                elif abs(y1 - y2) < 0.05:
                    y = y1
                    if y < (box.y + 1.0) or y > (box.y + box.height - 1.0):
                        continue
                    low = min(x1, x2)
                    high = max(x1, x2)
                    if high <= (box.x + 1.0) or low >= (box.x + box.width - 1.0):
                        continue
                    count += 1
        return count

    routed_segments: list[tuple[str, float, float, float, str]] = []

    def vertical_rail_needs_clearance(
        candidate_x: float,
        y1: float,
        y2: float,
        source_device: str,
        dest_device: str,
        minimum_clearance: float,
    ) -> bool:
        """Return whether a vertical rail must move for a box or prior rail."""
        if vertical_crosses_other_boxes(candidate_x, y1, y2, source_device, dest_device):
            return True
        vertical_low, vertical_high = sorted((y1, y2))
        return any(
            axis == "V"
            and abs(candidate_x - existing_x) < minimum_clearance
            and min(vertical_high, existing_end) - max(vertical_low, existing_start) > 2.0
            for axis, existing_x, existing_start, existing_end, _protocol in routed_segments
        )

    def route_segments(points: list[tuple[float, float]]) -> list[tuple[str, float, float, float]]:
        segments: list[tuple[str, float, float, float]] = []
        if len(points) < 2:
            return segments
        for idx in range(1, len(points)):
            x1, y1 = points[idx - 1]
            x2, y2 = points[idx]
            if abs(x1 - x2) < 0.05:
                start, end = sorted((y1, y2))
                if end - start > 0.25:
                    segments.append(("V", x1, start, end))
            elif abs(y1 - y2) < 0.05:
                start, end = sorted((x1, x2))
                if end - start > 0.25:
                    segments.append(("H", y1, start, end))
        return segments

    def horizontal_overlap_extent(y: float, x1: float, x2: float) -> float:
        low = min(x1, x2)
        high = max(x1, x2)
        if high - low < 0.5:
            return 0.0
        overlap = 0.0
        for seg_axis, seg_const, seg_start, seg_end, _seg_family in routed_segments:
            if seg_axis != "H":
                continue
            if abs(seg_const - y) > 0.05:
                continue
            inter = min(high, seg_end) - max(low, seg_start)
            if inter > 0.2:
                overlap += inter
        return overlap

    def pick_route_clear_x(
        desired_x: float,
        low_x: float,
        high_x: float,
        y1: float,
        y2: float,
        minimum_clearance: float,
    ) -> float | None:
        """Pick a box-free vertical rail that also clears routed vertical rails."""
        base = pick_clear_x(
            desired_x=desired_x,
            low_x=low_x,
            high_x=high_x,
            clear_intervals=clear_x_intervals,
        )
        if base is None:
            return None
        vertical_low, vertical_high = sorted((y1, y2))

        def clears_routes(candidate: float) -> bool:
            for axis, existing_x, existing_start, existing_end, _protocol in routed_segments:
                if axis != "V" or abs(candidate - existing_x) >= minimum_clearance:
                    continue
                overlap_len = min(vertical_high, existing_end) - max(vertical_low, existing_start)
                if overlap_len > 2.0:
                    return False
            return True

        if clears_routes(base):
            return base
        best: tuple[float, float] | None = None
        for interval_start, interval_end in clear_x_intervals:
            start = max(low_x, interval_start)
            end = min(high_x, interval_end)
            probe = math.ceil(start)
            while probe <= end:
                candidate = float(probe)
                probe += 1
                if not clears_routes(candidate):
                    continue
                score = abs(candidate - desired_x)
                if best is None or score < best[0]:
                    best = (score, candidate)
        return best[1] if best is not None else base

    def route_overlap_metrics(
        points: list[tuple[float, float]],
        route_protocol: str,
        minimum_clearance: float,
    ) -> tuple[int, float, float, int, float]:
        diff_protocol_count = 0
        diff_protocol_len = 0.0
        same_protocol_len = 0.0
        clearance_count = 0
        clearance_len = 0.0
        for seg_axis, seg_const, seg_start, seg_end in route_segments(points):
            for ex_axis, ex_const, ex_start, ex_end, ex_protocol in routed_segments:
                if seg_axis != ex_axis:
                    continue
                separation = abs(seg_const - ex_const)
                if separation >= minimum_clearance:
                    continue
                overlap_start = max(seg_start, ex_start)
                overlap_end = min(seg_end, ex_end)
                overlap_len = overlap_end - overlap_start
                if overlap_len <= 2.0:
                    continue
                if separation > 0.55 and (
                    ex_protocol != route_protocol or route_protocol == "POWER"
                ):
                    clearance_count += 1
                    clearance_len += overlap_len
                elif separation <= 0.55 and ex_protocol == route_protocol:
                    same_protocol_len += overlap_len
                elif separation <= 0.55:
                    diff_protocol_count += 1
                    diff_protocol_len += overlap_len
        return (
            diff_protocol_count,
            diff_protocol_len,
            same_protocol_len,
            clearance_count,
            clearance_len,
        )

    def route_candidate_score(
        points: list[tuple[float, float]],
        source_device: str,
        dest_device: str,
        source_box: DeviceBox,
        dest_box: DeviceBox,
        y_start: float,
        y_end: float,
        route_protocol: str,
        minimum_clearance: float,
    ) -> tuple[object, ...]:
        (
            overlap_count,
            overlap_len,
            same_protocol_overlap_len,
            clearance_count,
            clearance_len,
        ) = route_overlap_metrics(points, route_protocol, minimum_clearance)
        return (
            route_cross_count(points, source_device, dest_device),
            route_endpoint_cross_count(points, source_box, dest_box),
            route_group_cross_count(points, source_box, dest_box),
            overlap_count,
            route_outside_band_distance(points, y_start, y_end),
            route_bend_count(points),
            clearance_count,
            round(overlap_len, 1),
            round(clearance_len, 1),
            round(same_protocol_overlap_len, 1),
            route_manhattan_length(points),
        )

    score_labels = (
        "box_crossings",
        "endpoint_crossings",
        "unrelated_group_crossings",
        "different_family_overlap_count",
        "outside_band_distance",
        "bend_count",
        "parallel_clearance_violation_count",
        "different_family_overlap_len",
        "parallel_clearance_violation_len",
        "same_family_overlap_len",
        "manhattan_length",
    )

    def serialize_route_score(score: tuple[object, ...]) -> dict[str, object]:
        payload = {
            label: score[idx] if idx < len(score) else None
            for idx, label in enumerate(score_labels)
        }
        # Keep the historical family keys while exposing their refined meaning:
        # routing now distinguishes HDMI, Thunderbolt, ADAT, MADI, clock, etc.
        payload["different_protocol_overlap_count"] = payload["different_family_overlap_count"]
        payload["different_protocol_overlap_len"] = payload["different_family_overlap_len"]
        payload["same_protocol_overlap_len"] = payload["same_family_overlap_len"]
        return payload

    def bounded_slot_shift(slot_idx: int, slot_total: int, step: float, max_abs: float) -> float:
        if slot_total <= 1:
            return 0.0
        center = (slot_total - 1) / 2.0
        shift = (slot_idx - center) * step
        return max(-max_abs, min(max_abs, shift))

    def first_vertical_turn_x(points: list[tuple[float, float]]) -> float | None:
        if len(points) < 2:
            return None
        for idx in range(1, len(points)):
            x1, y1 = points[idx - 1]
            x2, y2 = points[idx]
            if abs(x1 - x2) < 0.05 and abs(y1 - y2) > 0.05:
                return x1
        return None

    for index, connection in enumerate(ordered_connections):
        src_box = boxes[connection.source_device]
        dst_box = boxes[connection.dest_device]
        sx, sy, dx, dy = anchor_cache[index]
        source_col = device_column.get(connection.source_device, 0)
        dest_col = device_column.get(connection.dest_device, 0)
        family, wire_color = resolve_connection_family_and_color(connection)
        legend_family, power_style_color = power_style_by_connection.get(
            connection,
            (power_group_by_connection.get(connection, family), ""),
        )
        if power_style_color:
            wire_color = power_style_color
        cable_namespace = connection.cable_id.partition("-")[0].strip().upper()
        if cable_namespace == "AUDIO":
            route_protocol = "ANALOG"
        elif cable_namespace == "POWER":
            route_protocol = "POWER"
        elif cable_namespace == "NETWORK":
            route_protocol = "NETWORK"
        else:
            route_protocol = normalize_connection_type(connection.connection_type or family).upper()
        minimum_clearance = power_wire_clearance if family == "Power" else wire_clearance

        raw_label = raw_label_by_index.get(index, "")
        cable_label = html.escape(raw_label)
        label_width = label_width_by_index.get(index, 26.0)

        route_pos, route_total = route_slot.get(index, (0, 1))
        route_center = (route_total - 1) / 2.0
        route_spread = (route_pos - route_center) * 8.0 if route_total > 1 else 0.0
        route_frac = ((route_pos + 1) / (route_total + 1)) if route_total > 1 else 0.5
        src_slot_idx, src_slot_total = source_slot.get(index, (0, 1))
        fifo_lead_group_key = (connection.source_device, source_col, dest_col)
        potential_fifo_turn = (
            dx >= sx - 1.0
            and src_slot_total > 1
            and abs(dest_col - source_col) <= 1
            and fifo_forward_turns
            and not source_row_conflict.get(index, False)
        )

        # Reserve a straight lead near both ports so labels can sit on flat wire segments.
        source_dir = 1.0 if sx >= (src_box.x + src_box.width - 0.5) else -1.0
        dest_dir = -1.0 if dx <= (dst_box.x + 0.5) else 1.0
        lead_len = max(108.0, label_width + 34.0) if raw_label else 68.0
        if dx >= sx - 1.0:
            # Keep some central corridor available for fan-outs so lanes do not collapse.
            forward_span = max(0.0, dx - sx)
            desired_mid_corridor = 26.0 + min(46.0, max(route_total - 1, 0) * 7.0)
            max_lead_by_span = (forward_span - desired_mid_corridor) / 2.0
            if max_lead_by_span > 0.0:
                if max_lead_by_span >= 52.0:
                    lead_len = min(lead_len, max_lead_by_span)
                else:
                    lead_len = max(30.0, max_lead_by_span)
        else:
            # Backward wraps need less lead to prevent long destination rows from
            # overlapping unrelated signals near the left-side turn.
            max_backward_lead = 82.0 if overview_mode else 90.0
            lead_len = min(lead_len, max_backward_lead)
        if potential_fifo_turn:
            group_lead_len = fifo_group_lead_len.get(fifo_lead_group_key)
            if group_lead_len is not None:
                lead_len = group_lead_len

        src_lead_x = max(12.0, min(width - 12.0, sx + source_dir * lead_len))
        dst_lead_x = max(12.0, min(width - 12.0, dx + dest_dir * lead_len))
        if src_slot_total > 1 and not potential_fifo_turn:
            src_slot_step = 10.0 + min(4.0, max(route_total - 1, 0) * 0.6)
            src_slot_max_abs = 34.0 + min(16.0, max(route_total - 1, 0) * 0.35)
            src_lead_x += source_dir * bounded_slot_shift(
                src_slot_idx,
                src_slot_total,
                src_slot_step,
                src_slot_max_abs,
            )
        src_device_slot_idx, src_device_slot_total = source_device_slot.get(index, (0, 1))
        if src_device_slot_total > 1 and not potential_fifo_turn:
            src_device_center = (src_device_slot_total - 1) / 2.0
            # Backward routes need visibly separate departure rails. Tiny
            # per-device offsets made unrelated protocols look like one line.
            src_device_step = (
                minimum_clearance
                if dx < sx - 1.0
                else (2.2 if overview_mode else 1.6)
            )
            src_device_offset = (src_device_slot_idx - src_device_center) * src_device_step
            src_device_max_abs = (
                min(60.0, max(18.0, src_device_center * minimum_clearance))
                if dx < sx - 1.0
                else 18.0
            )
            src_lead_x += source_dir * max(
                -src_device_max_abs,
                min(src_device_max_abs, src_device_offset),
            )
        dst_slot_idx, dst_slot_total = dest_slot.get(index, (0, 1))
        if dst_slot_total > 1 and not potential_fifo_turn:
            dst_slot_step = 10.0 + min(4.0, max(route_total - 1, 0) * 0.6)
            dst_slot_max_abs = 34.0 + min(16.0, max(route_total - 1, 0) * 0.35)
            dst_lead_x += dest_dir * bounded_slot_shift(
                dst_slot_idx,
                dst_slot_total,
                dst_slot_step,
                dst_slot_max_abs,
            )
        dst_device_slot_idx, dst_device_slot_total = dest_device_slot.get(index, (0, 1))
        if dst_device_slot_total > 1 and not potential_fifo_turn:
            dst_device_center = (dst_device_slot_total - 1) / 2.0
            dst_device_step = (
                minimum_clearance
                if dx < sx - 1.0
                else (2.2 if overview_mode else 1.6)
            )
            dst_device_offset = (dst_device_slot_idx - dst_device_center) * dst_device_step
            dst_device_max_abs = (
                min(60.0, max(18.0, dst_device_center * minimum_clearance))
                if dx < sx - 1.0
                else 18.0
            )
            dst_lead_x += dest_dir * max(
                -dst_device_max_abs,
                min(dst_device_max_abs, dst_device_offset),
            )
        src_lead_x = max(12.0, min(width - 12.0, src_lead_x))
        dst_lead_x = max(12.0, min(width - 12.0, dst_lead_x))

        # Preserve actual gutter space for multiple right-to-left returns. A
        # destination lead that consumes the whole left margin forces every
        # return onto x=12 and makes unrelated protocols share one rail.
        if dx < sx - 1.0 and dest_dir < 0 and dx <= (dst_box.x + 0.5):
            _back_slot_idx, back_slot_total = backward_slot.get(index, (0, 1))
            available_left_gutter = max(0.0, dx - 12.0)
            reserved_return_lanes = 20.0 + (back_slot_total * wire_clearance)
            max_destination_lead = max(
                18.0,
                available_left_gutter - reserved_return_lanes,
            )
            dst_lead_x = max(dst_lead_x, dx - max_destination_lead)

        # Keep forward routes monotonic in X so they don't "wiggle" backward
        # when lead lengths overlap in tight column spacing.
        if dx >= sx - 1.0:
            available_gap = max(8.0, (dx - sx) - 12.0)
            desired_gap = 22.0 + min(54.0, max(route_total - 1, 0) * 7.0)
            if raw_label:
                desired_gap = max(desired_gap, min(56.0, (label_width * 0.55) + 18.0))
            required_gap = min(desired_gap, available_gap)
        else:
            required_gap = 2.0

        if dx >= sx - 1.0 and src_lead_x >= (dst_lead_x - required_gap):
            mid_x = (sx + dx) / 2.0
            half_gap = required_gap / 2.0
            src_lead_x = min(src_lead_x, mid_x - half_gap)
            dst_lead_x = max(dst_lead_x, mid_x + half_gap)
            src_lead_x = max(12.0, min(width - 12.0, src_lead_x))
            dst_lead_x = max(12.0, min(width - 12.0, dst_lead_x))

        # Never allow lead offsets to flip to the opposite side of their port.
        # This prevents "bend too soon" artifacts where an OUT port immediately
        # routes backward before turning toward destination.
        span_abs = abs(dx - sx)
        base_side_min = min(24.0, max(8.0, span_abs * 0.18))
        lead_side_min = min(base_side_min, max(2.0, (span_abs / 2.0) - 2.0))
        if source_dir >= 0:
            src_lead_x = max(src_lead_x, sx + lead_side_min)
        else:
            src_lead_x = min(src_lead_x, sx - lead_side_min)
        if dest_dir <= 0:
            dst_lead_x = min(dst_lead_x, dx - lead_side_min)
        else:
            dst_lead_x = max(dst_lead_x, dx + lead_side_min)
        src_lead_x = max(12.0, min(width - 12.0, src_lead_x))
        dst_lead_x = max(12.0, min(width - 12.0, dst_lead_x))

        # Keep 90-degree turn order stable (FIFO) for forward fan-outs
        # from a single source lane.
        fifo_turn = potential_fifo_turn
        if fifo_turn:
            route_frac = (src_slot_idx + 1) / (src_slot_total + 1)

        # Large FIFO bundles need wider effective corridor use; otherwise
        # turn lanes collapse into visually dense vertical rails.
        effective_turn_edge_margin = forward_turn_edge_margin
        if fifo_turn and src_slot_total > 6 and forward_turn_edge_margin > 0.0:
            fifo_density = min(1.0, max(0.0, (src_slot_total - 6) / 18.0))
            effective_turn_edge_margin = forward_turn_edge_margin * (1.0 - (0.85 * fifo_density))
            effective_turn_edge_margin = max(0.0, min(forward_turn_edge_margin, effective_turn_edge_margin))

        # Keep first/last forward turns away from corridor extremes so big
        # channel maps do not bend immediately next to source/destination.
        if dx >= sx - 1.0:
            frac_total = src_slot_total if fifo_turn and src_slot_total > 1 else route_total
            if frac_total > 2 and effective_turn_edge_margin > 0.0:
                route_frac = (
                    effective_turn_edge_margin
                    + (route_frac * (1.0 - (2.0 * effective_turn_edge_margin)))
                )
                route_frac = max(0.0, min(1.0, route_frac))
        # FIFO turn order must be shared across all fan-outs from the same
        # source lane, not per destination device, otherwise sibling routes
        # can collapse onto the same turn X.
        fifo_group_key = (
            connection.source_device,
            source_col,
            dest_col,
        )
        turn_order_direction = -1 if fifo_group_key in fifo_reverse_groups else 1
        prev_fifo_turn_x = fifo_last_mx.get(fifo_group_key) if fifo_turn else None

        lane_shift = route_spread
        slot, slot_count = pair_slot.get(index, (0, 1))
        if slot_count > 1:
            pair_center = (slot_count - 1) / 2.0
            pair_step = 6.0 if fifo_turn else 12.0
            lane_shift += (slot - pair_center) * pair_step
        route_key = route_key_by_index.get(index, (0, 0, 1))
        pair_key = (connection.source_device, connection.dest_device)
        pair_route_pos, pair_route_total = pair_route_slot.get((route_key, pair_key), (0, 1))
        if pair_route_total > 1:
            pair_route_center = (pair_route_total - 1) / 2.0
            pair_route_step = 8.0 if fifo_turn else 18.0
            lane_shift += (pair_route_pos - pair_route_center) * pair_route_step
        src_left = src_box.x
        src_right = src_box.x + src_box.width
        dst_left = dst_box.x
        dst_right = dst_box.x + dst_box.width
        overlap = min(src_right, dst_right) - max(src_left, dst_left)
        backward = dx < sx - 1.0
        src_role = device_port_roles.get(connection.source_device, {}).get(
            connection.source_jack,
            "unknown",
        )
        dst_role = device_port_roles.get(connection.dest_device, {}).get(
            connection.dest_jack,
            "unknown",
        )
        if src_role == "unknown":
            src_role = parse_direction(connection.source_jack)
        if dst_role == "unknown":
            dst_role = parse_direction(connection.dest_jack)
        backward_out_to_in = backward and src_role in {"out", "io"} and dst_role in {"in", "io"}
        best_points: list[tuple[float, float]] = []
        best_score: tuple[object, ...] = ()

        if backward:
            # "Return" paths go down and around the left side to keep flow readable.
            back_slot_idx, back_slot_total = backward_slot.get(index, (0, 1))
            back_center = (back_slot_total - 1) / 2.0
            back_shift = back_slot_idx - back_center
            span_columns = abs(source_col - dest_col)
            # Keep the outer-left turn safely left of both lead segments so the
            # return path doesn't create a vertical "stub" near the destination port.
            lead_left_bound = min(src_lead_x, dst_lead_x) - 20.0
            backward_lane_step = power_lane_spacing if family == "Power" else max(11.0, wire_clearance)
            desired_outer_left = min(src_left, dst_left) - 26.0 - (back_shift * backward_lane_step) - (span_columns * 4.0)
            desired_outer_left = min(
                desired_outer_left,
                lead_left_bound - (back_slot_idx * backward_lane_step),
            )
            outer_left = max(12.0, desired_outer_left)
            snap_outer_max = max(12.0, lead_left_bound)
            if snap_outer_max > 12.0:
                snapped_outer = pick_route_clear_x(
                    desired_x=outer_left,
                    low_x=12.0,
                    high_x=snap_outer_max,
                    y1=0.0,
                    y2=height,
                    minimum_clearance=minimum_clearance,
                )
                if snapped_outer is not None:
                    outer_left = snapped_outer
            def build_backward_points(turn_y: float) -> list[tuple[float, float]]:
                src_turn_x = src_lead_x
                dst_turn_x = dst_lead_x
                if abs(turn_y - sy) > 0.05 and vertical_rail_needs_clearance(
                    src_turn_x,
                    sy,
                    turn_y,
                    connection.source_device,
                    connection.dest_device,
                    minimum_clearance,
                ):
                    snap = pick_route_clear_x(
                        desired_x=src_turn_x,
                        low_x=12.0,
                        high_x=width - 12.0,
                        y1=sy,
                        y2=turn_y,
                        minimum_clearance=minimum_clearance,
                    )
                    if snap is not None:
                        src_turn_x = snap
                if abs(turn_y - dy) > 0.05 and vertical_rail_needs_clearance(
                    dst_turn_x,
                    dy,
                    turn_y,
                    connection.source_device,
                    connection.dest_device,
                    minimum_clearance,
                ):
                    snap = pick_route_clear_x(
                        desired_x=dst_turn_x,
                        low_x=12.0,
                        high_x=width - 12.0,
                        y1=dy,
                        y2=turn_y,
                        minimum_clearance=minimum_clearance,
                    )
                    if snap is not None:
                        dst_turn_x = snap

                points: list[tuple[float, float]] = [
                    (sx, sy),
                    (src_lead_x, sy),
                ]
                if abs(src_turn_x - src_lead_x) > 0.05:
                    points.append((src_turn_x, sy))
                if abs(turn_y - sy) > 0.05:
                    points.append((src_turn_x, turn_y))
                points.append((outer_left, turn_y))
                if abs(turn_y - dy) > 0.05:
                    points.append((outer_left, dy))
                points.append((dst_turn_x, dy))
                if abs(dst_lead_x - dst_turn_x) > 0.05:
                    points.append((dst_lead_x, dy))
                points.append((dx, dy))
                return points

            src_bottom = src_box.y + src_box.height
            dst_bottom = dst_box.y + dst_box.height
            src_top = src_box.y
            dst_top = dst_box.y

            backward_vertical_step = power_lane_spacing if family == "Power" else 16.0
            legacy_bottom = route_bottom_base + (span_columns * 18.0) + (back_slot_idx * backward_vertical_step)
            legacy_bottom = min(legacy_bottom, height - 14.0)
            group_safe_bottom = (
                max(
                    group_bottom_by_device.get(connection.source_device, src_bottom),
                    group_bottom_by_device.get(connection.dest_device, dst_bottom),
                )
                + GROUP_ROUTE_CLEARANCE
            )
            compact_bottom = max(
                group_safe_bottom,
                min(
                    height - 14.0,
                    max(src_bottom, dst_bottom)
                    + 18.0
                    + (span_columns * 10.0)
                    + (back_slot_idx * (power_lane_spacing if family == "Power" else 10.0)),
                ),
            )
            # Keep top-turn lanes actually above endpoint boxes.
            top_limit = min(src_top, dst_top) - 18.0
            compact_top = max(
                margin_y + 8.0,
                min(route_top_base - (back_slot_idx * 8.0), top_limit),
            )

            candidate_turns: list[float] = [compact_bottom]
            # Probe neighbouring bottom lanes. This is essential when return
            # routes from different protocols would otherwise share the exact
            # same outer horizontal rail.
            for delta in (
                minimum_clearance,
                -minimum_clearance,
                minimum_clearance * 2.0,
                -(minimum_clearance * 2.0),
            ):
                candidate = compact_bottom + delta
                if candidate < group_safe_bottom:
                    continue
                if candidate >= height - 14.0:
                    continue
                candidate_turns.append(candidate)
            # Right-to-left output->input links default to wrapping below.
            allow_top_wrap = not (
                backward_out_to_in
                and backward_out_to_in_wrap == "below"
            )
            if allow_top_wrap and compact_top < min(sy, dy) - 3.0:
                candidate_turns.append(compact_top)
            if not overview_mode:
                candidate_turns.append(legacy_bottom)

            best_points = build_backward_points(candidate_turns[0])
            best_score = route_candidate_score(
                best_points,
                connection.source_device,
                connection.dest_device,
                src_box,
                dst_box,
                sy,
                dy,
                route_protocol,
                minimum_clearance,
            )
            if overview_mode and (
                int(best_score[0]) > 0 or int(best_score[2]) > 0
            ):
                legacy_points = build_backward_points(legacy_bottom)
                legacy_score = route_candidate_score(
                    legacy_points,
                    connection.source_device,
                    connection.dest_device,
                    src_box,
                    dst_box,
                    sy,
                    dy,
                    route_protocol,
                    minimum_clearance,
                )
                if legacy_score < best_score:
                    best_points = legacy_points
                    best_score = legacy_score
            for turn_y in candidate_turns[1:]:
                candidate_points = build_backward_points(turn_y)
                candidate_score = route_candidate_score(
                    candidate_points,
                    connection.source_device,
                    connection.dest_device,
                    src_box,
                    dst_box,
                    sy,
                    dy,
                    route_protocol,
                    minimum_clearance,
                )
                if candidate_score < best_score:
                    best_points = candidate_points
                    best_score = candidate_score
        else:
            span_columns = abs(source_col - dest_col)
            if overlap > 0:
                # If devices overlap on X, route outside boxes while preserving straight leads.
                side_base = max(36.0, lead_len * 0.55)
                if sx >= src_right - 0.5:
                    mx = max(src_right, dst_right) + side_base + lane_shift
                else:
                    mx = min(src_left, dst_left) - side_base + lane_shift
            else:
                low_x, high_x = min(src_lead_x, dst_lead_x), max(src_lead_x, dst_lead_x)
                base_mid = (low_x + high_x) / 2.0
                span = high_x - low_x
                if span > 64:
                    if fifo_turn and src_slot_total > 4:
                        fifo_density = min(1.0, max(0.0, (src_slot_total - 4) / 20.0))
                        edge_cap = 14.0 - (10.0 * fifo_density)
                        edge_ratio = 0.22 - (0.16 * fifo_density)
                        edge_cap = max(3.0, edge_cap)
                        edge_ratio = max(0.05, edge_ratio)
                        edge_pad = min(edge_cap, span * edge_ratio)
                    else:
                        edge_pad = min(14.0, span * 0.22)
                    if fifo_turn and src_slot_total > 1:
                        fifo_clearance_target = (
                            power_lane_spacing if family == "Power" else minimum_clearance
                        )
                        required_fifo_span = (src_slot_total - 1) * fifo_clearance_target
                        if span >= required_fifo_span:
                            # Do not spend corridor width on decorative edge
                            # padding when that would squeeze an otherwise
                            # comfortably spaced fan-out.
                            edge_pad = min(
                                edge_pad,
                                max(0.0, (span - required_fifo_span) / 2.0),
                            )
                    corridor_min = low_x + edge_pad
                    corridor_max = high_x - edge_pad
                else:
                    if span > 18:
                        corridor_min = low_x + min(6.0, span * 0.18)
                        corridor_max = high_x - min(6.0, span * 0.18)
                    else:
                        corridor_min = low_x
                        corridor_max = high_x

                if corridor_max - corridor_min > 1.0:
                    corridor_segments: list[tuple[float, float]] = []
                    for start, end in clear_x_intervals:
                        c_start = max(start, corridor_min)
                        c_end = min(end, corridor_max)
                        if c_end - c_start > 1.0:
                            corridor_segments.append((c_start, c_end))

                    # Distribute lanes across the available corridor to prevent stacked rails.
                    desired_frac = route_frac
                    if turn_order_direction < 0:
                        desired_frac = 1.0 - desired_frac
                    if (not fifo_turn) and span_columns >= 2:
                        # For long single-lane forward runs with strong vertical travel,
                        # bias the elbow earlier so the drop doesn't happen "too late".
                        vertical_span = abs(dy - sy)
                        horizontal_span = max(1.0, abs(dst_lead_x - src_lead_x))
                        vertical_dominance = vertical_span / horizontal_span
                        early_turn_bias = min(0.85, max(0.0, (vertical_dominance - 0.45) / 0.8))
                        if early_turn_bias > 0.0:
                            desired_frac = (desired_frac * (1.0 - early_turn_bias)) + (0.08 * early_turn_bias)
                    if (not fifo_turn) and span_columns >= 2 and family == "Video":
                        # HDMI/video runs are easier to read when the drop happens
                        # in the first free corridor rather than near the center split.
                        desired_frac = min(desired_frac, 0.20)

                    desired_mx = corridor_min + desired_frac * (corridor_max - corridor_min)
                    if slot_count > 1 and not fifo_turn:
                        slot_center = (slot_count - 1) / 2.0
                        fine_shift = (slot - slot_center) * min(
                            6.0, (corridor_max - corridor_min) / (slot_count + 1)
                        )
                        desired_mx += fine_shift * turn_order_direction

                    if corridor_segments:
                        if fifo_turn:
                            # Preserve FIFO order with uniform spacing across the
                            # effective (edge-margined) corridor.
                            mx = max(corridor_min, min(corridor_max, desired_mx))
                            if src_slot_total > 1:
                                uniform_frac = src_slot_idx / max(1, (src_slot_total - 1))
                            else:
                                uniform_frac = 0.5
                            if turn_order_direction < 0:
                                uniform_frac = 1.0 - uniform_frac
                            uniform_edge_margin = effective_turn_edge_margin
                            fifo_clearance_target = (
                                power_lane_spacing if family == "Power" else minimum_clearance
                            )
                            required_fifo_span = max(0, src_slot_total - 1) * fifo_clearance_target
                            if (corridor_max - corridor_min) >= required_fifo_span:
                                uniform_edge_margin = 0.0
                            if (
                                src_slot_total > 2
                                and uniform_edge_margin > 0.0
                            ):
                                uniform_frac = (
                                    uniform_edge_margin
                                    + (uniform_frac * (1.0 - (2.0 * uniform_edge_margin)))
                                )
                                uniform_frac = max(0.0, min(1.0, uniform_frac))
                            mx = corridor_min + (uniform_frac * (corridor_max - corridor_min))
                        else:
                            clear_mx = pick_clear_x(
                                desired_x=desired_mx,
                                low_x=corridor_min,
                                high_x=corridor_max,
                                clear_intervals=clear_x_intervals,
                            )
                            if clear_mx is not None:
                                mx = clear_mx
                            else:
                                mx = max(corridor_min, min(desired_mx, corridor_max))
                    else:
                        mx = max(corridor_min, min(desired_mx, corridor_max))

                    # Enforce a minimum horizontal gap between rails in the same route corridor.
                    lane_usage_key: tuple[object, ...]
                    if fifo_turn:
                        lane_usage_key = (
                            "fifo_turn",
                            route_key,
                            connection.source_device,
                            source_col,
                            dest_col,
                        )
                    else:
                        lane_usage_key = route_key
                    global_lane_usage_key: tuple[object, ...] = (
                        "forward_global",
                        route_key[2],
                    )
                    used_mx = route_used_mx[lane_usage_key]
                    global_used_mx: list[float] = route_used_mx[global_lane_usage_key]
                    min_gap = max(minimum_clearance, 16.0 if route_total > 1 else 12.0)
                    min_global_gap = max(minimum_clearance, 10.0 if overview_mode else 8.0)
                    fifo_min_gap_base = (
                        power_lane_spacing
                        if family == "Power"
                        else max(minimum_clearance, 8.0 if overview_mode else 10.0)
                    )
                    fifo_min_gap = fifo_min_gap_base
                    if fifo_turn and src_slot_total > 1:
                        corridor_span = max(0.0, corridor_max - corridor_min)
                        if corridor_span > 0.0:
                            effective_span = corridor_span
                            if src_slot_total > 2 and forward_turn_edge_margin > 0.0:
                                effective_span *= max(0.05, 1.0 - (2.0 * forward_turn_edge_margin))
                            max_uniform_gap = effective_span / max(1, (src_slot_total - 1))
                            # Relax FIFO spacing when a high channel-count fanout
                            # cannot physically fit the default gap in the corridor.
                            fifo_min_gap = max(0.5, min(fifo_min_gap_base, max_uniform_gap))

                    # FIFO groups should keep turn order monotonic from top-most source
                    # port to bottom-most source port. Unrelated wires must not force a
                    # later slot to "jump left" and create backwards-looking elbows.
                    if fifo_turn:
                        if prev_fifo_turn_x is not None:
                            if turn_order_direction > 0:
                                mx = max(mx, min(corridor_max, prev_fifo_turn_x + fifo_min_gap))
                            else:
                                mx = min(mx, max(corridor_min, prev_fifo_turn_x - fifo_min_gap))

                    def mx_is_clear(candidate: float) -> bool:
                        if fifo_turn:
                            if prev_fifo_turn_x is not None:
                                if (
                                    turn_order_direction > 0
                                    and candidate < (prev_fifo_turn_x + fifo_min_gap - 0.01)
                                ):
                                    return False
                                if (
                                    turn_order_direction < 0
                                    and candidate > (prev_fifo_turn_x - fifo_min_gap + 0.01)
                                ):
                                    return False
                            if any(abs(candidate - prev) < fifo_min_gap for prev in used_mx):
                                return False
                        else:
                            if any(abs(candidate - prev) < min_gap for prev in used_mx):
                                return False
                            if any(abs(candidate - prev) < min_global_gap for prev in global_used_mx):
                                return False
                        return True

                    if not mx_is_clear(mx):
                        if fifo_turn:
                            # Preserve the group's crossing-free turn order by
                            # searching only in its configured direction.
                            probe = mx
                            found = False
                            while corridor_min <= probe <= corridor_max:
                                if mx_is_clear(probe):
                                    mx = probe
                                    found = True
                                    break
                                probe += float(turn_order_direction)
                            if not found:
                                if prev_fifo_turn_x is None:
                                    mx = corridor_min if turn_order_direction > 0 else corridor_max
                                else:
                                    directed_x = prev_fifo_turn_x + (
                                        turn_order_direction * fifo_min_gap
                                    )
                                    mx = max(corridor_min, min(corridor_max, directed_x))
                        else:
                            best_candidate = mx
                            best_score = float("inf")
                            search_segments = (
                                corridor_segments
                                if corridor_segments
                                else [(corridor_min, corridor_max)]
                            )
                            for seg_start, seg_end in search_segments:
                                probe = seg_start
                                while probe <= seg_end:
                                    if mx_is_clear(probe):
                                        score = abs(probe - mx)
                                        if score < best_score:
                                            best_score = score
                                            best_candidate = probe
                                    probe += 1.0
                            if best_score < float("inf"):
                                mx = best_candidate
                            elif used_mx or global_used_mx:
                                occupied = used_mx + global_used_mx
                                edge_candidates = [
                                    corridor_min,
                                    corridor_max,
                                    (corridor_min + corridor_max) / 2.0,
                                ]
                                mx = max(
                                    edge_candidates,
                                    key=lambda cand: min(abs(cand - prev) for prev in occupied),
                                )
                    used_mx.append(mx)
                    if not fifo_turn:
                        global_used_mx.append(mx)
                else:
                    if span > 2:
                        mx = max(low_x + 1.0, min(base_mid, high_x - 1.0))
                    else:
                        mx = base_mid

            mx = max(14.0, min(mx, width - 14.0))
            if dx >= sx - 1.0 and overlap <= 0:
                # Keep forward elbows between lead anchors so routes never jog
                # backward near the destination endpoint.
                forward_min_x = min(src_lead_x, dst_lead_x)
                forward_max_x = max(src_lead_x, dst_lead_x)
                mx = max(forward_min_x, min(forward_max_x, mx))
            if fifo_turn and prev_fifo_turn_x is not None:
                # Last-resort guard: retain the selected monotonic direction.
                if turn_order_direction > 0 and mx < prev_fifo_turn_x:
                    mx = prev_fifo_turn_x
                elif turn_order_direction < 0 and mx > prev_fifo_turn_x:
                    mx = prev_fifo_turn_x
            # Long HDMI/video links are clearer when they drop near the source
            # rather than carrying a long top rail before the first turn.
            if (
                video_early_turn
                and family == "Video"
                and dx >= sx - 1.0
                and abs(dy - sy) >= (ROW_HEIGHT * max(1.0, video_vertical_rows_threshold))
            ):
                mx = src_lead_x

            # For very short vertical hops (adjacent destination rows), prefer a
            # cleaner single-turn look and avoid shared horizontal stubs that read
            # as odd corners/merges.
            if (
                not fifo_turn
                and abs(dy - sy) <= (ROW_HEIGHT + 4.0)
                and abs(dst_lead_x - src_lead_x) <= 220.0
            ):
                mx = src_lead_x
            # Keep near-level links visually straight by pushing tiny vertical
            # correction to the destination-side lead instead of mid-cable.
            if (
                not fifo_turn
                and abs(dy - sy) <= 4.0
                and abs(dst_lead_x - src_lead_x) <= 220.0
            ):
                mx = dst_lead_x
            # General sibling-row conflict rule: if this cable lands on a row
            # used as a sibling source row, turn near destination to avoid
            # shared horizontal rails/branch-like merges.
            if (
                not fifo_turn
                and source_row_conflict.get(index, False)
                and dx >= sx - 1.0
                and overlap <= 0
            ):
                mx = dst_lead_x
            # Remove tiny horizontal stubs before/after the main turn; they read as
            # visual kinks without adding routing value.
            # Do not apply this to FIFO fanouts, otherwise first/last channels can
            # collapse onto identical turn lanes and look inconsistent.
            if not fifo_turn:
                if abs(mx - src_lead_x) < 10.0:
                    mx = src_lead_x
                elif abs(dst_lead_x - mx) < 10.0:
                    mx = dst_lead_x
            if (not fifo_turn or family == "Power") and vertical_rail_needs_clearance(
                mx,
                sy,
                dy,
                connection.source_device,
                connection.dest_device,
                minimum_clearance,
            ):
                snap = pick_route_clear_x(
                    desired_x=mx,
                    low_x=12.0,
                    high_x=width - 12.0,
                    y1=sy,
                    y2=dy,
                    minimum_clearance=minimum_clearance,
                )
                if snap is not None:
                    mx = snap
            if fifo_turn and prev_fifo_turn_x is not None:
                if turn_order_direction > 0 and mx < prev_fifo_turn_x:
                    mx = prev_fifo_turn_x
                elif turn_order_direction < 0 and mx > prev_fifo_turn_x:
                    mx = prev_fifo_turn_x

            # Resolve same-row horizontal overlaps by re-selecting turn X within
            # lead bounds. This avoids branch-like merges while preserving clean
            # orthogonal paths (no extra micro-bends).
            use_row_overlap_adjust = (
                dx >= sx - 1.0
                and overlap <= 0
                and route_total > 1
                and route_total <= 8
                and not fifo_turn
            )
            if use_row_overlap_adjust:
                lead_low = min(src_lead_x, dst_lead_x)
                lead_high = max(src_lead_x, dst_lead_x)
                if lead_high - lead_low > 4.0:
                    def row_overlap(candidate_x: float) -> float:
                        return (
                            horizontal_overlap_extent(sy, src_lead_x, candidate_x)
                            + horizontal_overlap_extent(dy, candidate_x, dst_lead_x)
                        )

                    base_overlap = row_overlap(mx)
                    if base_overlap > 1.0:
                        desired_mx = mx
                        best_mx = mx
                        best_overlap = base_overlap
                        best_distance = 0.0
                        min_fifo_x = lead_low
                        if fifo_turn and prev_fifo_turn_x is not None:
                            min_fifo_x = max(min_fifo_x, prev_fifo_turn_x + fifo_min_gap)
                        probe = int(math.floor(lead_low))
                        probe_end = int(math.ceil(lead_high))
                        while probe <= probe_end:
                            candidate = float(probe)
                            probe += 1
                            if candidate < (min_fifo_x - 0.05):
                                continue
                            overlap_score = row_overlap(candidate)
                            distance_score = abs(candidate - desired_mx)
                            if (
                                overlap_score < (best_overlap - 0.05)
                                or (
                                    abs(overlap_score - best_overlap) <= 0.05
                                    and distance_score < best_distance
                                )
                            ):
                                best_mx = candidate
                                best_overlap = overlap_score
                                best_distance = distance_score
                        mx = best_mx
                        if fifo_turn and prev_fifo_turn_x is not None:
                            # Prefer FIFO spacing, but if that still creates a
                            # same-row merge, allow a small local retreat to
                            # keep wires visually separate.
                            preferred_mx = max(mx, prev_fifo_turn_x + fifo_min_gap)
                            preferred_mx = min(lead_high, max(lead_low, preferred_mx))
                            preferred_overlap = row_overlap(preferred_mx)
                            if preferred_overlap <= 2.0:
                                mx = preferred_mx
                            else:
                                retreat_min = max(lead_low, prev_fifo_turn_x - 14.0)
                                best_escape_mx = preferred_mx
                                best_escape_overlap = preferred_overlap
                                best_escape_dist = 0.0
                                probe = int(math.floor(retreat_min))
                                probe_end = int(math.ceil(lead_high))
                                while probe <= probe_end:
                                    candidate = float(probe)
                                    probe += 1
                                    if abs(candidate - prev_fifo_turn_x) < 3.0:
                                        continue
                                    overlap_score = row_overlap(candidate)
                                    distance_score = abs(candidate - preferred_mx)
                                    if (
                                        overlap_score < (best_escape_overlap - 0.05)
                                        or (
                                            abs(overlap_score - best_escape_overlap) <= 0.05
                                            and distance_score < best_escape_dist
                                        )
                                    ):
                                        best_escape_mx = candidate
                                        best_escape_overlap = overlap_score
                                        best_escape_dist = distance_score
                                mx = min(lead_high, max(lead_low, best_escape_mx))

            # Re-snap the final turn because the row-overlap adjustment above
            # can otherwise move it back beside an existing rail. FIFO signal
            # bundles have already been spaced together as a group; power keeps
            # this final safeguard because its clearance is safety-significant.
            if family == "Power" or not fifo_turn:
                final_low_x = 12.0
                final_high_x = width - 12.0
                if dx >= sx - 1.0 and overlap <= 0:
                    final_low_x = min(src_lead_x, dst_lead_x)
                    final_high_x = max(src_lead_x, dst_lead_x)
                if fifo_turn and prev_fifo_turn_x is not None:
                    final_low_x = max(final_low_x, prev_fifo_turn_x)
                if final_high_x >= final_low_x:
                    snap = pick_route_clear_x(
                        desired_x=mx,
                        low_x=final_low_x,
                        high_x=final_high_x,
                        y1=sy,
                        y2=dy,
                        minimum_clearance=minimum_clearance,
                    )
                    if snap is not None:
                        mx = snap

            direct_points = [
                (sx, sy),
                (src_lead_x, sy),
                (mx, sy),
                (mx, dy),
                (dst_lead_x, dy),
                (dx, dy),
            ]

            best_points = direct_points
            best_score = route_candidate_score(
                direct_points,
                connection.source_device,
                connection.dest_device,
                src_box,
                dst_box,
                sy,
                dy,
                route_protocol,
                minimum_clearance,
            )

            needs_clearance_detour = bool(
                int(best_score[2]) > 0
                or int(best_score[3]) > 0
                or int(best_score[4]) > 0
                or float(best_score[7]) > 0.0
            )
            # A single HDMI cable should stay in the endpoint band whenever
            # its compact route clears device boxes. Wire crossings already
            # have a white under-stroke and are less distracting than sending
            # one cable up to a remote outer lane and back down again.
            compact_video_route = (
                family == "Video"
                and int(best_score[0]) == 0
                and int(best_score[2]) == 0
            )
            if (
                (span_columns >= 2 or needs_clearance_detour)
                and not fifo_turn
                and not compact_video_route
            ):
                lane_spacing = power_lane_spacing if family == "Power" else (12.0 if overview_mode else 10.0)
                base_bottom = route_bottom_base + (route_pos * lane_spacing)
                if route_total > 1:
                    # Keep a reserved lane budget for later routes so high-index
                    # links don't collapse onto the exact same bottom detour row.
                    reserve_for_later = max(0.0, (route_total - 1 - route_pos) * lane_spacing)
                    slot_max_bottom = (height - 14.0) - reserve_for_later
                    base_bottom = min(base_bottom, slot_max_bottom)
                base_bottom = min(base_bottom, height - 14.0)
                bottom_candidates: list[float] = [base_bottom]
                if overview_mode:
                    for delta in (12.0, -12.0, 24.0, -24.0):
                        candidate_bottom = base_bottom + delta
                        if candidate_bottom <= max(sy, dy) + 3.0:
                            continue
                        if candidate_bottom >= height - 14.0:
                            continue
                        bottom_candidates.append(candidate_bottom)
                seen_bottoms: set[float] = set()
                for outer_bottom in bottom_candidates:
                    lane_key = round(outer_bottom, 1)
                    if lane_key in seen_bottoms:
                        continue
                    seen_bottoms.add(lane_key)
                    src_drop_x = src_lead_x
                    dst_rise_x = dst_lead_x
                    if abs(outer_bottom - sy) > 0.05 and vertical_rail_needs_clearance(
                        src_drop_x,
                        sy,
                        outer_bottom,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=src_drop_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=sy,
                            y2=outer_bottom,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            src_drop_x = snap
                    if abs(outer_bottom - dy) > 0.05 and vertical_rail_needs_clearance(
                        dst_rise_x,
                        dy,
                        outer_bottom,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=dst_rise_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=dy,
                            y2=outer_bottom,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            dst_rise_x = snap

                    detour_points: list[tuple[float, float]] = [
                        (sx, sy),
                        (src_lead_x, sy),
                    ]
                    if abs(src_drop_x - src_lead_x) > 0.05:
                        detour_points.append((src_drop_x, sy))
                    detour_points.extend(
                        [
                            (src_drop_x, outer_bottom),
                            (dst_rise_x, outer_bottom),
                            (dst_rise_x, dy),
                        ]
                    )
                    if abs(dst_lead_x - dst_rise_x) > 0.05:
                        detour_points.append((dst_lead_x, dy))
                    detour_points.append((dx, dy))

                    detour_score = route_candidate_score(
                        detour_points,
                        connection.source_device,
                        connection.dest_device,
                        src_box,
                        dst_box,
                        sy,
                        dy,
                        route_protocol,
                        minimum_clearance,
                    )
                    if detour_score < best_score:
                        best_points = detour_points
                        best_score = detour_score

            top_lane_limit = min(sy, dy) - 4.0
            if (
                (span_columns >= 2 or needs_clearance_detour)
                and not fifo_turn
                and not compact_video_route
            ):
                lane_spacing = power_lane_spacing if family == "Power" else (12.0 if overview_mode else 10.0)
                base_top = stacked_top_route_lane(
                    route_top_base,
                    route_pos,
                    lane_spacing,
                    margin_y,
                )
                if route_total > 1:
                    # Keep enough vertical budget so each earlier/later route can
                    # still have a distinct top lane near the destination side.
                    reserve_for_later = max(0.0, (route_total - 1 - route_pos) * lane_spacing)
                    slot_max_top = top_lane_limit - reserve_for_later
                    base_top = min(base_top, slot_max_top)
                base_top = max(margin_y + 8.0, base_top)
            else:
                base_top = route_top_base
            if (
                span_columns >= 2
                and not fifo_turn
                and not compact_video_route
                and base_top < top_lane_limit
            ):
                top_candidates: list[float] = [base_top]
                if overview_mode:
                    for delta in (-12.0, -24.0, -36.0, -48.0):
                        candidate_top = base_top + delta
                        if candidate_top < margin_y + 8.0:
                            continue
                        if candidate_top >= top_lane_limit:
                            continue
                        top_candidates.append(candidate_top)
                seen_tops: set[float] = set()
                for top_lane in top_candidates:
                    lane_key = round(top_lane, 1)
                    if lane_key in seen_tops:
                        continue
                    seen_tops.add(lane_key)
                    src_rise_x = src_lead_x
                    dst_drop_x = dst_lead_x
                    if abs(top_lane - sy) > 0.05 and vertical_rail_needs_clearance(
                        src_rise_x,
                        sy,
                        top_lane,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=src_rise_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=sy,
                            y2=top_lane,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            src_rise_x = snap
                    if abs(top_lane - dy) > 0.05 and vertical_rail_needs_clearance(
                        dst_drop_x,
                        dy,
                        top_lane,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=dst_drop_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=dy,
                            y2=top_lane,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            dst_drop_x = snap

                    top_points: list[tuple[float, float]] = [
                        (sx, sy),
                        (src_lead_x, sy),
                    ]
                    if abs(src_rise_x - src_lead_x) > 0.05:
                        top_points.append((src_rise_x, sy))
                    top_points.extend(
                        [
                            (src_rise_x, top_lane),
                            (dst_drop_x, top_lane),
                            (dst_drop_x, dy),
                        ]
                    )
                    if abs(dst_lead_x - dst_drop_x) > 0.05:
                        top_points.append((dst_lead_x, dy))
                    top_points.append((dx, dy))

                    top_score = route_candidate_score(
                        top_points,
                        connection.source_device,
                        connection.dest_device,
                        src_box,
                        dst_box,
                        sy,
                        dy,
                        route_protocol,
                        minimum_clearance,
                    )
                    if top_score < best_score:
                        best_points = top_points
                        best_score = top_score

            # Overview safety fallback: if a forward route still crosses other
            # device boxes, try explicit outer-lane routes above/below all boxes.
            if overview_mode and best_score and (
                int(best_score[0]) > 0 or int(best_score[2]) > 0
            ):
                outer_lane_spacing = power_lane_spacing if family == "Power" else 12.0
                reserve_for_later = (
                    max(0.0, (route_total - 1 - route_pos) * outer_lane_spacing)
                    if route_total > 1
                    else 0.0
                )
                top_outer = stacked_top_route_lane(
                    route_top_base - 8.0,
                    route_pos,
                    outer_lane_spacing,
                    margin_y,
                )
                top_outer_cap = min(sy, dy) - 6.0 - reserve_for_later
                top_outer = min(top_outer, top_outer_cap)

                bottom_outer = min(height - 14.0, (max_bottom + 20.0) + (route_pos * outer_lane_spacing))
                bottom_outer_cap = (height - 14.0) - reserve_for_later
                bottom_outer = min(bottom_outer, bottom_outer_cap)

                outer_lanes: list[float] = []
                if top_outer >= margin_y + 8.0:
                    outer_lanes.append(top_outer)
                if bottom_outer <= height - 14.0:
                    outer_lanes.append(bottom_outer)
                # Probe nearby lanes to reduce residual rail stacking when many
                # routes are forced into fallback mode.
                for delta in (-12.0, -24.0, -36.0, -48.0):
                    candidate_top = top_outer + delta
                    if margin_y + 8.0 <= candidate_top < (min(sy, dy) - 4.0):
                        outer_lanes.append(candidate_top)
                    candidate_bottom = bottom_outer + delta
                    if (max(sy, dy) + 3.0) < candidate_bottom <= (height - 14.0):
                        outer_lanes.append(candidate_bottom)
                seen_outer: set[float] = set()
                for outer_lane in outer_lanes:
                    lane_key = round(outer_lane, 1)
                    if lane_key in seen_outer:
                        continue
                    seen_outer.add(lane_key)
                    if abs(outer_lane - sy) < 3.0 and abs(outer_lane - dy) < 3.0:
                        continue

                    src_outer_x = src_lead_x
                    dst_outer_x = dst_lead_x
                    if abs(outer_lane - sy) > 0.05 and vertical_rail_needs_clearance(
                        src_outer_x,
                        sy,
                        outer_lane,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=src_outer_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=sy,
                            y2=outer_lane,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            src_outer_x = snap
                    if abs(outer_lane - dy) > 0.05 and vertical_rail_needs_clearance(
                        dst_outer_x,
                        dy,
                        outer_lane,
                        connection.source_device,
                        connection.dest_device,
                        minimum_clearance,
                    ):
                        snap = pick_route_clear_x(
                            desired_x=dst_outer_x,
                            low_x=12.0,
                            high_x=width - 12.0,
                            y1=dy,
                            y2=outer_lane,
                            minimum_clearance=minimum_clearance,
                        )
                        if snap is not None:
                            dst_outer_x = snap

                    outer_points: list[tuple[float, float]] = [
                        (sx, sy),
                        (src_lead_x, sy),
                    ]
                    if abs(src_outer_x - src_lead_x) > 0.05:
                        outer_points.append((src_outer_x, sy))
                    outer_points.extend(
                        [
                            (src_outer_x, outer_lane),
                            (dst_outer_x, outer_lane),
                            (dst_outer_x, dy),
                        ]
                    )
                    if abs(dst_lead_x - dst_outer_x) > 0.05:
                        outer_points.append((dst_lead_x, dy))
                    outer_points.append((dx, dy))

                    outer_score = route_candidate_score(
                        outer_points,
                        connection.source_device,
                        connection.dest_device,
                        src_box,
                        dst_box,
                        sy,
                        dy,
                        route_protocol,
                        minimum_clearance,
                    )
                    if outer_score < best_score:
                        best_points = outer_points
                        best_score = outer_score
        # A collapsed multichannel trunk starts and ends on its dotted
        # collector rails. Starting at the averaged device anchor would make
        # the dashed trunk overshoot through the collector toward the device.
        multichannel_source_bundle_x: float | None = None
        multichannel_dest_bundle_x: float | None = None
        if connection.connection_type.strip().upper().startswith("MC"):
            source_parts = [
                part.strip()
                for part in connection.source_jack.split("+")
                if part.strip()
            ]
            source_part_ys = sorted(
                {
                    y
                    for part in source_parts
                    for y in [src_box.out_port_y.get(part, src_box.in_port_y.get(part))]
                    if y is not None
                }
            )
            if len(source_part_ys) >= 2:
                multichannel_source_bundle_x = max(
                    8.0,
                    min(width - 8.0, sx + (source_dir * 14.0)),
                )
                if best_points:
                    best_points[0] = (multichannel_source_bundle_x, sy)

            dest_parts = [
                part.strip()
                for part in connection.dest_jack.split("+")
                if part.strip()
            ]
            dest_part_ys = sorted(
                {
                    y
                    for part in dest_parts
                    for y in [dst_box.in_port_y.get(part, dst_box.out_port_y.get(part))]
                    if y is not None
                }
            )
            if len(dest_part_ys) >= 2:
                multichannel_dest_bundle_x = max(
                    8.0,
                    min(width - 8.0, dx + (dest_dir * 14.0)),
                )
                if best_points:
                    best_points[-1] = (multichannel_dest_bundle_x, dy)

        # A collapsed stereo source must still show both physical mono outputs.
        # Start the trunk at a small collector outside the device instead of at
        # the averaged (and therefore non-existent) port position between them.
        stereo_source_collector: tuple[float, list[float]] | None = None
        if connection.connection_type.strip().upper() == "ST" and "+" in connection.source_jack:
            source_parts = [
                part.strip()
                for part in connection.source_jack.split("+")
                if part.strip()
            ]
            source_part_ys: list[float] = []
            for part in source_parts:
                part_y = src_box.out_port_y.get(part)
                if part_y is None:
                    part_y = src_box.in_port_y.get(part)
                if part_y is not None and not any(abs(part_y - seen_y) < 0.05 for seen_y in source_part_ys):
                    source_part_ys.append(part_y)
            source_part_ys.sort()
            if len(source_part_ys) >= 2:
                available_lead = abs(src_lead_x - sx)
                collector_offset = max(6.0, min(14.0, available_lead * 0.5))
                collector_x = max(
                    8.0,
                    min(width - 8.0, sx + (source_dir * collector_offset)),
                )
                stereo_source_collector = (collector_x, source_part_ys)
                if best_points:
                    best_points[0] = (collector_x, sy)

        best_points = simplify_orthogonal_route(best_points)
        best_score = route_candidate_score(
            best_points,
            connection.source_device,
            connection.dest_device,
            src_box,
            dst_box,
            sy,
            dy,
            route_protocol,
            minimum_clearance,
        )
        path = route_to_path(best_points)
        if fifo_turn:
            final_turn_x = first_vertical_turn_x(best_points)
            if final_turn_x is not None:
                fifo_last_mx[fifo_group_key] = final_turn_x

        for seg_axis, seg_const, seg_start, seg_end in route_segments(best_points):
            routed_segments.append((seg_axis, seg_const, seg_start, seg_end, route_protocol))

        if route_debug_records is not None:
            route_debug_records.append(
                {
                    "layer": layer,
                    "overview_mode": overview_mode,
                    "cable_id": connection.cable_id,
                    "family": family,
                    "protocol": route_protocol,
                    "source": {
                        "device": connection.source_device,
                        "port": connection.source_jack,
                        "x": round(sx, 1),
                        "y": round(sy, 1),
                        "column": source_col,
                    },
                    "destination": {
                        "device": connection.dest_device,
                        "port": connection.dest_jack,
                        "x": round(dx, 1),
                        "y": round(dy, 1),
                        "column": dest_col,
                    },
                    "route_mode": (
                        "backward_wrap_below"
                        if backward and backward_out_to_in
                        else ("backward" if backward else "forward")
                    ),
                    "route_slots": {
                        "route_pos": route_pos,
                        "route_total": route_total,
                        "source_slot_idx": src_slot_idx,
                        "source_slot_total": src_slot_total,
                        "dest_slot_idx": dst_slot_idx,
                        "dest_slot_total": dst_slot_total,
                    },
                    "anchors": {
                        "src_lead_x": round(src_lead_x, 1),
                        "dst_lead_x": round(dst_lead_x, 1),
                    },
                    "points": [
                        {"x": round(px, 1), "y": round(py, 1)}
                        for px, py in best_points
                    ],
                    "path": path,
                    "score": serialize_route_score(best_score),
                }
            )

        family_counts[legend_family] += 1
        family_colors[legend_family] = wire_color
        bidirectional = (
            connection.cable_id in bidirectional_connection_ids
            or is_bidirectional_connection(connection)
        )
        is_multichannel = connection.connection_type.upper().startswith("MC")
        stroke_width = 1.8 if "normalled" in connection.status.lower() else 1.35
        if is_multichannel:
            stroke_width = max(stroke_width, 2.3)
        stroke_dash = ""
        if is_multichannel:
            stroke_dash = ' stroke-dasharray="8 4"'
        elif "patch override" in connection.status.lower():
            stroke_dash = ' stroke-dasharray="7 5"'
        marker_attrs = (
            ' marker-start="url(#arrow)" marker-end="url(#arrow)"'
            if bidirectional
            else ' marker-end="url(#arrow)"'
        )

        def part_anchor_y(box: DeviceBox, part: str, prefer: str) -> float | None:
            if prefer == "out":
                if part in box.out_port_y:
                    return box.out_port_y[part]
                if part in box.in_port_y:
                    return box.in_port_y[part]
            else:
                if part in box.in_port_y:
                    return box.in_port_y[part]
                if part in box.out_port_y:
                    return box.out_port_y[part]
            return None

        def unique_sorted(values: list[float]) -> list[float]:
            seen: set[int] = set()
            result: list[float] = []
            for value in sorted(values):
                key = int(round(value * 10.0))
                if key in seen:
                    continue
                seen.add(key)
                result.append(value)
            return result

        src_ys: list[float] = []
        dst_ys: list[float] = []
        if is_multichannel:
            src_parts = [part.strip() for part in connection.source_jack.split("+") if part.strip()]
            dst_parts = [part.strip() for part in connection.dest_jack.split("+") if part.strip()]
            src_ys = unique_sorted(
                [
                    y
                    for y in (
                        part_anchor_y(src_box, part, "out")
                        for part in src_parts
                    )
                    if y is not None
                ]
            )
            dst_ys = unique_sorted(
                [
                    y
                    for y in (
                        part_anchor_y(dst_box, part, "in")
                        for part in dst_parts
                    )
                    if y is not None
                ]
            )

            # The breakout branches carry the endpoint arrows for a snake.
            # Avoid leaving an extra arrow on the bundled trunk between ports.
            main_marker_parts: list[str] = []
            if bidirectional and len(src_ys) < 2:
                main_marker_parts.append('marker-start="url(#arrow)"')
            if len(dst_ys) < 2:
                main_marker_parts.append('marker-end="url(#arrow)"')
            marker_attrs = (
                f" {' '.join(main_marker_parts)}"
                if main_marker_parts
                else ""
            )

        detail = html.escape(
            f"{connection.cable_id}: {connection.source_device} [{connection.source_jack}] -> {connection.dest_device} [{connection.dest_jack}]"
        )

        connection_wire_lines = [
            f'  <path d="{path}" fill="none" stroke="#f8fafc" stroke-width="{stroke_width + 2.4:.2f}"{stroke_dash} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"/>',
            f'  <path d="{path}" fill="none" stroke="{wire_color}" stroke-width="{stroke_width}"{stroke_dash} stroke-linecap="round" stroke-linejoin="round"{marker_attrs}><title>{detail}</title></path>',
        ]

        if stereo_source_collector is not None:
            collector_x, source_part_ys = stereo_source_collector
            collector_stroke = max(1.35, stroke_width)
            connection_wire_lines.append(
                f'  <line class="stereo-source-collector" x1="{collector_x:.1f}" y1="{source_part_ys[0]:.1f}" x2="{collector_x:.1f}" y2="{source_part_ys[-1]:.1f}" stroke="#f8fafc" stroke-width="{collector_stroke + 2.4:.2f}" stroke-linecap="round" aria-hidden="true"/>'
            )
            connection_wire_lines.append(
                f'  <line class="stereo-source-collector" x1="{collector_x:.1f}" y1="{source_part_ys[0]:.1f}" x2="{collector_x:.1f}" y2="{source_part_ys[-1]:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round"/>'
            )
            for part_y in source_part_ys:
                connection_wire_lines.append(
                    f'  <line class="stereo-source-branch" x1="{sx:.1f}" y1="{part_y:.1f}" x2="{collector_x:.1f}" y2="{part_y:.1f}" stroke="#f8fafc" stroke-width="{collector_stroke + 2.4:.2f}" stroke-linecap="round" aria-hidden="true"/>'
                )
                connection_wire_lines.append(
                    f'  <line class="stereo-source-branch" x1="{sx:.1f}" y1="{part_y:.1f}" x2="{collector_x:.1f}" y2="{part_y:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round"/>'
                )
            connection_wire_lines.append(
                f'  <circle class="stereo-source-merge" cx="{collector_x:.1f}" cy="{sy:.1f}" r="2.8" fill="{wire_color}" stroke="#f8fafc" stroke-width="1.1"/>'
            )

        # Multichannel links get explicit fan-in / fan-out collectors so it's
        # clear that multiple channel ports feed one bundled trunk.
        if is_multichannel:
            collector_offset = 14.0
            collector_stroke = max(1.25, stroke_width - 0.25)
            collector_dash = ' stroke-dasharray="2 2"'

            if len(src_ys) >= 2:
                src_bundle_x = (
                    multichannel_source_bundle_x
                    if multichannel_source_bundle_x is not None
                    else max(8.0, min(width - 8.0, sx + (source_dir * collector_offset)))
                )
                connection_wire_lines.append(
                    f'  <line class="multichannel-source-collector" x1="{src_bundle_x:.1f}" y1="{src_ys[0]:.1f}" x2="{src_bundle_x:.1f}" y2="{src_ys[-1]:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round"{collector_dash}/>'
                )
                for y in src_ys:
                    source_marker = ' marker-start="url(#arrow)"' if bidirectional else ""
                    connection_wire_lines.append(
                        f'  <line class="multichannel-source-branch" x1="{sx:.1f}" y1="{y:.1f}" x2="{src_bundle_x:.1f}" y2="{y:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round" opacity="0.95"{source_marker}/>'
                    )

            if len(dst_ys) >= 2:
                dst_bundle_x = (
                    multichannel_dest_bundle_x
                    if multichannel_dest_bundle_x is not None
                    else max(8.0, min(width - 8.0, dx + (dest_dir * collector_offset)))
                )
                connection_wire_lines.append(
                    f'  <line class="multichannel-destination-collector" x1="{dst_bundle_x:.1f}" y1="{dst_ys[0]:.1f}" x2="{dst_bundle_x:.1f}" y2="{dst_ys[-1]:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round"{collector_dash}/>'
                )
                for y in dst_ys:
                    connection_wire_lines.append(
                        f'  <line class="multichannel-destination-branch" x1="{dst_bundle_x:.1f}" y1="{y:.1f}" x2="{dx:.1f}" y2="{y:.1f}" stroke="{wire_color}" stroke-width="{collector_stroke:.2f}" stroke-linecap="round" opacity="0.95" marker-end="url(#arrow)"/>'
                    )

        pending_connection_wire_lines.append((family == "Power", connection_wire_lines))

        # Put labels on straight lead segments and avoid label-on-label collisions.
        if raw_label:
            if source_label_side == "below":
                src_label_candidates = [0.0] + [label_offset_step * idx for idx in range(1, 7)]
                src_label_y_base = sy + label_wire_gap + 10.0
                src_label_x, src_label_y, src_rect_x = find_label_position(
                    desired_x=(sx + src_lead_x) / 2.0,
                    desired_y=src_label_y_base,
                    label_width_px=label_width,
                    y_offsets=src_label_candidates,
                    blocked_lines=None,
                    min_allowed_y=min(height - 16.0, sy + label_wire_gap + 10.0),
                )
            else:
                src_label_candidates = [0.0] + [-(label_offset_step * idx) for idx in range(1, 7)]
                src_label_y_base = sy - label_wire_gap - 4.0
                src_label_x, src_label_y, src_rect_x = find_label_position(
                    desired_x=(sx + src_lead_x) / 2.0,
                    desired_y=src_label_y_base,
                    label_width_px=label_width,
                    y_offsets=src_label_candidates,
                    blocked_lines=None,
                    max_allowed_y=max(margin_y + 20.0, sy - label_wire_gap - 4.0),
                )
            pending_connection_label_lines.append(
                f'  <rect x="{src_rect_x:.1f}" y="{src_label_y - 10.0:.1f}" width="{label_width + 6.0:.1f}" height="14.0" rx="2" ry="2" fill="#ffffff" fill-opacity="1" stroke="#e2e8f0" stroke-width="0.9"/>'
            )
            pending_connection_label_lines.append(
                f'  <text x="{src_label_x:.1f}" y="{src_label_y:.1f}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="{label_font_size:.1f}" fill="{wire_color}">{cable_label}</text>'
            )
            if dest_label_side == "above":
                dst_label_candidates = [0.0] + [-(label_offset_step * idx) for idx in range(1, 7)]
                dst_label_y_base = dy - label_wire_gap - 4.0
                dst_label_x, dst_label_y, dst_rect_x = find_label_position(
                    desired_x=(dx + dst_lead_x) / 2.0,
                    desired_y=dst_label_y_base,
                    label_width_px=label_width,
                    y_offsets=dst_label_candidates,
                    blocked_lines=None,
                    max_allowed_y=max(margin_y + 20.0, dy - label_wire_gap - 4.0),
                )
            else:
                dst_label_candidates = [0.0] + [label_offset_step * idx for idx in range(1, 7)]
                dst_label_y_base = dy + label_wire_gap + 10.0
                dst_label_x, dst_label_y, dst_rect_x = find_label_position(
                    desired_x=(dx + dst_lead_x) / 2.0,
                    desired_y=dst_label_y_base,
                    label_width_px=label_width,
                    y_offsets=dst_label_candidates,
                    blocked_lines=None,
                    min_allowed_y=min(height - 16.0, dy + label_wire_gap + 10.0),
                )
            pending_connection_label_lines.append(
                f'  <rect x="{dst_rect_x:.1f}" y="{dst_label_y - 10.0:.1f}" width="{label_width + 6.0:.1f}" height="14.0" rx="2" ry="2" fill="#ffffff" fill-opacity="1" stroke="#e2e8f0" stroke-width="0.9"/>'
            )
            pending_connection_label_lines.append(
                f'  <text x="{dst_label_x:.1f}" y="{dst_label_y:.1f}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="{label_font_size:.1f}" fill="{wire_color}">{cable_label}</text>'
            )

    # Paint signal wires first and power wires last. The pale under-stroke makes
    # every crossing legible while keeping the power layer visibly on top.
    for is_power in (False, True):
        for wire_is_power, wire_lines in pending_connection_wire_lines:
            if wire_is_power == is_power:
                svg_lines.extend(wire_lines)

    # Draw all connection labels after all wires so no wire can overlap text boxes.
    svg_lines.extend(pending_connection_label_lines)

    for box in boxes.values():
        title_text = truncate_to_px(box.name, box.width - 96)
        connected_count = sum(
            1 for port in box.port_roles.keys() if box.port_connected.get(port, False)
        )
        total_count = len(box.port_roles)
        row_count = max(len(box.in_ports), len(box.out_ports), 1)

        svg_lines.extend(
            [
                f'  <rect x="{box.x:.1f}" y="{box.y:.1f}" width="{box.width:.1f}" height="{box.height:.1f}" rx="6" ry="6" fill="#ffffff" stroke="#334155" stroke-width="1.2"/>',
                f'  <rect x="{box.x:.1f}" y="{box.y:.1f}" width="{box.width:.1f}" height="{HEADER_HEIGHT:.1f}" rx="6" ry="6" fill="#e2e8f0" stroke="#334155" stroke-width="1.0"/>',
                f'  <line x1="{box.x:.1f}" y1="{box.y + HEADER_HEIGHT:.1f}" x2="{box.x + box.width:.1f}" y2="{box.y + HEADER_HEIGHT:.1f}" stroke="#334155" stroke-width="1"/>',
                f'  <text x="{box.x + 10:.1f}" y="{box.y + 14:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="9" fill="#475569">IN</text>',
                f'  <text x="{box.x + box.width - 10:.1f}" y="{box.y + 14:.1f}" text-anchor="end" font-family="Helvetica, Arial, sans-serif" font-size="9" fill="#475569">OUT</text>',
                f'  <text x="{box.x + box.width / 2.0:.1f}" y="{box.y + 20:.1f}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="12" fill="#0f172a">{html.escape(title_text)}</text>',
                f'  <text x="{box.x + box.width / 2.0:.1f}" y="{box.y + 31:.1f}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="9" fill="#64748b">{connected_count}/{total_count} wired ports</text>',
            ]
        )

        # The combined overview summarizes mains power as a compact input-side
        # circuit badge instead of drawing power routes, ports, or distributor
        # devices through the signal-flow diagram.
        power_group = (overview_power_groups or {}).get(box.name)
        if overview_mode and power_group:
            badge_width = 48.0
            badge_height = 16.0
            badge_x = box.x - badge_width + 3.0
            badge_y = box.y + 10.0
            badge_color = power_group_color(power_group)
            svg_lines.extend(
                [
                    f'  <rect data-power-group="{html.escape(power_group)}" x="{badge_x:.1f}" y="{badge_y:.1f}" width="{badge_width:.1f}" height="{badge_height:.1f}" rx="3" ry="3" fill="{badge_color}" stroke="#ffffff" stroke-width="1.2"><title>Power circuit: {html.escape(power_group)}</title></rect>',
                    f'  <text x="{badge_x + badge_width / 2.0:.1f}" y="{badge_y + 11.2:.1f}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="8.5" font-weight="700" fill="#ffffff">{html.escape(power_group)}</text>',
                ]
            )

        for index in range(row_count):
            py = box.y + HEADER_HEIGHT + index * ROW_HEIGHT + (ROW_HEIGHT / 2.0)
            if index > 0:
                y_line = box.y + HEADER_HEIGHT + index * ROW_HEIGHT
                svg_lines.append(
                    f'  <line x1="{box.x:.1f}" y1="{y_line:.1f}" x2="{box.x + box.width:.1f}" y2="{y_line:.1f}" stroke="#cbd5e1" stroke-width="1"/>'
                )

            if index < len(box.in_ports):
                in_port = box.in_ports[index]
                if in_port:
                    in_connected = box.port_connected.get(in_port, False)
                    in_text = truncate_to_px(abbreviate_port_label(in_port), (box.width / 2.0) - 20)
                    in_text_color = "#1e293b" if in_connected else "#94a3b8"
                    in_fill = "#334155" if in_connected else "#ffffff"
                    in_stroke = "#334155" if in_connected else "#94a3b8"
                    svg_lines.append(
                        f'  <text x="{box.x + 13:.1f}" y="{py + 3:.1f}" text-anchor="start" font-family="Helvetica, Arial, sans-serif" font-size="9" fill="{in_text_color}">{html.escape(in_text)}</text>'
                    )
                    svg_lines.append(
                        f'  <circle cx="{box.x:.1f}" cy="{py:.1f}" r="2.2" fill="{in_fill}" stroke="{in_stroke}" stroke-width="1"/>'
                    )

            if index < len(box.out_ports):
                out_port = box.out_ports[index]
                if out_port:
                    out_connected = box.port_connected.get(out_port, False)
                    out_text = truncate_to_px(abbreviate_port_label(out_port), (box.width / 2.0) - 20)
                    out_text_color = "#1e293b" if out_connected else "#94a3b8"
                    out_fill = "#334155" if out_connected else "#ffffff"
                    out_stroke = "#334155" if out_connected else "#94a3b8"
                    svg_lines.append(
                        f'  <text x="{box.x + box.width - 13:.1f}" y="{py + 3:.1f}" text-anchor="end" font-family="Helvetica, Arial, sans-serif" font-size="9" fill="{out_text_color}">{html.escape(out_text)}</text>'
                    )
                    svg_lines.append(
                        f'  <circle cx="{box.x + box.width:.1f}" cy="{py:.1f}" r="2.2" fill="{out_fill}" stroke="{out_stroke}" stroke-width="1"/>'
                    )

    # Draw group labels after boxes/wires so labels never hide behind device blocks.
    draw_group_blocks(svg_lines, display_groups, draw_boxes=False, draw_labels=True)

    legend_x = width - 290.0
    legend_y = margin_y + 6.0
    legend_items = sorted(family_counts.items(), key=lambda item: natural_key(item[0]))
    legend_height = 28.0 + (len(legend_items) * 14.0) + 34.0
    svg_lines.append(
        f'  <rect x="{legend_x - 10:.1f}" y="{legend_y - 14:.1f}" width="280" height="{legend_height:.1f}" rx="8" ry="8" fill="#ffffff" fill-opacity="0.9" stroke="#cbd5e1" stroke-width="1"/>'
    )
    legend_title = "Power Groups" if power_group_by_connection else "Connection Types"
    svg_lines.append(
        f'  <text x="{legend_x:.1f}" y="{legend_y - 2:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#475569">{legend_title}</text>'
    )
    for index, (family, count) in enumerate(legend_items):
        y = legend_y + 13 + (index * 14)
        color = family_colors[family]
        svg_lines.append(
            f'  <rect x="{legend_x:.1f}" y="{y - 8:.1f}" width="9" height="9" fill="{color}" rx="1" ry="1"/>'
        )
        svg_lines.append(
            f'  <text x="{legend_x + 14:.1f}" y="{y:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#475569">{html.escape(family)} ({count})</text>'
        )

    info_y = legend_y + 26 + (len(legend_items) * 14)
    svg_lines.append(
        f'  <text x="{legend_x:.1f}" y="{info_y:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#64748b">IN ports are left, OUT ports are right</text>'
    )
    if not overview_mode:
        svg_lines.append(
            f'  <text x="{legend_x:.1f}" y="{info_y + 13:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#64748b">Unwired ports are light gray</text>'
        )
    else:
        svg_lines.append(
            f'  <text x="{legend_x:.1f}" y="{info_y + 13:.1f}" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#64748b">Overview mode: only wired ports shown</text>'
        )
    svg_lines.append("</svg>")

    return "\n".join(svg_lines)


def render_connection_table(
    connections: list[Connection],
    device_types: dict[str, str],
) -> str:
    rows: list[str] = []
    for connection in sorted(connections, key=lambda item: natural_key(item.cable_id)):
        rows.append(
            "<tr>"
            f"<td>{html.escape(connection.cable_id)}</td>"
            f"<td>{html.escape(connection.source_device)}</td>"
            f"<td>{html.escape(device_types.get(connection.source_device, 'Other'))}</td>"
            f"<td>{html.escape(connection.source_jack)}</td>"
            f"<td>{html.escape(connection.dest_device)}</td>"
            f"<td>{html.escape(device_types.get(connection.dest_device, 'Other'))}</td>"
            f"<td>{html.escape(connection.dest_jack)}</td>"
            f"<td>{html.escape(connection.cable_type)}</td>"
            f"<td>{html.escape(connection.connection_type)}</td>"
            f"<td>{html.escape(connection.signal_type)}</td>"
            f"<td>{html.escape(connection.status)}</td>"
            "</tr>"
        )
    body = "\n".join(rows)
    return (
        '<div class="connection-table-wrap">'
        "<table>"
        "<thead><tr><th>Cable ID</th><th>Source Device</th><th>Source Type</th><th>Source Port</th><th>Dest Device</th><th>Dest Type</th><th>Dest Port</th><th>Cable Type</th><th>Connection Type</th><th>Signal Type</th><th>Status</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
        "</div>"
    )


def build_html(
    title: str,
    grouped_connections: dict[str, list[Connection]],
    svgs: dict[str, str],
    generated_on: dt.date,
    layer_extra_devices: dict[str, set[str]] | None = None,
) -> str:
    generated_iso = generated_on.isoformat()
    extras = layer_extra_devices or {}
    sections: list[str] = []
    for layer in sorted(grouped_connections, key=natural_key):
        connections = grouped_connections[layer]
        color = resolve_layer_color(layer)
        layer_devices = (
            {c.source_device for c in connections}
            | {c.dest_device for c in connections}
            | set(extras.get(layer, set()))
        )
        layer_types = {
            device: classify_device_type(device)
            for device in layer_devices
        }
        section = (
            f'<section class="layer" id="{slugify(layer)}">\n'
            '  <div class="layer-head">\n'
            f'    <h2><span class="swatch" style="background:{color};"></span>{html.escape(layer)}</h2>\n'
            '    <div class="layer-stats">\n'
            f'      <span class="stat-chip">Cables <strong>{len(connections)}</strong></span>\n'
            f'      <span class="stat-chip">Devices <strong>{len(layer_devices)}</strong></span>\n'
            '    </div>\n'
            '  </div>\n'
            '  <p class="layer-note">Grouped by device type. IN ports are left, OUT ports are right.</p>\n'
            f'  <div class="diagram">{svgs[layer]}</div>\n'
            f'  <details class="connection-details"><summary>Connection List ({len(connections)})</summary>\n'
            f"    {render_connection_table(connections, layer_types)}\n"
            "  </details>\n"
            "</section>"
        )
        sections.append(section)

    nav_links = "".join(
        f'<a class="nav-chip" href="#{slugify(layer)}">{html.escape(layer)}</a>'
        for layer in sorted(grouped_connections, key=natural_key)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)} | {generated_iso}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #e9eef4;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #cbd5e1;
      --link: #1d4ed8;
      --diagram-bg: #ffffff;
      --thead-bg: #e2e8f0;
      --chip-bg: #eef2ff;
      --chip-border: #bfdbfe;
      --toolbar-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
    }}
    body.theme-dark {{
      color-scheme: dark;
      --bg: #0b1220;
      --panel: #111827;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --border: #334155;
      --link: #60a5fa;
      --diagram-bg: #0f172a;
      --thead-bg: #1f2937;
      --chip-bg: #1e293b;
      --chip-border: #334155;
      --toolbar-shadow: 0 1px 2px rgba(2, 6, 23, 0.4);
    }}
    body {{
      margin: 0;
      padding: 14px;
      background: var(--bg);
      color: var(--text);
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      line-height: 1.35;
    }}
    .page {{
      display: grid;
      gap: 12px;
    }}
    .top-panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
    }}
    h1 {{
      margin: 0 0 8px 0;
      font-size: 1.5rem;
    }}
    .meta {{
      color: var(--muted);
      margin-bottom: 0;
    }}
    .layer {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      margin: 0;
      scroll-margin-top: 84px;
    }}
    .layer-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 4px;
      flex-wrap: wrap;
    }}
    .layer h2 {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0;
      font-size: 1.18rem;
    }}
    .layer-note {{
      margin: 0 0 10px 0;
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .layer-stats {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .stat-chip {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid var(--chip-border);
      background: var(--chip-bg);
      color: var(--text);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.74rem;
    }}
    .swatch {{
      display: inline-block;
      width: 12px;
      height: 12px;
      border-radius: 2px;
    }}
    .diagram {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--diagram-bg);
      padding: 10px;
    }}
    .diagram svg {{
      display: block;
    }}
    details {{
      margin-top: 10px;
    }}
    .connection-details > summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--text);
      padding: 4px 0;
    }}
    .connection-table-wrap {{
      margin-top: 8px;
      max-height: 360px;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
    }}
    table {{
      width: 100%;
      min-width: 980px;
      border-collapse: collapse;
      font-size: 0.84rem;
    }}
    th, td {{
      border: 1px solid var(--border);
      padding: 6px 8px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: var(--thead-bg);
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tbody tr:nth-child(even) {{
      background: color-mix(in srgb, var(--panel) 88%, var(--bg) 12%);
    }}
    td:first-child {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.8rem;
      white-space: nowrap;
    }}
    .toolbar {{
      margin: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      position: sticky;
      top: 8px;
      z-index: 10;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: var(--toolbar-shadow);
      padding: 8px 10px;
    }}
    .layer-nav {{
      margin: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .nav-chip {{
      color: var(--link);
      text-decoration: none;
      font-size: 0.8rem;
      border: 1px solid var(--chip-border);
      background: var(--chip-bg);
      border-radius: 999px;
      padding: 2px 8px;
    }}
    .nav-chip:hover {{
      text-decoration: none;
      filter: brightness(0.98);
    }}
    .toolbar-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .theme-toggle-btn {{
      font-size: 0.86rem;
      padding: 4px 9px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      cursor: pointer;
    }}
    .list-toggle-btn {{
      font-size: 0.82rem;
      padding: 4px 8px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      cursor: pointer;
    }}
    @media (max-width: 900px) {{
      .toolbar {{
        top: 6px;
      }}
      .connection-table-wrap {{
        max-height: 280px;
      }}
    }}
  </style>
</head>
<body class="main-prototype-mode">
  <div class="page">
  <header class="top-panel">
    <h1>{html.escape(title)} | {generated_iso}</h1>
    <div class="meta">Generated: {generated_iso} | Source: model/matrix JSON | No Graphviz</div>
  </header>
  <div class="toolbar">
    <nav class="layer-nav">{nav_links}</nav>
    <div class="toolbar-actions">
      <button id="toggleListsBtn" class="list-toggle-btn" type="button">Expand Lists</button>
      <button id="themeToggleBtn" class="theme-toggle-btn" type="button">Dark Mode: Off</button>
    </div>
  </div>
  {"".join(sections)}
  </div>
  <script>
    (function() {{
      const STORAGE_KEY = "studioWiringThemeModeV1";
      const btn = document.getElementById("themeToggleBtn");
      const listBtn = document.getElementById("toggleListsBtn");
      if (!btn) return;

      function getInitialTheme() {{
        try {{
          const stored = String(window.localStorage.getItem(STORAGE_KEY) || "").toLowerCase().trim();
          if (stored === "dark" || stored === "light") return stored;
        }} catch (error) {{
          // Ignore storage issues.
        }}
        return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
      }}

      function applyTheme(mode, persist) {{
        const resolved = mode === "dark" ? "dark" : "light";
        document.body.classList.toggle("theme-dark", resolved === "dark");
        document.body.classList.toggle("theme-light", resolved !== "dark");
        btn.textContent = resolved === "dark" ? "Dark Mode: On" : "Dark Mode: Off";
        btn.title = resolved === "dark" ? "Switch to light mode" : "Switch to dark mode";
        if (persist) {{
          try {{
            window.localStorage.setItem(STORAGE_KEY, resolved);
          }} catch (error) {{
            // Ignore storage issues.
          }}
        }}
      }}

      let mode = getInitialTheme();
      applyTheme(mode, false);

      btn.addEventListener("click", function() {{
        mode = mode === "dark" ? "light" : "dark";
        applyTheme(mode, true);
      }});

      if (listBtn) {{
        listBtn.addEventListener("click", function() {{
          const details = Array.from(document.querySelectorAll("details.connection-details"));
          if (!details.length) return;
          const shouldOpen = details.some((item) => !item.open);
          details.forEach((item) => {{
            item.open = shouldOpen;
          }});
          listBtn.textContent = shouldOpen ? "Collapse Lists" : "Expand Lists";
        }});
      }}
    }})();
  </script>
</body>
</html>
"""


def build_routing_matrix_html(
    title: str,
    model_data: dict[str, object],
    matrix_payload: dict[str, object],
    generated_on: dt.date,
    live_model_url: str | None = None,
    live_matrix_url: str | None = None,
    enable_live_json: bool = True,
    show_patchbays: bool = False,
) -> str:
    generated_iso = generated_on.isoformat()
    safe_title = html.escape(title)
    empty_model_template = build_empty_model_template(
        title=str(model_data.get("title") or title or "Studio Sidecar"),
        families=model_data.get("families") if isinstance(model_data, dict) else None,
    )
    model_json = json.dumps(model_data, ensure_ascii=False).replace("</", "<\\/")
    matrix_json = json.dumps(matrix_payload, ensure_ascii=False).replace("</", "<\\/")
    empty_model_template_json = json.dumps(empty_model_template, ensure_ascii=False).replace("</", "<\\/")
    template = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__ | Wiring Matrix</title>
  <script>
    (function applyInitialShellRoute() {
      const parameters = new URLSearchParams(window.location.search || "");
      const requestedPanel = String(parameters.get("tab") || "matrix").trim().toLowerCase();
      if (parameters.get("embedded") === "1") {
        document.documentElement.classList.add("embedded-mode");
      }
      document.documentElement.dataset.activeShellPanel = requestedPanel;
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --border: #cbd5e1;
      --accent: #0f766e;
      --on: #dcfce7;
      --on-border: #16a34a;
      --warn: #fef3c7;
      --warn-border: #d97706;
      --matrix-source-col-width: 110px;
      --matrix-cell-width: 110px;
      --matrix-row-height: 26px;
      --matrix-first-row-height: 26px;
      --matrix-dest-device-row-height: 30px;
      --matrix-dest-header-height: 172px;
      --matrix-dest-inner-height: 166px;
      --matrix-dest-label-max-height: 138px;
    }
    body {
      margin: 0;
      font-family: Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
      overflow-x: hidden;
    }
    .wrap {
      padding: 16px;
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      min-width: 0;
    }
    h1 {
      margin: 0 0 4px 0;
      font-size: 1.35rem;
    }
    .meta {
      color: var(--muted);
      font-size: 0.9rem;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .matrix-controls-panel {
      padding: 6px;
    }
    .matrix-controls-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, max-content));
      gap: 6px;
      width: fit-content;
      max-width: 100%;
      align-items: start;
      justify-content: start;
    }
    .matrix-control-group {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 5px 6px;
      background: #f8fafc;
      display: flex;
      flex-direction: column;
      gap: 4px;
      width: fit-content;
      max-width: 100%;
    }
    .matrix-control-title {
      font-size: 0.66rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      color: #475569;
      text-transform: uppercase;
    }
    .matrix-control-group label {
      display: flex;
      flex-direction: column;
      gap: 2px;
      font-size: 0.74rem;
      color: #475569;
      line-height: 1.1;
      width: fit-content;
      max-width: 100%;
    }
    .matrix-control-group select,
    .matrix-control-group input[type="text"],
    .matrix-control-group input[type="search"],
    .matrix-control-group input[type="number"] {
      width: clamp(160px, 20vw, 280px);
      font-size: 0.78rem;
      padding: 3px 6px;
      min-height: 28px;
    }
    .matrix-control-group .inline-checkbox {
      flex-direction: row;
      align-items: center;
      gap: 4px;
      color: var(--ink);
      font-size: 0.76rem;
      width: auto;
    }
    .matrix-control-group .controls-row {
      display: grid;
      grid-template-columns: repeat(2, max-content);
      gap: 4px;
      width: fit-content;
      max-width: 100%;
    }
    .matrix-control-group .controls-row button {
      min-height: 27px;
      padding: 3px 6px;
      font-size: 0.78rem;
      line-height: 1.05;
    }
    .matrix-control-group .hint {
      font-size: 0.68rem;
      color: #64748b;
      line-height: 1.1;
    }
    .matrix-control-group .inline-pair {
      flex-direction: row;
      align-items: center;
      gap: 6px;
    }
    .matrix-control-group .inline-pair input[type="number"] {
      width: 64px;
      min-height: 24px;
      padding: 2px 4px;
      font-size: 0.74rem;
    }
    .tab-bar {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .header-tools {
      margin-left: auto;
      display: flex;
      align-items: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }
    .project-tools {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }
    .project-tool {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 150px;
      max-width: 220px;
    }
    .project-tool span {
      font-size: 0.68rem;
      color: var(--muted);
      font-weight: 700;
      line-height: 1;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }
    .project-tool select {
      min-height: 28px;
      font-size: 0.8rem;
      padding: 2px 6px;
      min-width: 150px;
      max-width: 220px;
    }
    .theme-tools {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .theme-toggle-btn {
      min-width: 124px;
      font-weight: 600;
    }
    .config-manager-actions {
      display: flex;
      align-items: center;
      gap: 5px;
      flex-wrap: wrap;
    }
    .config-manager-actions button {
      min-height: 28px;
      padding: 3px 7px;
      font-size: 0.75rem;
    }
    .config-dialog {
      width: min(520px, calc(100vw - 32px));
      max-height: calc(100vh - 32px);
      padding: 0;
      color: var(--ink);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 22px 60px rgba(15, 23, 42, 0.32);
    }
    .config-dialog::backdrop {
      background: rgba(15, 23, 42, 0.58);
    }
    .config-dialog-form {
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 20px;
    }
    .config-dialog-title {
      margin: 0;
      font-size: 1.15rem;
    }
    .config-dialog-description,
    .config-dialog-consequence {
      margin: 0;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.45;
    }
    .config-dialog-field {
      display: flex;
      flex-direction: column;
      gap: 5px;
      font-weight: 700;
      font-size: 0.8rem;
    }
    .config-dialog-field input[type="text"] {
      width: 100%;
      min-height: 36px;
      box-sizing: border-box;
    }
    .config-dialog-path-wrap {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f8fafc;
    }
    .config-dialog-path-label {
      color: var(--muted);
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .config-dialog-path {
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.78rem;
    }
    .config-dialog-error {
      min-height: 1.2em;
      margin: 0;
      color: #b91c1c;
      font-size: 0.78rem;
      font-weight: 700;
    }
    .config-dialog-overwrite {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 10px;
      color: #92400e;
      background: var(--warn);
      border: 1px solid var(--warn-border);
      border-radius: 8px;
      font-size: 0.8rem;
      line-height: 1.35;
    }
    .config-dialog-overwrite.hidden {
      display: none;
    }
    .config-dialog-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    body.theme-dark .config-dialog-path-wrap {
      background: #0b1220;
    }
    body.theme-dark .config-dialog-error {
      color: #fca5a5;
    }
    .tab-btn {
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--ink);
      border-radius: 7px;
      padding: 6px 10px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
    }
    .tab-btn.active {
      background: #dbeafe;
      border-color: #60a5fa;
    }
    .tab-panel.hidden {
      display: none;
    }
    .tab-panel {
      min-width: 0;
    }
    html.embedded-mode body .wrap {
      padding: 0;
      gap: 0;
    }
    html.embedded-mode body .app-title-panel,
    html.embedded-mode body .tab-bar {
      display: none !important;
    }
    html.embedded-mode body .tab-panel {
      margin: 0;
    }
    html.embedded-mode body.connection-overview-mode #panelMatrix > :not(#connectionList) {
      display: none !important;
    }
    html.embedded-mode body.connection-overview-mode #connectionList {
      display: block !important;
      height: calc(100dvh - 2px);
      min-height: 0;
      border-radius: 0;
    }
    .hidden {
      display: none !important;
    }
    label {
      font-size: 0.85rem;
      color: var(--muted);
    }
    .scale-control {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 210px;
    }
    .scale-input-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .scale-input-row input[type="number"] {
      width: 74px;
      min-width: 74px;
    }
    .scale-input-row input[type="range"] {
      width: 140px;
      min-width: 120px;
      accent-color: var(--accent);
    }
    .resize-handle {
      position: absolute;
      z-index: 20;
      background: transparent;
      user-select: none;
      touch-action: none;
    }
    .resize-handle::after {
      content: "";
      position: absolute;
      background: rgba(30, 64, 175, 0.22);
      border-radius: 999px;
      transition: background 120ms ease;
    }
    .resize-handle:hover::after,
    .resize-handle.active::after {
      background: rgba(30, 64, 175, 0.55);
    }
    .resize-handle.col {
      top: 3px;
      right: -5px;
      width: 10px;
      height: calc(100% - 6px);
      cursor: col-resize;
    }
    .resize-handle.col::after {
      top: 2px;
      left: 4px;
      width: 2px;
      height: calc(100% - 4px);
    }
    .resize-handle.row {
      left: 3px;
      bottom: -5px;
      width: calc(100% - 6px);
      height: 10px;
      cursor: row-resize;
    }
    .resize-handle.row.edge-right {
      left: auto;
      right: 3px;
      width: 22px;
    }
    .resize-handle.row-top {
      top: -5px;
      bottom: auto;
    }
    .resize-handle.row::after {
      left: 2px;
      top: 4px;
      width: calc(100% - 4px);
      height: 2px;
    }
    select, input[type="text"], input[type="search"], input[type="number"], button {
      font-size: 0.9rem;
      padding: 4px 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
    }
    button {
      cursor: pointer;
    }
    button:hover {
      border-color: #94a3b8;
    }
    .status {
      font-size: 0.84rem;
      color: var(--muted);
      min-height: 1.2em;
    }
    .status.warn {
      color: #b45309;
    }
    .debug-tools {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 6px;
      padding: 8px;
    }
    .debug-tools-head {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .debug-tools-head button {
      font-size: 0.78rem;
      padding: 4px 8px;
    }
    .debug-report {
      margin: 0;
      font-size: 0.72rem;
      line-height: 1.3;
      max-height: 240px;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #f8fafc;
      padding: 8px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }
    .matrix-wrap {
      overflow-x: auto;
      overflow-y: auto;
      width: 100%;
      max-width: 100%;
      height: var(--matrix-viewport-height, calc(100dvh - 170px));
      min-height: 420px;
      max-height: none;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      touch-action: pan-x pan-y;
      scrollbar-gutter: stable both-edges;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 0;
      position: relative;
      isolation: isolate;
    }
    .matrix-x-scroll {
      overflow-x: auto;
      overflow-y: hidden;
      width: 100%;
      max-width: 100%;
      height: 14px;
      min-height: 14px;
      max-height: 14px;
      padding: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      scrollbar-gutter: stable;
      -webkit-overflow-scrolling: touch;
    }
    .matrix-x-scroll .inner {
      height: 1px;
      min-height: 1px;
    }
    table {
      border-collapse: separate;
      border-spacing: 0;
      width: max-content;
      min-width: max-content;
    }
    .matrix-wrap table {
      table-layout: fixed;
      position: relative;
      z-index: 0;
    }
    th, td {
      border-right: 1px solid #e2e8f0;
      border-bottom: 1px solid #e2e8f0;
      padding: 0;
      min-width: var(--matrix-cell-width);
      height: var(--matrix-row-height);
      text-align: center;
      vertical-align: middle;
      font-size: 0.70rem;
    }
    tbody tr:first-child th,
    tbody tr:first-child td {
      height: var(--matrix-first-row-height);
      min-height: var(--matrix-first-row-height);
      max-height: var(--matrix-first-row-height);
    }
    thead th {
      position: sticky;
      top: 0;
      background: #f1f5f9;
      z-index: 30;
      padding: 2px 3px;
      box-shadow: inset 0 -1px 0 #cbd5e1;
      background-clip: padding-box;
    }
    thead tr.dest-device-row th {
      top: 0;
      z-index: 32;
    }
    thead tr.dest-port-row th {
      top: 0;
      z-index: 32;
    }
    .sticky-left {
      position: sticky;
      left: 0;
      background: #f8fafc;
      z-index: 24;
      width: var(--matrix-source-col-width);
      min-width: var(--matrix-source-col-width);
      max-width: var(--matrix-source-col-width);
      text-align: left;
      padding: 2px 6px;
      box-shadow: inset -1px 0 0 #cbd5e1;
      background-clip: padding-box;
    }
    tbody .sticky-left {
      z-index: 26;
    }
    thead .sticky-left {
      z-index: 36;
      background: #e2e8f0;
    }
    thead .sticky-left.top-left {
      left: 0;
      top: 0;
      z-index: 40;
      background: #dbe5f1;
      box-shadow: inset -1px 0 0 #94a3b8, inset 0 -1px 0 #94a3b8;
    }
    .port-head {
      line-height: 1.05;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .port-head span {
      color: var(--muted);
      font-size: 0.62rem;
      display: block;
      line-height: 1.05;
    }
    .source-head .dev {
      font-size: 0.82rem;
      font-weight: 600;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      display: block;
      max-width: calc(var(--matrix-source-col-width) - 12px);
    }
    .source-head .prt {
      font-size: 0.72rem;
      color: #475569;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      display: flex;
      align-items: center;
      gap: 4px;
      max-width: calc(var(--matrix-source-col-width) - 12px);
    }
    .port-label-text {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .port-head .port-family-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-left: 4px;
      padding: 0 4px;
      min-width: 22px;
      height: 13px;
      border: 1px solid #a16207;
      border-radius: 4px;
      background: #fef3c7;
      color: #854d0e;
      font-size: 0.56rem;
      font-weight: 800;
      line-height: 1;
      letter-spacing: 0.03em;
      vertical-align: middle;
      flex: 0 0 auto;
    }
    .source-head.device-folder .dev {
      font-size: 0.84rem;
      font-weight: 700;
      max-width: calc(var(--matrix-source-col-width) - 12px);
    }
    .source-head.device-folder .prt {
      font-size: 0.70rem;
      font-weight: 600;
      color: #64748b;
      max-width: calc(var(--matrix-source-col-width) - 12px);
    }
    .source-head.top-left .dev {
      font-size: 0.86rem;
      font-weight: 700;
    }
    .source-head.top-left .prt {
      font-size: 0.70rem;
      font-weight: 600;
    }
    .dest-head {
      min-width: var(--matrix-cell-width);
      width: var(--matrix-cell-width);
      max-width: var(--matrix-cell-width);
      vertical-align: bottom;
      padding: 1px;
    }
    .dest-head.dest-device-head {
      height: var(--matrix-dest-device-row-height);
      min-height: var(--matrix-dest-device-row-height);
      max-height: var(--matrix-dest-device-row-height);
      vertical-align: middle;
      background: #e2e8f0;
      padding: 2px 3px;
    }
    .dest-head.dest-port-head {
      height: var(--matrix-dest-header-height);
      min-height: var(--matrix-dest-header-height);
      max-height: var(--matrix-dest-header-height);
      vertical-align: bottom;
      padding: 1px;
    }
    .dest-device-col {
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: 4px;
      min-width: 0;
    }
    .dest-device-name {
      font-size: 0.82rem;
      font-weight: 700;
      color: #334155;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      max-width: none;
    }
    .dest-device-meta {
      font-size: 0.68rem;
      font-weight: 600;
      color: #64748b;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: none;
    }
    .dest-col {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      justify-content: flex-end;
      gap: 2px;
      height: var(--matrix-dest-inner-height);
      min-height: var(--matrix-dest-inner-height);
      padding: 0 2px;
    }
    .dest-labels {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      justify-content: flex-end;
      gap: 2px;
      min-height: 0;
      min-width: 0;
    }
    .dest-controls {
      display: flex;
      justify-content: flex-end;
      gap: 2px;
      min-height: 12px;
    }
    .dest-device {
      font-size: 0.80rem;
      font-weight: 700;
      color: #334155;
      display: block;
      line-height: 1.05;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: calc(var(--matrix-cell-width) - 4px);
    }
    .dest-port {
      font-size: 0.82rem;
      color: #475569;
      display: flex;
      align-items: center;
      gap: 3px;
      line-height: 1.05;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: calc(var(--matrix-cell-width) - 4px);
      max-height: var(--matrix-dest-label-max-height);
      text-align: left;
    }
    body.dest-orientation-vertical .dest-col {
      align-items: center;
      justify-content: flex-end;
      padding: 0 1px;
    }
    body.dest-orientation-vertical .dest-head.dest-device-head .dest-device-col {
      align-items: center;
      justify-content: flex-start;
    }
    body.dest-orientation-vertical .dest-device-name {
      writing-mode: vertical-rl;
      text-orientation: mixed;
      transform: rotate(180deg);
      transform-origin: center center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: clip;
      line-height: 1.0;
      max-height: calc(var(--matrix-dest-device-row-height) - 8px);
    }
    body.dest-orientation-vertical .dest-device-meta {
      display: none;
    }
    body.dest-orientation-vertical .dest-controls {
      justify-content: center;
    }
    body.dest-orientation-vertical .dest-labels {
      flex-direction: row;
      align-items: flex-end;
      justify-content: center;
      gap: 2px;
      min-height: calc(var(--matrix-dest-inner-height) - 14px);
    }
    body.dest-orientation-vertical .dest-device,
    body.dest-orientation-vertical .port-label-text {
      writing-mode: vertical-rl;
      text-orientation: mixed;
      transform: rotate(180deg);
      transform-origin: center center;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: clip;
      max-width: none;
      max-height: none;
      text-align: left;
      line-height: 1.0;
    }
    body.dest-orientation-vertical .dest-port {
      flex-direction: row;
      align-items: flex-end;
      overflow: visible;
      max-width: none;
      max-height: none;
    }
    body.dest-orientation-vertical .dest-port .port-family-badge {
      writing-mode: horizontal-tb;
      transform: none;
    }
    .source-line {
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
    }
    .source-head.collapsed,
    .dest-head.collapsed {
      background: #d7e3f0;
    }
    .group-toggle {
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: #334155;
      border-radius: 4px;
      width: 14px;
      height: 14px;
      min-width: 14px;
      min-height: 14px;
      padding: 0;
      line-height: 12px;
      font-size: 0.58rem;
      cursor: pointer;
    }
    .group-toggle:hover {
      border-color: #94a3b8;
      background: #f8fafc;
    }
    .dest-head .group-toggle {
      display: inline-block;
      margin: 0;
      width: 12px;
      height: 12px;
      min-width: 12px;
      min-height: 12px;
      line-height: 10px;
    }
    td.cell {
      cursor: pointer;
      background: #fff;
      min-width: var(--matrix-cell-width);
      width: var(--matrix-cell-width);
      height: var(--matrix-row-height);
      position: relative;
      z-index: 1;
      transition: background-color 120ms ease, box-shadow 120ms ease;
    }
    td.cell:hover {
      background: #dcfce7;
      box-shadow: inset 0 0 0 1px #22c55e;
    }
    .axis-hover-row {
      outline: 1px solid rgba(37, 99, 235, 0.42);
      outline-offset: -1px;
    }
    .axis-hover-col {
      outline: 1px solid rgba(29, 78, 216, 0.52);
      outline-offset: -1px;
    }
    td.cell.axis-hover-point {
      outline: 2px solid rgba(30, 64, 175, 0.9);
      outline-offset: -2px;
    }
    td.cell.range-anchor {
      background: #dbeafe;
      box-shadow: inset 0 0 0 2px #2563eb;
    }
    td.cell.on {
      background: var(--on);
      box-shadow: inset 0 0 0 1px var(--on-border);
    }
    td.cell.on.override {
      background: var(--warn);
      box-shadow: inset 0 0 0 1px var(--warn-border);
    }
    td.cell.disabled-port {
      cursor: not-allowed;
      background: #eef2f7;
      box-shadow: inset 0 0 0 1px #cbd5e1;
    }
    td.cell.disabled-port:hover {
      background: #e2e8f0;
      box-shadow: inset 0 0 0 1px #94a3b8;
    }
    td.cell.group-blocked {
      cursor: not-allowed;
      color: #475569;
      font-size: 0.60rem;
      background: #fdecc8;
    }
    td.cell.group-blocked.group-on {
      background: #f4d8a0;
      color: #0f172a;
      font-weight: 700;
    }
    tr.device-folder-row th {
      background: #d3dfec;
      border-bottom: 1px solid #b8c7da;
    }
    td.cell.incompatible {
      background: #fee2e2;
      color: #b91c1c;
      cursor: not-allowed;
      box-shadow: inset 0 0 0 1px #fca5a5;
    }
    .split {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 12px;
    }
    .conn-list {
      width: 100%;
      max-width: 100%;
      height: var(--matrix-viewport-height, calc(100dvh - 170px));
      min-height: 420px;
      max-height: none;
      overflow: auto;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .conn-list table {
      width: 100%;
      min-width: 0;
    }
    .conn-list th {
      text-align: left;
      padding: 6px 8px;
      background: #f1f5f9;
    }
    .conn-list td {
      text-align: left;
      padding: 6px 8px;
      height: auto;
      min-width: 0;
      font-size: 0.78rem;
    }
    .matrix-hover-tooltip {
      position: fixed;
      z-index: 50;
      max-width: min(520px, calc(100vw - 20px));
      pointer-events: none;
      background: rgba(255, 255, 255, 0.98);
      color: #0f172a;
      border: 1px solid #94a3b8;
      border-radius: 8px;
      box-shadow: 0 8px 20px rgba(15, 23, 42, 0.22);
      padding: 6px 8px;
      font-size: 0.72rem;
      line-height: 1.25;
      white-space: pre-wrap;
    }
    .matrix-hover-tooltip .title {
      display: block;
      font-weight: 700;
      margin-bottom: 4px;
      color: #0f172a;
    }
    .matrix-hover-tooltip .line {
      display: block;
      color: #1e293b;
    }
    .results-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
    }
    .results-block {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 8px;
    }
    .results-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
      gap: 8px;
    }
    .results-title {
      font-size: 0.84rem;
      font-weight: 700;
      color: #0f172a;
    }
    .results-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .preview-status {
      margin-bottom: 8px;
    }
    .preview-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 10px;
    }
    .preview-card {
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      background: #ffffff;
      padding: 8px;
      display: grid;
      gap: 6px;
    }
    .preview-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.78rem;
    }
    .preview-card-title {
      font-weight: 700;
      color: #0f172a;
    }
    .preview-card-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .preview-open-link {
      color: #2563eb;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.74rem;
    }
    .preview-open-link:hover {
      text-decoration: underline;
    }
    .preview-download-button {
      min-width: 0;
      height: auto;
      padding: 3px 7px;
      font-size: 0.72rem;
    }
    .preview-image {
      width: 100%;
      height: 220px;
      object-fit: contain;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #f8fafc;
    }
    .editor-layout {
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      gap: 12px;
    }
    .device-list-wrap {
      max-height: 68vh;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 4px;
    }
    .visibility-list-wrap {
      max-height: 68vh;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 4px;
    }
    .visibility-list-header,
    .visibility-item {
      display: grid;
      grid-template-columns: 18px minmax(180px, 1fr) repeat(4, minmax(72px, 0.35fr)) auto;
      gap: 8px;
      align-items: center;
      min-width: 680px;
    }
    .visibility-list-header {
      position: sticky;
      top: 0;
      z-index: 2;
      padding: 7px 8px;
      background: #e2e8f0;
      border-radius: 6px;
      color: #334155;
      font-size: 0.7rem;
      font-weight: 700;
      text-align: center;
    }
    .visibility-list-header .visibility-device-heading {
      text-align: left;
    }
    .visibility-item {
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 6px 8px;
      margin: 6px 2px;
      background: #f8fafc;
    }
    .visibility-item.selected {
      border-color: #3b82f6;
      background: #eff6ff;
      box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.18);
    }
    .visibility-item.dragging {
      opacity: 0.55;
    }
    .visibility-item.drag-over-before {
      box-shadow: inset 0 3px 0 #2563eb;
    }
    .visibility-item.drag-over-after {
      box-shadow: inset 0 -3px 0 #2563eb;
    }
    .visibility-item.off {
      opacity: 0.75;
      background: #f8fafc;
    }
    .visibility-device-info {
      min-width: 0;
      cursor: default;
    }
    .visibility-toggle-cell {
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .visibility-toggle-cell input {
      width: 17px;
      height: 17px;
      cursor: pointer;
    }
    .visibility-drag-grip {
      font-size: 0.84rem;
      color: #64748b;
      letter-spacing: 0.5px;
      user-select: none;
      cursor: grab;
      font-weight: 700;
      width: 14px;
      text-align: center;
      line-height: 1;
    }
    .visibility-item.dragging .visibility-drag-grip {
      cursor: grabbing;
    }
    .visibility-name {
      font-size: 0.86rem;
      font-weight: 700;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .visibility-meta {
      font-size: 0.74rem;
      color: #64748b;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .device-item {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px;
      align-items: center;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 6px;
      margin: 6px 2px;
      background: #f8fafc;
    }
    .device-item.active {
      border-color: #60a5fa;
      background: #eff6ff;
    }
    .device-title {
      font-size: 0.86rem;
      font-weight: 700;
      color: #0f172a;
      text-align: left;
    }
    .device-sub {
      font-size: 0.72rem;
      color: #475569;
      text-align: left;
      margin-top: 2px;
    }
    .device-action-btn {
      padding: 4px 7px;
      font-size: 0.78rem;
    }
    .editor-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(120px, 0.6fr) minmax(90px, 0.35fr) auto auto;
      gap: 8px;
      align-items: end;
      margin-bottom: 10px;
    }
    .rack-editor-controls {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr)) auto auto;
      gap: 8px;
      align-items: end;
    }
    .rack-editor-controls label {
      display: flex;
      flex-direction: column;
      gap: 3px;
    }
    .rack-capability-field {
      flex-direction: row !important;
      align-items: center;
      align-self: end;
      gap: 6px !important;
      min-height: 30px;
      white-space: nowrap;
    }
    .rack-capability-field input[type="checkbox"] {
      width: 16px;
      height: 16px;
      min-height: 0;
      margin: 0;
    }
    .rack-editor-status {
      margin-top: 8px;
    }
    .rack-editor-lists {
      display: block;
    }
    .rack-device-list {
      margin: 6px 0 0;
      padding-left: 22px;
      max-height: 180px;
      overflow: auto;
    }
    .rack-device-list button {
      width: 100%;
      border: 0;
      background: transparent;
      padding: 4px 2px;
      text-align: left;
      color: inherit;
    }
    .rack-device-list button[draggable="true"],
    .rack-device-block[draggable="true"] {
      cursor: grab;
    }
    .rack-device-list button[draggable="true"]:active,
    .rack-device-block[draggable="true"]:active {
      cursor: grabbing;
    }
    .rack-device-list button:hover,
    .rack-device-list button:focus-visible {
      color: var(--accent);
      text-decoration: underline;
    }
    .rack-drag-affordance {
      display: inline-block;
      margin-right: 5px;
      color: var(--muted);
      font-weight: 700;
      letter-spacing: -0.12em;
    }
    .racks-layout {
      display: grid;
      grid-template-columns: repeat(4, minmax(180px, 1fr));
      gap: 12px;
    }
    .rack-card {
      min-width: 0;
    }
    .rack-card h3 {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin: 0 0 6px;
      font-size: 0.95rem;
    }
    .rack-grid {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      grid-template-rows: repeat(16, minmax(30px, 1fr));
      position: relative;
      min-height: 480px;
      border: 2px solid #475569;
      border-radius: 5px;
      overflow: hidden;
      background: #f8fafc;
    }
    .rack-grid.rack-drop-target {
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.25);
    }
    .rack-grid.rack-drop-valid {
      border-color: #16a34a;
      box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.28);
    }
    .rack-grid.rack-drop-invalid {
      border-color: #dc2626;
      box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.24);
    }
    .rack-unit-label,
    .rack-unit-slot {
      border-bottom: 1px solid #cbd5e1;
      box-sizing: border-box;
      min-height: 30px;
    }
    .rack-unit-label {
      grid-column: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      border-right: 1px solid #94a3b8;
      color: var(--muted);
      background: #e2e8f0;
      font: 700 0.7rem ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .rack-unit-slot {
      grid-column: 2;
      background: #fff;
    }
    .rack-device-block {
      grid-column: 2;
      z-index: 2;
      margin: 2px 3px;
      border: 1px solid #0f766e;
      border-radius: 5px;
      background: #ccfbf1;
      color: #134e4a;
      padding: 4px 6px;
      overflow: hidden;
      text-align: left;
      font-size: 0.74rem;
      font-weight: 700;
      line-height: 1.2;
    }
    .rack-device-block.active {
      outline: 3px solid #2563eb;
      outline-offset: -2px;
    }
    [data-rack-drag-device].rack-dragging {
      opacity: 0.48;
    }
    .rack-device-block span {
      display: block;
      font-size: 0.66rem;
      font-weight: 500;
    }
    .rack-device-block .rack-drag-affordance {
      display: inline-block;
      font-size: 0.78rem;
      font-weight: 700;
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .editor-card {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 8px;
    }
    .mini-tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 10px;
    }
    .matrix-subtabs {
      margin-bottom: 8px;
    }
    .mini-tab-btn {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #fff;
      padding: 4px 8px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }
    .mini-tab-btn.active {
      background: #dcfce7;
      border-color: #4ade80;
    }
    .port-add-grid {
      display: grid;
      grid-template-columns: 170px 90px 170px 120px 120px auto;
      gap: 8px;
      align-items: end;
      margin-bottom: 8px;
    }
    .port-table-wrap {
      max-height: 52vh;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .port-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
    }
    .port-table th,
    .port-table td {
      border-bottom: 1px solid #e2e8f0;
      border-right: 1px solid #e2e8f0;
      text-align: left;
      padding: 6px;
      font-size: 0.78rem;
      height: auto;
      min-width: 0;
    }
    .port-table th:last-child,
    .port-table td:last-child {
      border-right: 0;
    }
    .muted-note {
      color: #64748b;
      font-size: 0.76rem;
    }
    .muted-note.warn {
      color: #b45309;
    }
    body.theme-dark {
      color-scheme: dark;
      --bg: #0b1220;
      --panel: #111827;
      --ink: #e5e7eb;
      --muted: #94a3b8;
      --border: #334155;
      --accent: #38bdf8;
      --on: #052e1d;
      --on-border: #22c55e;
      --warn: #422e0c;
      --warn-border: #f59e0b;
    }
    body.theme-dark .tab-btn,
    body.theme-dark .mini-tab-btn,
    body.theme-dark .group-toggle,
    body.theme-dark select,
    body.theme-dark input[type="text"],
    body.theme-dark input[type="search"],
    body.theme-dark input[type="number"],
    body.theme-dark button {
      background: #0f172a;
      border-color: #334155;
      color: #e5e7eb;
    }
    body.theme-dark button:hover,
    body.theme-dark .tab-btn:hover,
    body.theme-dark .mini-tab-btn:hover,
    body.theme-dark .group-toggle:hover {
      border-color: #64748b;
      background: #172033;
    }
    body.theme-dark .tab-btn.active {
      background: #1e3a8a;
      border-color: #3b82f6;
      color: #eff6ff;
    }
    body.theme-dark .mini-tab-btn.active {
      background: #14532d;
      border-color: #22c55e;
      color: #dcfce7;
    }
    body.theme-dark .matrix-wrap,
    body.theme-dark .matrix-x-scroll,
    body.theme-dark .device-list-wrap,
    body.theme-dark .visibility-list-wrap,
    body.theme-dark .editor-card,
    body.theme-dark .results-block,
    body.theme-dark .rack-grid,
    body.theme-dark .conn-list {
      background: #0f172a;
      border-color: #334155;
    }
    body.theme-dark .matrix-control-group {
      background: #172033;
      border-color: #334155;
    }
    body.theme-dark .matrix-control-title,
    body.theme-dark .matrix-control-group label {
      color: #cbd5e1;
    }
    body.theme-dark .matrix-control-group .hint {
      color: #94a3b8;
    }
    body.theme-dark th,
    body.theme-dark td {
      border-right-color: #273449;
      border-bottom-color: #273449;
    }
    body.theme-dark thead th {
      background: #1f2937;
      box-shadow: inset 0 -1px 0 #334155;
    }
    body.theme-dark .dest-head.dest-device-head {
      background: #243244;
    }
    body.theme-dark .sticky-left {
      background: #111827;
      box-shadow: inset -1px 0 0 #334155;
    }
    body.theme-dark thead .sticky-left {
      background: #1f2937;
    }
    body.theme-dark thead .sticky-left.top-left {
      background: #243244;
      box-shadow: inset -1px 0 0 #475569, inset 0 -1px 0 #475569;
    }
    body.theme-dark td.cell {
      background: #0f172a;
    }
    body.theme-dark td.cell:hover {
      background: #0f2f24;
      box-shadow: inset 0 0 0 1px #22c55e;
    }
    body.theme-dark .axis-hover-row {
      outline: 1px solid rgba(96, 165, 250, 0.42);
    }
    body.theme-dark .axis-hover-col {
      outline: 1px solid rgba(147, 197, 253, 0.52);
    }
    body.theme-dark td.cell.axis-hover-point {
      outline: 2px solid rgba(59, 130, 246, 0.95);
    }
    body.theme-dark td.cell.range-anchor {
      background: #1e3a8a;
      box-shadow: inset 0 0 0 2px #93c5fd;
    }
    body.theme-dark .source-head .dev,
    body.theme-dark .results-title,
    body.theme-dark .preview-card-title,
    body.theme-dark .device-title {
      color: #e5e7eb;
    }
    body.theme-dark .source-head .prt,
    body.theme-dark .dest-port,
    body.theme-dark .device-sub,
    body.theme-dark .muted-note {
      color: #94a3b8;
    }
    body.theme-dark .port-family-badge {
      border-color: #d97706;
      background: #422006;
      color: #fde68a;
    }
    body.theme-dark .dest-device {
      color: #cbd5e1;
    }
    body.theme-dark .dest-device-name {
      color: #e2e8f0;
    }
    body.theme-dark .dest-device-meta {
      color: #94a3b8;
    }
    body.theme-dark .source-head.collapsed,
    body.theme-dark .dest-head.collapsed {
      background: #1b2a3d;
    }
    body.theme-dark td.cell.group-blocked {
      background: #3f2f16;
      color: #d6c7a5;
    }
    body.theme-dark td.cell.group-blocked.group-on {
      background: #5a4321;
      color: #f3e8c8;
    }
    body.theme-dark tr.device-folder-row th {
      background: #223249;
      border-bottom: 1px solid #415676;
    }
    body.theme-dark td.cell.incompatible {
      background: #4a1f24;
      color: #fca5a5;
      box-shadow: inset 0 0 0 1px #7f1d1d;
    }
    body.theme-dark td.cell.disabled-port {
      background: #1e293b;
      box-shadow: inset 0 0 0 1px #334155;
    }
    body.theme-dark td.cell.disabled-port:hover {
      background: #273449;
      box-shadow: inset 0 0 0 1px #64748b;
    }
    body.theme-dark .conn-list th {
      background: #1f2937;
    }
    body.theme-dark .matrix-hover-tooltip {
      background: rgba(15, 23, 42, 0.98);
      color: #e5e7eb;
      border-color: #475569;
      box-shadow: 0 10px 24px rgba(2, 6, 23, 0.55);
    }
    body.theme-dark .matrix-hover-tooltip .title {
      color: #f8fafc;
    }
    body.theme-dark .matrix-hover-tooltip .line {
      color: #cbd5e1;
    }
    body.theme-dark .device-item {
      border-color: #334155;
      background: #0f172a;
    }
    body.theme-dark .visibility-item {
      border-color: #334155;
      background: #0f172a;
    }
    body.theme-dark .visibility-list-header {
      background: #1e293b;
      color: #cbd5e1;
    }
    body.theme-dark .visibility-item.selected {
      border-color: #60a5fa;
      background: #172554;
    }
    body.theme-dark .visibility-item.drag-over-before {
      box-shadow: inset 0 3px 0 #60a5fa;
    }
    body.theme-dark .visibility-item.drag-over-after {
      box-shadow: inset 0 -3px 0 #60a5fa;
    }
    body.theme-dark .visibility-item.off {
      background: #111827;
      opacity: 0.8;
    }
    body.theme-dark .visibility-name {
      color: #e5e7eb;
    }
    body.theme-dark .visibility-meta {
      color: #94a3b8;
    }
    body.theme-dark .visibility-drag-grip {
      color: #94a3b8;
    }
    body.theme-dark .device-item.active {
      border-color: #60a5fa;
      background: #172554;
    }
    body.theme-dark .rack-unit-label {
      background: #1e293b;
      border-color: #475569;
    }
    body.theme-dark .rack-unit-slot {
      background: #0f172a;
      border-color: #334155;
    }
    body.theme-dark .rack-device-block {
      background: #134e4a;
      border-color: #2dd4bf;
      color: #ccfbf1;
    }
    body.theme-dark .preview-card {
      border-color: #334155;
      background: #0b1220;
    }
    body.theme-dark .preview-image {
      border-color: #334155;
      background: #111827;
    }
    body.theme-dark .preview-open-link {
      color: #93c5fd;
    }
    body.theme-dark .preview-download-button {
      border-color: #475569;
      background: #172033;
      color: #dbeafe;
    }
    body.theme-dark .resize-handle::after {
      background: rgba(147, 197, 253, 0.28);
    }
    body.theme-dark .resize-handle:hover::after,
    body.theme-dark .resize-handle.active::after {
      background: rgba(147, 197, 253, 0.62);
    }
    body.theme-dark .status.warn {
      color: #f59e0b;
    }
    body.theme-dark .debug-report {
      background: #0b1220;
      border-color: #334155;
      color: #cbd5e1;
    }
    body.theme-dark .muted-note.warn {
      color: #f59e0b;
    }
    @media (max-width: 1200px) {
      .split {
        grid-template-columns: 1fr;
      }
      .matrix-controls-grid {
        grid-template-columns: repeat(2, minmax(220px, max-content));
      }
      .editor-layout {
        grid-template-columns: 1fr;
      }
      .racks-layout {
        grid-template-columns: repeat(2, minmax(180px, 1fr));
      }
      .rack-editor-controls {
        grid-template-columns: repeat(3, minmax(120px, 1fr));
      }
      .port-add-grid {
        grid-template-columns: 1fr 1fr;
      }
    }
    @media (max-width: 780px) {
      .header-tools {
        width: 100%;
        margin-left: 0;
      }
      .project-tools {
        width: 100%;
      }
      .project-tool,
      .project-tool select {
        min-width: 0;
        max-width: none;
        width: 100%;
      }
      .matrix-controls-grid {
        grid-template-columns: minmax(220px, 1fr);
        width: 100%;
      }
      .matrix-control-group,
      .matrix-control-group label,
      .matrix-control-group .controls-row {
        width: 100%;
      }
      .matrix-control-group select,
      .matrix-control-group input[type="text"],
      .matrix-control-group input[type="search"],
      .matrix-control-group input[type="number"] {
        width: 100%;
      }
      .racks-layout,
      .rack-editor-lists,
      .rack-editor-controls,
      .editor-head {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div id="appTitlePanel" class="panel app-title-panel">
      <h1>__TITLE__ | Wiring Matrix</h1>
      <div class="meta">Generated: __DATE__ | Click a cell to connect/disconnect.</div>
    </div>
    <div id="internalAppTabBar" class="panel tab-bar">
      <button id="mainTabMatrix" class="tab-btn active" type="button">Wiring Matrix</button>
      <button id="mainTabDevices" class="tab-btn" type="button">Devices & Ports</button>
      <button id="mainTabRack" class="tab-btn" type="button">Rack Editor</button>
      <button id="mainTabVisibility" class="tab-btn" type="button">Visibility</button>
      <button id="mainTabVisuals" class="tab-btn" type="button">Visuals</button>
      <div class="header-tools">
        <div id="projectTools" class="project-tools">
          <label class="project-tool"><span>Project</span>
            <select id="projectSelect"></select>
          </label>
          <label class="project-tool"><span>Device Config</span>
            <select id="deviceConfigSelect"></select>
          </label>
          <label class="project-tool"><span>Patch Config</span>
            <select id="patchConfigSelect"></select>
          </label>
          <div class="config-manager-actions" aria-label="Configuration manager">
            <button id="createProjectBtn" type="button">New Project</button>
            <button id="createDeviceConfigBtn" type="button">New Device Config</button>
            <button id="createPatchConfigBtn" type="button">New Patch Config</button>
          </div>
        </div>
        <div class="theme-tools">
          <button id="themeToggleBtn" class="theme-toggle-btn" type="button">Dark Mode: Off</button>
        </div>
      </div>
    </div>

    <div id="panelMatrix" class="tab-panel hidden">
      <div class="panel controls matrix-controls-panel">
        <div class="matrix-controls-grid">
          <div class="matrix-control-group">
            <div class="matrix-control-title">Filtering</div>
            <label>Family
              <select id="familySelect"></select>
            </label>
            <label>Filter Sources
              <input id="sourceFilterInput" type="search" placeholder="device or port..." />
            </label>
            <label>Filter Destinations
              <input id="destFilterInput" type="search" placeholder="device or port..." />
            </label>
          </div>
          <div class="matrix-control-group">
            <div class="matrix-control-title">Routing</div>
            <label>Patch Mode
              <select id="patchModeSelect">
                <option value="single">Single</option>
                <option value="paint">Paint</option>
                <option value="range">Range</option>
                <option value="stereo">Stereo Pair</option>
                <option value="multi">Multi Pair</option>
              </select>
            </label>
            <label class="inline-pair">Pair Count
              <input id="pairCountInput" type="number" min="2" max="64" step="1" value="8" />
            </label>
            <label>Type Tag (optional)
              <input id="typeTagInput" type="text" placeholder="ST, MC8, CAT6..." />
            </label>
            <label class="inline-checkbox">
              <input id="overrideToggle" type="checkbox" />
              Override 1:1 for new links
            </label>
            <div id="patchModeHint" class="hint">Single: click one crosspoint.</div>
          </div>
          <div class="matrix-control-group">
            <div class="matrix-control-title">View</div>
            <div class="controls-row">
              <button id="collapseGroupsBtn" type="button">Collapse Groups</button>
              <button id="expandGroupsBtn" type="button">Expand Groups</button>
              <button id="collapseDevicesBtn" type="button">Collapse Devices</button>
              <button id="expandDevicesBtn" type="button">Expand Devices</button>
            </div>
            <div class="controls-row">
              <button id="resetScaleBtn" type="button">Reset Scale</button>
              <button id="resetBtn" type="button">Reset View</button>
            </div>
          </div>
          <div class="matrix-control-group">
            <div class="matrix-control-title">Data</div>
            <div class="controls-row">
              <button id="importJsonBtn" type="button">Import JSON</button>
              <button id="exportJsonBtn" type="button">Export JSON</button>
              <button id="loadEmptyTemplateBtn" type="button">Load Empty Template</button>
            </div>
            <div class="controls-row">
              <button id="saveDeviceConfigAsBtn" type="button">Save Device Config As</button>
              <button id="savePatchConfigAsBtn" type="button">Save Patch Config As</button>
            </div>
          </div>
        </div>
        <input id="importModelFile" type="file" accept="application/json" style="display:none;" />
        <input id="importConnectionsFile" type="file" accept="application/json" style="display:none;" />
      </div>
      <div id="status" class="status panel"></div>
      <div class="panel debug-tools">
        <div class="debug-tools-head">
          <button id="copyDebugReportBtn" type="button">Copy Debug Report</button>
          <button id="toggleDebugPanelBtn" type="button">Show Debug Details</button>
        </div>
        <pre id="debugReportPanel" class="debug-report hidden"></pre>
      </div>
      <div class="mini-tabs matrix-subtabs">
        <button id="matrixSubTabPatch" class="mini-tab-btn active" type="button">Patch Matrix</button>
        <button id="matrixSubTabConnections" class="mini-tab-btn" type="button">Connections Table</button>
      </div>
      <div class="panel matrix-x-scroll" id="matrixXScroll"><div id="matrixXScrollInner" class="inner"></div></div>
      <div class="panel matrix-wrap" id="matrixContainer"></div>
      <div class="panel conn-list hidden" id="connectionList"></div>
    </div>
    <div id="matrixHoverTooltip" class="matrix-hover-tooltip hidden"></div>

    <div id="panelDevices" class="tab-panel hidden">
      <div class="panel controls">
        <label>Device Name
          <input id="newDeviceName" type="text" placeholder="New device name..." />
        </label>
        <label>Device Type
          <input id="newDeviceType" type="text" placeholder="Interface / Converter" />
        </label>
        <label>Location
          <select id="newDeviceLocation">
            <option value="Desk">Desk</option>
            <option value="Rack">Rack</option>
          </select>
        </label>
        <label>Height (U/HE)
          <input id="newDeviceRackUnits" type="number" min="1" max="16" step="1" value="1" />
        </label>
        <label class="rack-capability-field">
          <input id="newDeviceRackMountable" type="checkbox" /> Rack mountable
        </label>
        <button id="addDeviceBtn" type="button">Add Device</button>
        <button id="downloadModelBtn" type="button">Download Model JSON</button>
      </div>
      <div class="editor-layout">
        <div class="panel">
          <div class="device-list-wrap" id="deviceListPanel"></div>
        </div>
        <div class="panel">
        <div class="editor-head">
            <label>Selected Device Name
              <input id="deviceNameInput" type="text" placeholder="Device name" />
            </label>
            <label>Device Type
              <input id="deviceTypeInput" type="text" placeholder="Device type" />
            </label>
            <label>Location
              <select id="deviceLocationInput">
                <option value="Desk">Desk</option>
                <option value="Rack">Rack</option>
              </select>
            </label>
            <label>Height (U/HE)
              <input id="deviceRackUnitsInput" type="number" min="1" max="16" step="1" value="1" />
            </label>
            <label class="rack-capability-field">
              <input id="deviceRackMountableInput" type="checkbox" /> Rack mountable
            </label>
            <button id="saveDeviceMetaBtn" type="button">Save Device</button>
          </div>
          <div class="mini-tabs">
            <button id="portTabInputs" class="mini-tab-btn active" type="button">Inputs</button>
            <button id="portTabOutputs" class="mini-tab-btn" type="button">Outputs</button>
          </div>
          <div id="deviceEditorPanel"></div>
        </div>
      </div>
    </div>

    <div id="panelRack" class="tab-panel hidden">
      <section class="panel" aria-labelledby="rackEditorHeading">
        <h2 id="rackEditorHeading">Rack Editor</h2>
        <p class="muted-note">Place Rack devices in one of four 16U racks. Start U is the lowest unit occupied by the device.</p>
        <div class="rack-editor-controls">
          <label>Device
            <select id="rackEditorDeviceSelect"></select>
          </label>
          <label>Location
            <select id="rackEditorLocationSelect">
              <option value="Desk">Desk</option>
              <option value="Rack">Rack</option>
            </select>
          </label>
          <label>Height (U/HE)
            <input id="rackEditorUnitsInput" type="number" min="1" max="16" step="1" value="1" />
          </label>
          <label>Rack
            <select id="rackEditorRackSelect">
              <option value="1">Rack 1</option>
              <option value="2">Rack 2</option>
              <option value="3">Rack 3</option>
              <option value="4">Rack 4</option>
            </select>
          </label>
          <label>Start U (lowest)
            <select id="rackEditorStartUSelect"></select>
          </label>
          <button id="applyRackPlacementBtn" type="button">Apply Placement</button>
          <button id="removeRackPlacementBtn" type="button">Remove from Rack</button>
        </div>
        <div id="rackEditorStatus" class="status rack-editor-status" role="status" aria-live="polite"></div>
      </section>
      <div class="rack-editor-lists">
        <section class="panel" aria-labelledby="rackUnplacedHeading">
          <h3 id="rackUnplacedHeading">Unplaced Rack Devices</h3>
          <ul id="rackUnplacedList" class="rack-device-list"></ul>
        </section>
      </div>
      <div id="rackEditorRacks" class="racks-layout" aria-label="Four 16U equipment racks"></div>
    </div>

    <div id="panelVisibility" class="tab-panel hidden">
      <div class="panel controls">
        <button id="showAllDevicesBtn" type="button">Show Everywhere</button>
        <button id="hideAllDevicesBtn" type="button">Hide Everywhere</button>
        <button id="invertVisibleDevicesBtn" type="button">Invert All Visibility</button>
        <span class="muted-note">Option-click a checkbox for all devices. Shift-click selects a range; Command-click adds or removes devices. A toggle on a selected device applies to the group.</span>
      </div>
      <div id="visibilitySummary" class="status panel"></div>
      <div class="panel">
        <div class="visibility-list-wrap" id="visibilityListPanel"></div>
      </div>
    </div>

    <div id="panelVisuals" class="tab-panel hidden">
      <div class="panel">
        <div class="results-grid">
          <div class="results-block">
            <div class="results-head">
              <div class="results-title">Finished Results: Visual Representation (live)</div>
              <div class="results-actions">
                <button id="regeneratePreviewsBtn" type="button">Regenerate Visuals</button>
                <button id="downloadSvgsBtn" type="button">Save SVG Folder</button>
                <button id="openRouteDebugBtn" type="button">Open Route Debug JSON</button>
              </div>
            </div>
            <div id="previewStatus" class="muted-note preview-status"></div>
            <div class="preview-grid">
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">Audio Analog</span>
                  <span class="preview-card-actions"><a id="previewLinkAudioAnalog" class="preview-open-link" href="../svgs/audio-analog.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="audioAnalog">Download</button></span>
                </div>
                <object id="previewAudioAnalog" class="preview-image" data="../svgs/audio-analog.svg" type="image/svg+xml" aria-label="Audio Analog preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">Computer/Data</span>
                  <span class="preview-card-actions"><a id="previewLinkComputerData" class="preview-open-link" href="../svgs/computer-data.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="computerData">Download</button></span>
                </div>
                <object id="previewComputerData" class="preview-image" data="../svgs/computer-data.svg" type="image/svg+xml" aria-label="Computer/Data preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">Digital Audio</span>
                  <span class="preview-card-actions"><a id="previewLinkDigitalAudio" class="preview-open-link" href="../svgs/digital-audio.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="digitalAudio">Download</button></span>
                </div>
                <object id="previewDigitalAudio" class="preview-image" data="../svgs/digital-audio.svg" type="image/svg+xml" aria-label="Digital Audio preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">All Audio</span>
                  <span class="preview-card-actions"><a id="previewLinkAllAudio" class="preview-open-link" href="../svgs/all-audio.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="allAudio">Download</button></span>
                </div>
                <object id="previewAllAudio" class="preview-image" data="../svgs/all-audio.svg" type="image/svg+xml" aria-label="All Audio preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">Network</span>
                  <span class="preview-card-actions"><a id="previewLinkNetwork" class="preview-open-link" href="../svgs/network.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="network">Download</button></span>
                </div>
                <object id="previewNetwork" class="preview-image" data="../svgs/network.svg" type="image/svg+xml" aria-label="Network preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">Power</span>
                  <span class="preview-card-actions"><a id="previewLinkPower" class="preview-open-link" href="../svgs/power.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="power">Download</button></span>
                </div>
                <object id="previewPower" class="preview-image" data="../svgs/power.svg" type="image/svg+xml" aria-label="Power preview"></object>
              </div>
              <div class="preview-card">
                <div class="preview-card-head">
                  <span class="preview-card-title">All Connections</span>
                  <span class="preview-card-actions"><a id="previewLinkAllConnections" class="preview-open-link" href="../svgs/all-connections.svg" target="_blank" rel="noopener noreferrer">Open</a><button class="preview-download-button" type="button" data-preview-download="allConnections">Download</button></span>
                </div>
                <object id="previewAllConnections" class="preview-image" data="../svgs/all-connections.svg" type="image/svg+xml" aria-label="All Connections preview"></object>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <dialog id="configDialog" class="config-dialog" aria-labelledby="configDialogTitle" aria-describedby="configDialogDescription">
      <form id="configDialogForm" class="config-dialog-form" method="dialog" novalidate>
        <h2 id="configDialogTitle" class="config-dialog-title">Configuration</h2>
        <p id="configDialogDescription" class="config-dialog-description"></p>
        <label class="config-dialog-field"><span id="configDialogInputLabel">Name</span>
          <input id="configDialogInput" type="text" autocomplete="off" aria-describedby="configDialogError configDialogTarget" />
        </label>
        <div class="config-dialog-path-wrap">
          <span class="config-dialog-path-label">Target path</span>
          <output id="configDialogTarget" class="config-dialog-path"></output>
        </div>
        <p id="configDialogConsequence" class="config-dialog-consequence"></p>
        <label id="configDialogOverwrite" class="config-dialog-overwrite hidden">
          <input id="configDialogOverwriteCheck" type="checkbox" />
          <span id="configDialogOverwriteText">Replace the existing file at this exact path.</span>
        </label>
        <p id="configDialogError" class="config-dialog-error" role="alert" aria-live="polite"></p>
        <div class="config-dialog-actions">
          <button id="configDialogCancel" type="button">Cancel</button>
          <button id="configDialogSubmit" type="submit">Continue</button>
        </div>
      </form>
    </dialog>
  </div>
  <script>
    const EMBEDDED_MODEL = __MODEL_JSON__;
    const EMPTY_MODEL_TEMPLATE = __EMPTY_MODEL_TEMPLATE_JSON__;
    const EMBEDDED_MATRIX = __MATRIX_JSON__;
    const FAMILY_ALL = "ALL";
    const FAMILY_ORDER = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER"];

    const familySelect = document.getElementById("familySelect");
    const sourceFilterInput = document.getElementById("sourceFilterInput");
    const destFilterInput = document.getElementById("destFilterInput");
    const patchModeSelect = document.getElementById("patchModeSelect");
    const pairCountInput = document.getElementById("pairCountInput");
    const patchModeHint = document.getElementById("patchModeHint");
    const typeTagInput = document.getElementById("typeTagInput");
    const importJsonBtn = document.getElementById("importJsonBtn");
    const exportJsonBtn = document.getElementById("exportJsonBtn");
    const loadEmptyTemplateBtn = document.getElementById("loadEmptyTemplateBtn");
    const saveDeviceConfigAsBtn = document.getElementById("saveDeviceConfigAsBtn");
    const savePatchConfigAsBtn = document.getElementById("savePatchConfigAsBtn");
    const overrideToggle = document.getElementById("overrideToggle");
    const collapseGroupsBtn = document.getElementById("collapseGroupsBtn");
    const expandGroupsBtn = document.getElementById("expandGroupsBtn");
    const collapseDevicesBtn = document.getElementById("collapseDevicesBtn");
    const expandDevicesBtn = document.getElementById("expandDevicesBtn");
    const matrixXScroll = document.getElementById("matrixXScroll");
    const matrixXScrollInner = document.getElementById("matrixXScrollInner");
    const matrixContainer = document.getElementById("matrixContainer");
    const matrixHoverTooltip = document.getElementById("matrixHoverTooltip");
    const connectionList = document.getElementById("connectionList");
    const statusEl = document.getElementById("status");
    const copyDebugReportBtn = document.getElementById("copyDebugReportBtn");
    const toggleDebugPanelBtn = document.getElementById("toggleDebugPanelBtn");
    const debugReportPanel = document.getElementById("debugReportPanel");
    const importModelFile = document.getElementById("importModelFile");
    const importConnectionsFile = document.getElementById("importConnectionsFile");
    const mainTabMatrix = document.getElementById("mainTabMatrix");
    const mainTabDevices = document.getElementById("mainTabDevices");
    const mainTabRack = document.getElementById("mainTabRack");
    const mainTabVisibility = document.getElementById("mainTabVisibility");
    const mainTabVisuals = document.getElementById("mainTabVisuals");
    const matrixSubTabPatch = document.getElementById("matrixSubTabPatch");
    const matrixSubTabConnections = document.getElementById("matrixSubTabConnections");
    const panelMatrix = document.getElementById("panelMatrix");
    const panelDevices = document.getElementById("panelDevices");
    const panelRack = document.getElementById("panelRack");
    const panelVisibility = document.getElementById("panelVisibility");
    const panelVisuals = document.getElementById("panelVisuals");
    const newDeviceName = document.getElementById("newDeviceName");
    const newDeviceType = document.getElementById("newDeviceType");
    const newDeviceLocation = document.getElementById("newDeviceLocation");
    const newDeviceRackUnits = document.getElementById("newDeviceRackUnits");
    const newDeviceRackMountable = document.getElementById("newDeviceRackMountable");
    const addDeviceBtn = document.getElementById("addDeviceBtn");
    const downloadModelBtn = document.getElementById("downloadModelBtn");
    const deviceListPanel = document.getElementById("deviceListPanel");
    const deviceEditorPanel = document.getElementById("deviceEditorPanel");
    const deviceNameInput = document.getElementById("deviceNameInput");
    const deviceTypeInput = document.getElementById("deviceTypeInput");
    const deviceLocationInput = document.getElementById("deviceLocationInput");
    const deviceRackUnitsInput = document.getElementById("deviceRackUnitsInput");
    const deviceRackMountableInput = document.getElementById("deviceRackMountableInput");
    const saveDeviceMetaBtn = document.getElementById("saveDeviceMetaBtn");
    const portTabInputs = document.getElementById("portTabInputs");
    const portTabOutputs = document.getElementById("portTabOutputs");
    const visibilitySummary = document.getElementById("visibilitySummary");
    const visibilityListPanel = document.getElementById("visibilityListPanel");
    const showAllDevicesBtn = document.getElementById("showAllDevicesBtn");
    const hideAllDevicesBtn = document.getElementById("hideAllDevicesBtn");
    const invertVisibleDevicesBtn = document.getElementById("invertVisibleDevicesBtn");
    const rackEditorDeviceSelect = document.getElementById("rackEditorDeviceSelect");
    const rackEditorLocationSelect = document.getElementById("rackEditorLocationSelect");
    const rackEditorUnitsInput = document.getElementById("rackEditorUnitsInput");
    const rackEditorRackSelect = document.getElementById("rackEditorRackSelect");
    const rackEditorStartUSelect = document.getElementById("rackEditorStartUSelect");
    const applyRackPlacementBtn = document.getElementById("applyRackPlacementBtn");
    const removeRackPlacementBtn = document.getElementById("removeRackPlacementBtn");
    const rackEditorStatus = document.getElementById("rackEditorStatus");
    const rackUnplacedList = document.getElementById("rackUnplacedList");
    const rackEditorRacks = document.getElementById("rackEditorRacks");
    const previewStatus = document.getElementById("previewStatus");
    const regeneratePreviewsBtn = document.getElementById("regeneratePreviewsBtn");
    const downloadSvgsBtn = document.getElementById("downloadSvgsBtn");
    const openRouteDebugBtn = document.getElementById("openRouteDebugBtn");
    const previewAudioAnalog = document.getElementById("previewAudioAnalog");
    const previewComputerData = document.getElementById("previewComputerData");
    const previewDigitalAudio = document.getElementById("previewDigitalAudio");
    const previewAllAudio = document.getElementById("previewAllAudio");
    const previewNetwork = document.getElementById("previewNetwork");
    const previewPower = document.getElementById("previewPower");
    const previewAllConnections = document.getElementById("previewAllConnections");
    const previewLinkAudioAnalog = document.getElementById("previewLinkAudioAnalog");
    const previewLinkComputerData = document.getElementById("previewLinkComputerData");
    const previewLinkDigitalAudio = document.getElementById("previewLinkDigitalAudio");
    const previewLinkAllAudio = document.getElementById("previewLinkAllAudio");
    const previewLinkNetwork = document.getElementById("previewLinkNetwork");
    const previewLinkPower = document.getElementById("previewLinkPower");
    const previewLinkAllConnections = document.getElementById("previewLinkAllConnections");
    const resetScaleBtn = document.getElementById("resetScaleBtn");
    const themeToggleBtn = document.getElementById("themeToggleBtn");
    const projectTools = document.getElementById("projectTools");
    const projectSelect = document.getElementById("projectSelect");
    const deviceConfigSelect = document.getElementById("deviceConfigSelect");
    const patchConfigSelect = document.getElementById("patchConfigSelect");
    const createProjectBtn = document.getElementById("createProjectBtn");
    const createDeviceConfigBtn = document.getElementById("createDeviceConfigBtn");
    const createPatchConfigBtn = document.getElementById("createPatchConfigBtn");
    const configDialog = document.getElementById("configDialog");
    const configDialogForm = document.getElementById("configDialogForm");
    const configDialogTitle = document.getElementById("configDialogTitle");
    const configDialogDescription = document.getElementById("configDialogDescription");
    const configDialogInputLabel = document.getElementById("configDialogInputLabel");
    const configDialogInput = document.getElementById("configDialogInput");
    const configDialogTarget = document.getElementById("configDialogTarget");
    const configDialogConsequence = document.getElementById("configDialogConsequence");
    const configDialogOverwrite = document.getElementById("configDialogOverwrite");
    const configDialogOverwriteCheck = document.getElementById("configDialogOverwriteCheck");
    const configDialogOverwriteText = document.getElementById("configDialogOverwriteText");
    const configDialogError = document.getElementById("configDialogError");
    const configDialogCancel = document.getElementById("configDialogCancel");
    const configDialogSubmit = document.getElementById("configDialogSubmit");
    const PREVIEW_DOM = {
      audioAnalog: { img: previewAudioAnalog, link: previewLinkAudioAnalog },
      computerData: { img: previewComputerData, link: previewLinkComputerData },
      digitalAudio: { img: previewDigitalAudio, link: previewLinkDigitalAudio },
      allAudio: { img: previewAllAudio, link: previewLinkAllAudio },
      network: { img: previewNetwork, link: previewLinkNetwork },
      power: { img: previewPower, link: previewLinkPower },
      allConnections: { img: previewAllConnections, link: previewLinkAllConnections },
    };

    function cloneJson(data) {
      if (data == null) return data;
      return JSON.parse(JSON.stringify(data));
    }

    function isPatchbayDeviceName(name) {
      const token = String(name || "").toLowerCase();
      if (!token) return false;
      return token.includes("patchbay") || token.includes("desk patch") || token.includes("patch");
    }

    function normalizeMatrixPayload(payload) {
      if (Array.isArray(payload)) return { connections: payload };
      if (payload && typeof payload === "object") return payload;
      return { connections: [] };
    }

    function filterPatchbaysFromPayloads(modelInput, matrixInput) {
      const modelObject = (modelInput && typeof modelInput === "object") ? modelInput : {};
      const matrixObject = normalizeMatrixPayload(matrixInput);

      // Preserve patchbays in the model so Devices & Ports and Rack Manager can
      // manage them. Only their routing rows are hidden from the matrix view.
      const filteredModel = { ...modelObject };
      filteredModel.devices = Array.isArray(modelObject.devices) ? modelObject.devices : [];

      const filteredMatrix = { ...matrixObject };
      const rows = Array.isArray(matrixObject.connections) ? matrixObject.connections : [];
      filteredMatrix.connections = rows.filter((row) =>
        !isPatchbayDeviceName(String(row?.source_device || ""))
        && !isPatchbayDeviceName(String(row?.dest_device || ""))
      );
      return { model: filteredModel, matrix: filteredMatrix };
    }

    function loadJsonSync(url) {
      const target = String(url || "").trim();
      if (!target) return null;
      const cacheJoin = target.includes("?") ? "&" : "?";
      const requestUrl = `${target}${cacheJoin}_ts=${Date.now()}`;
      const req = new XMLHttpRequest();
      req.open("GET", requestUrl, false);
      req.send(null);
      if (req.status !== 0 && (req.status < 200 || req.status >= 300)) {
        throw new Error(`HTTP ${req.status} for ${target}`);
      }
      return JSON.parse(req.responseText || "null");
    }

    function hasUrlScheme(value) {
      return /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(String(value || "").trim());
    }

    function resolveInitialPayloads() {
      let model = cloneJson(EMBEDDED_MODEL) || {};
      let matrix = normalizeMatrixPayload(cloneJson(EMBEDDED_MATRIX));
      const filtered = filterPatchbaysFromPayloads(model, matrix);
      model = filtered.model;
      matrix = filtered.matrix;

      return { model, matrix };
    }

    const payloadState = resolveInitialPayloads();
    let MODEL = payloadState.model;
    let MATRIX = payloadState.matrix;
    const PORT_TYPE_PRESETS = {
      in: [
        { id: "MIC_IN", label: "Mic In", base: "Mic In", family: "AUDIO", transport: "XLR" },
        { id: "LINE_IN", label: "Line In", base: "Line In", family: "AUDIO", transport: "TRS" },
        { id: "AN_IN", label: "AN (Line In)", base: "Line In", family: "AUDIO", transport: "TRS" },
        { id: "SPK_IN", label: "Speaker Input", base: "Speaker Input", family: "AUDIO", transport: "SPK" },
        { id: "USB_IN", label: "USB", base: "USB", family: "COMP", transport: "USB" },
        { id: "ETH_IN", label: "Ethernet", base: "Ethernet", family: "NETWORK", transport: "CAT6" },
        { id: "HDMI_IN", label: "HDMI In", base: "HDMI In", family: "COMP", transport: "HDMI" },
        { id: "MADI_IN", label: "MADI In", base: "MADI In", family: "DIGI", transport: "MADI-OPT" },
        { id: "SPDIF_IN", label: "S/PDIF In", base: "S/PDIF In", family: "DIGI", transport: "SPDIF" },
        { id: "ADAT_IN", label: "ADAT In", base: "ADAT In", family: "DIGI", transport: "ADAT" },
        { id: "AES_IN", label: "AES In", base: "AES In", family: "DIGI", transport: "AES" },
        { id: "POWER_IN", label: "AC Power In", base: "AC Power In", family: "POWER", transport: "IEC" },
        { id: "CUSTOM_IN", label: "Custom", base: "Input", family: "AUDIO", transport: "" },
      ],
      out: [
        { id: "MIC_OUT", label: "Mic Out", base: "Mic Out", family: "AUDIO", transport: "XLR" },
        { id: "LINE_OUT", label: "Line Out", base: "Line Out", family: "AUDIO", transport: "TRS" },
        { id: "AN_OUT", label: "AN (Line Out)", base: "Line Out", family: "AUDIO", transport: "TRS" },
        { id: "SPK_OUT", label: "Speaker Out", base: "Speaker Out", family: "AUDIO", transport: "SPK" },
        { id: "USB_OUT", label: "USB", base: "USB", family: "COMP", transport: "USB" },
        { id: "ETH_OUT", label: "Ethernet", base: "Ethernet", family: "NETWORK", transport: "CAT6" },
        { id: "HDMI_OUT", label: "HDMI Out", base: "HDMI Out", family: "COMP", transport: "HDMI" },
        { id: "MADI_OUT", label: "MADI Out", base: "MADI Out", family: "DIGI", transport: "MADI-OPT" },
        { id: "SPDIF_OUT", label: "S/PDIF Out", base: "S/PDIF Out", family: "DIGI", transport: "SPDIF" },
        { id: "ADAT_OUT", label: "ADAT Out", base: "ADAT Out", family: "DIGI", transport: "ADAT" },
        { id: "AES_OUT", label: "AES Out", base: "AES Out", family: "DIGI", transport: "AES" },
        { id: "POWER_OUT", label: "AC Power Out", base: "AC Power Out", family: "POWER", transport: "SCHUKO" },
        { id: "CUSTOM_OUT", label: "Custom", base: "Output", family: "AUDIO", transport: "" },
      ],
    };
    const PORT_FAMILY_OPTIONS = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER"];
    const MATRIX_SCALE_STORAGE_KEY = "studioWiringMatrixScaleV1";
    const AUTO_SAVE_STORAGE_KEY = "studioWiringAutoSaveToDiskV1";
    const THEME_STORAGE_KEY = "studioWiringThemeModeV1";
    const PROJECT_SELECTION_STORAGE_KEY = "studioWiringProjectSelectionV1";
    const SAVE_DEBOUNCE_MS = 650;
    const IS_PROJECT_OUTPUT_PAGE = window.location.pathname.includes("/outputs/html/");
    const DEFAULT_PREVIEW_DIRECTORY = IS_PROJECT_OUTPUT_PAGE
      ? "../svgs"
      : "/projects/studio-sidecar/outputs/svgs";
    const DEFAULT_PREVIEW_PATHS = {
      audioAnalog: `${DEFAULT_PREVIEW_DIRECTORY}/audio-analog.svg`,
      computerData: `${DEFAULT_PREVIEW_DIRECTORY}/computer-data.svg`,
      digitalAudio: `${DEFAULT_PREVIEW_DIRECTORY}/digital-audio.svg`,
      allAudio: `${DEFAULT_PREVIEW_DIRECTORY}/all-audio.svg`,
      network: `${DEFAULT_PREVIEW_DIRECTORY}/network.svg`,
      power: `${DEFAULT_PREVIEW_DIRECTORY}/power.svg`,
      allConnections: `${DEFAULT_PREVIEW_DIRECTORY}/all-connections.svg`,
    };
    const DEFAULT_ROUTE_DEBUG_PATH = IS_PROJECT_OUTPUT_PAGE
      ? "../debug/route-debug.json"
      : "/projects/studio-sidecar/outputs/debug/route-debug.json";
    const PREVIEW_SPECS = [
      { key: "audioAnalog", label: "Audio Analog", file: "audio-analog.svg" },
      { key: "computerData", label: "Computer/Data", file: "computer-data.svg" },
      { key: "digitalAudio", label: "Digital Audio", file: "digital-audio.svg" },
      { key: "allAudio", label: "All Audio", file: "all-audio.svg" },
      { key: "network", label: "Network", file: "network.svg" },
      { key: "power", label: "Power", file: "power.svg" },
      { key: "allConnections", label: "All Connections", file: "all-connections.svg" },
    ];
    const DEFAULT_MATRIX_SCALE = {
      sourceWidth: 110,
      destWidth: 110,
      rowHeight: 26,
      firstRowHeight: 26,
      destHeaderHeight: 172,
    };
    const FIRST_ROW_HEIGHT_MIN = DEFAULT_MATRIX_SCALE.firstRowHeight;
    const FIRST_ROW_HEIGHT_MAX = 140;
    const MATRIX_VIEWPORT_MARGIN_PX = 4;
    const MATRIX_VIEWPORT_MIN_HEIGHT_PX = 420;
    const DEFAULT_PATCH_MODE = "single";
    const DEFAULT_PAIR_COUNT = 8;
    const PATCH_MODE_OPTIONS = new Set(["single", "paint", "range", "stereo", "multi"]);
    const DEVICE_VISIBILITY_TARGETS = [
      { key: "wiring_matrix", label: "Wiring Matrix", shortLabel: "Wiring" },
      { key: "routing_matrix", label: "Routing Matrix", shortLabel: "Routing" },
      { key: "connection_overview", label: "Connection Overview", shortLabel: "Overview" },
      { key: "visuals", label: "Visuals", shortLabel: "Visuals" },
    ];
    const prefersDarkMedia = (window.matchMedia && typeof window.matchMedia === "function")
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;

    let selectedDeviceName = "";
    let rackDragState = null;
    let devicePortTab = "in";
    let deviceEditorCommitInProgress = false;
    let saveApiEnabled = false;
    let saveApiConfig = null;
    let autoSaveEnabled = false;
    let saveTimer = null;
    let saveInFlight = false;
    let pendingSaveReason = "";
    let lastSavedConnectionsHash = "";
    let lastSavedModelHash = "";
    let loadedModelVersionHash = "";
    let loadedConnectionsVersionHash = "";
    let pendingModelEditSave = false;
    let regenerateApiEnabled = false;
    let previewPaths = { ...DEFAULT_PREVIEW_PATHS };
    let routeDebugPath = DEFAULT_ROUTE_DEBUG_PATH;
    let selectedThemeMode = "light";
    let hasExplicitThemePreference = false;
    let selectedMatrixSubTab = "patch";
    let selectedPatchMode = DEFAULT_PATCH_MODE;
    let pairCount = DEFAULT_PAIR_COUNT;
    let rangeSelectionAnchor = null;
    let paintSession = null;
    let matrixScale = { ...DEFAULT_MATRIX_SCALE };
    let matrixViewportRaf = 0;
    let matrixScrollSyncBusy = false;
    let projectCatalog = [];
    let selectedProjectKey = "";
    let selectedModelPath = "";
    let selectedConnectionsPath = "";
    let applyingProjectSelectors = false;
    let apiDetectInFlight = false;
    let apiRetryTimer = 0;
    let visibilityDragDeviceName = "";
    let lastPortCheckboxClickMeta = null;
    const lastPortToggleAnchorByKey = new Map();
    let lastVisibilityCheckboxClickMeta = null;
    let lastVisibilitySelectionAnchor = "";
    const selectedVisibilityDevices = new Set();
    const DEBUG_HISTORY_LIMIT = 80;
    const debugRuntime = {
      session_started_at: new Date().toISOString(),
      user_agent: String(navigator.userAgent || ""),
      language: String(navigator.language || ""),
      platform: String(navigator.platform || ""),
      initial_url: String(window.location.href || ""),
      actions: [],
      status_history: [],
      runtime_errors: [],
      unhandled_rejections: [],
    };
    window.__matrixDebug = debugRuntime;

    function normalizeMatrixSubTab(tabName) {
      const token = String(tabName || "patch").trim().toLowerCase();
      if (token === "connections") return "connections";
      return "patch";
    }

    function pushDebugHistory(list, entry, limit = DEBUG_HISTORY_LIMIT) {
      if (!Array.isArray(list)) return;
      list.push(entry);
      if (list.length > limit) {
        list.splice(0, list.length - limit);
      }
    }

    function eventTargetElement(event) {
      const target = event && event.target ? event.target : null;
      if (target instanceof HTMLElement) return target;
      if (target instanceof SVGElement) return target;
      if (target && typeof target === "object" && target.parentElement instanceof HTMLElement) {
        return target.parentElement;
      }
      return null;
    }

    function summarizeActionTarget(element) {
      if (!(element instanceof HTMLElement) && !(element instanceof SVGElement)) return "";
      const node = element instanceof HTMLElement
        ? element.closest("button,select,input,td.cell,[data-device-toggle],[data-group-toggle],[data-select-device],[data-remove-device],[data-port-remove],[data-visibility-device]")
        : null;
      const target = node || element;
      if (target instanceof HTMLTableCellElement && target.classList.contains("cell")) {
        const si = target.getAttribute("data-si");
        const di = target.getAttribute("data-di");
        if (si != null && di != null) return `cell(${si},${di})`;
        return "cell";
      }
      if (target instanceof HTMLElement) {
        const id = String(target.id || "").trim();
        if (id) return `#${id}`;
        const attrs = [
          "data-device-toggle",
          "data-group-toggle",
          "data-select-device",
          "data-remove-device",
          "data-port-remove",
          "data-visibility-device",
        ];
        for (const attr of attrs) {
          const value = target.getAttribute(attr);
          if (value != null) return `${attr}=${value}`;
        }
        const name = String(target.getAttribute("name") || "").trim();
        if (name) return `name=${name}`;
        const cls = String(target.className || "").trim().split(/\s+/).filter(Boolean).slice(0, 2).join(".");
        if (cls) return `${target.tagName.toLowerCase()}.${cls}`;
        return target.tagName.toLowerCase();
      }
      return "";
    }

    function captureDebugAction(type, detail = "") {
      pushDebugHistory(debugRuntime.actions, {
        ts: new Date().toISOString(),
        type: String(type || ""),
        detail: String(detail || ""),
      });
    }

    function captureDebugIssue(kind, message, extra = null) {
      const entry = {
        ts: new Date().toISOString(),
        kind: String(kind || ""),
        message: String(message || ""),
      };
      if (extra && typeof extra === "object") entry.extra = extra;
      if (kind === "unhandledrejection") {
        pushDebugHistory(debugRuntime.unhandled_rejections, entry, 30);
      } else {
        pushDebugHistory(debugRuntime.runtime_errors, entry, 30);
      }
    }

    function pushStatusHistory(message, warn = false) {
      pushDebugHistory(debugRuntime.status_history, {
        ts: new Date().toISOString(),
        warn: Boolean(warn),
        message: String(message || ""),
      });
    }

    function currentUiSnapshot() {
      const statusText = statusEl instanceof HTMLElement ? String(statusEl.textContent || "") : "";
      const matrixRows = matrixContainer instanceof HTMLElement
        ? matrixContainer.querySelectorAll("table tbody tr").length
        : -1;
      return {
        url: String(window.location.href || ""),
        status_text: statusText,
        status_warn: Boolean(statusEl instanceof HTMLElement && statusEl.classList.contains("warn")),
        matrix_rows: matrixRows,
        project_key: String(selectedProjectKey || ""),
        model_path: String(selectedModelPath || ""),
        connections_path: String(selectedConnectionsPath || ""),
        save_api_enabled: Boolean(saveApiEnabled),
        regenerate_api_enabled: Boolean(regenerateApiEnabled),
        patch_mode: String(selectedPatchMode || ""),
        matrix_family: String((familySelect && familySelect.value) || ""),
      };
    }

    function buildDebugReportPayload() {
      return {
        captured_at: new Date().toISOString(),
        ui: currentUiSnapshot(),
        env: {
          user_agent: debugRuntime.user_agent,
          language: debugRuntime.language,
          platform: debugRuntime.platform,
          session_started_at: debugRuntime.session_started_at,
        },
        actions: debugRuntime.actions.slice(-50),
        status_history: debugRuntime.status_history.slice(-50),
        runtime_errors: debugRuntime.runtime_errors.slice(-30),
        unhandled_rejections: debugRuntime.unhandled_rejections.slice(-30),
      };
    }

    function buildDebugReportText() {
      return JSON.stringify(buildDebugReportPayload(), null, 2);
    }

    function refreshDebugReportPanel() {
      if (!(debugReportPanel instanceof HTMLElement)) return;
      if (debugReportPanel.classList.contains("hidden")) return;
      debugReportPanel.textContent = buildDebugReportText();
    }

    function fallbackCopyText(text) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.opacity = "0";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.focus();
      area.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch (error) {
        copied = false;
      }
      document.body.removeChild(area);
      return copied;
    }

    async function copyDebugReportToClipboard() {
      const reportText = buildDebugReportText();
      let copied = false;
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
          await navigator.clipboard.writeText(reportText);
          copied = true;
        } else {
          copied = fallbackCopyText(reportText);
        }
      } catch (error) {
        copied = fallbackCopyText(reportText);
      }
      if (copied) {
        captureDebugAction("copy-debug-report", `chars=${reportText.length}`);
        setStatus("Copied debug report to clipboard.");
      } else {
        captureDebugIssue("copy-debug-report", "Clipboard copy failed");
        setStatus("Copy debug report failed. Open debug details and copy manually.", true);
      }
      refreshDebugReportPanel();
    }

    function toggleDebugReportPanel() {
      if (!(debugReportPanel instanceof HTMLElement)) return;
      const willShow = debugReportPanel.classList.contains("hidden");
      debugReportPanel.classList.toggle("hidden", !willShow);
      if (toggleDebugPanelBtn instanceof HTMLButtonElement) {
        toggleDebugPanelBtn.textContent = willShow ? "Hide Debug Details" : "Show Debug Details";
      }
      if (willShow) {
        debugReportPanel.textContent = buildDebugReportText();
      }
      captureDebugAction("toggle-debug-details", willShow ? "show" : "hide");
    }

    function bindDebugTools() {
      if (copyDebugReportBtn instanceof HTMLButtonElement) {
        copyDebugReportBtn.onclick = () => {
          void copyDebugReportToClipboard();
        };
      }
      if (toggleDebugPanelBtn instanceof HTMLButtonElement) {
        toggleDebugPanelBtn.onclick = () => {
          toggleDebugReportPanel();
        };
      }
      document.addEventListener("click", (event) => {
        const target = eventTargetElement(event);
        if (!target) return;
        const detail = summarizeActionTarget(target);
        if (!detail) return;
        captureDebugAction("click", detail);
      }, true);
      document.addEventListener("change", (event) => {
        const target = eventTargetElement(event);
        if (!target) return;
        const detail = summarizeActionTarget(target);
        if (!detail) return;
        let nextValue = "";
        if (target instanceof HTMLInputElement) {
          nextValue = target.type === "checkbox" ? String(Boolean(target.checked)) : String(target.value || "");
        } else if (target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) {
          nextValue = String(target.value || "");
        }
        captureDebugAction("change", nextValue ? `${detail} => ${nextValue}` : detail);
      }, true);
    }

    function captureDeviceEditorViewState() {
      if (!(deviceEditorPanel instanceof HTMLElement)) return null;
      const wrap = deviceEditorPanel.querySelector(".port-table-wrap");
      const state = {
        deviceName: String(selectedDeviceName || ""),
        portTab: String(devicePortTab || "in"),
        scrollTop: wrap instanceof HTMLElement ? Number(wrap.scrollTop || 0) : 0,
        scrollLeft: wrap instanceof HTMLElement ? Number(wrap.scrollLeft || 0) : 0,
        focus: null,
      };
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !deviceEditorPanel.contains(active)) return state;
      const trackedAttrs = [
        "data-port-name",
        "data-port-family",
        "data-port-transport",
        "data-port-visible",
        "data-port-enabled",
        "data-port-remove",
      ];
      for (const attr of trackedAttrs) {
        const value = active.getAttribute(attr);
        if (value == null) continue;
        state.focus = {
          attr,
          value: String(value),
          id: String(active.id || ""),
        };
        break;
      }
      if (!state.focus && active.id) {
        state.focus = { attr: "id", value: String(active.id), id: String(active.id) };
      }
      return state;
    }

    function restoreDeviceEditorViewState(state) {
      if (!(deviceEditorPanel instanceof HTMLElement) || !state || typeof state !== "object") return;
      if (String(state.deviceName || "") !== String(selectedDeviceName || "")) return;
      if (String(state.portTab || "in") !== String(devicePortTab || "in")) return;
      const wrap = deviceEditorPanel.querySelector(".port-table-wrap");
      if (wrap instanceof HTMLElement) {
        wrap.scrollTop = Math.max(0, Number(state.scrollTop || 0));
        wrap.scrollLeft = Math.max(0, Number(state.scrollLeft || 0));
      }
      const focus = state.focus;
      if (!focus || typeof focus !== "object") return;
      let target = null;
      if (focus.attr === "id" && focus.value) {
        const byId = document.getElementById(String(focus.value));
        if (byId instanceof HTMLElement && deviceEditorPanel.contains(byId)) {
          target = byId;
        }
      } else if (focus.attr && focus.value != null) {
        const attr = String(focus.attr);
        const value = String(focus.value);
        const candidates = Array.from(deviceEditorPanel.querySelectorAll(`[${attr}]`));
        target = candidates.find((node) => node instanceof HTMLElement && String(node.getAttribute(attr) || "") === value) || null;
      }
      if (target instanceof HTMLElement) {
        target.focus({ preventScroll: true });
      }
    }

    function consumePortCheckboxShift(attrName, index) {
      if (!lastPortCheckboxClickMeta || typeof lastPortCheckboxClickMeta !== "object") return false;
      const ageMs = Date.now() - Number(lastPortCheckboxClickMeta.ts || 0);
      if (ageMs > 1500) return false;
      if (String(lastPortCheckboxClickMeta.attr || "") !== String(attrName || "")) return false;
      if (Number(lastPortCheckboxClickMeta.index) !== Number(index)) return false;
      const shift = Boolean(lastPortCheckboxClickMeta.shiftKey);
      lastPortCheckboxClickMeta = null;
      return shift;
    }

    function applyPortCheckboxRange(device, ports, attrName, targetInput, checked, shiftKey) {
      if (!(targetInput instanceof HTMLInputElement)) return 0;
      if (!deviceEditorPanel) return 0;
      const selector = `input[type="checkbox"][${attrName}]`;
      const boxes = Array.from(deviceEditorPanel.querySelectorAll(selector));
      const currentPos = boxes.indexOf(targetInput);
      const key = `${String(device?.name || "")}::${String(devicePortTab || "in")}::${String(attrName || "")}`;
      const anchorPos = Number(lastPortToggleAnchorByKey.get(key));
      let startPos = currentPos;
      let endPos = currentPos;
      if (
        shiftKey
        && Number.isInteger(currentPos)
        && currentPos >= 0
        && Number.isInteger(anchorPos)
        && anchorPos >= 0
      ) {
        startPos = Math.min(anchorPos, currentPos);
        endPos = Math.max(anchorPos, currentPos);
      }
      const updateBox = (box) => {
        if (!(box instanceof HTMLInputElement)) return 0;
        const idx = Number(box.getAttribute(attrName));
        if (!Number.isInteger(idx) || idx < 0 || idx >= ports.length) return 0;
        const port = ports[idx];
        if (!port || typeof port !== "object") return 0;
        let changed = 0;
        if (attrName === "data-port-visible") {
          const prevVisible = !parseBoolLike(port?.hidden, false) && parseBoolLike(port?.visible, true);
          if (prevVisible !== checked) changed = 1;
          port.visible = checked;
          port.hidden = !checked;
        } else if (attrName === "data-port-enabled") {
          const prevEnabled = parseBoolLike(port?.enabled, true) && !parseBoolLike(port?.disabled, false);
          if (prevEnabled !== checked) changed = 1;
          port.enabled = checked;
          port.disabled = !checked;
        } else {
          return 0;
        }
        if (box.checked !== checked) box.checked = checked;
        return changed;
      };

      let changedCount = 0;
      if (Number.isInteger(startPos) && Number.isInteger(endPos) && startPos >= 0 && endPos >= startPos) {
        for (let pos = startPos; pos <= endPos; pos += 1) {
          changedCount += updateBox(boxes[pos]);
        }
      } else {
        changedCount += updateBox(targetInput);
      }

      if (Number.isInteger(currentPos) && currentPos >= 0) {
        lastPortToggleAnchorByKey.set(key, currentPos);
      }
      return changedCount;
    }

    function consumeVisibilityCheckboxClickMeta(deviceName, targetName) {
      if (!lastVisibilityCheckboxClickMeta || typeof lastVisibilityCheckboxClickMeta !== "object") return {};
      const ageMs = Date.now() - Number(lastVisibilityCheckboxClickMeta.ts || 0);
      if (ageMs > 1500) return {};
      if (String(lastVisibilityCheckboxClickMeta.device || "") !== String(deviceName || "")) return {};
      if (String(lastVisibilityCheckboxClickMeta.target || "") !== String(targetName || "")) return {};
      const meta = { ...lastVisibilityCheckboxClickMeta };
      lastVisibilityCheckboxClickMeta = null;
      return meta;
    }

    // ----- Theme + display preferences -----
    function loadThemePreference() {
      try {
        const raw = String(window.localStorage.getItem(THEME_STORAGE_KEY) || "").trim().toLowerCase();
        if (raw === "dark" || raw === "light") return raw;
      } catch (error) {
        // Ignore storage failures.
      }
      return "";
    }

    function persistThemePreference(mode) {
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, mode);
      } catch (error) {
        // Ignore storage failures.
      }
    }

    function resolveThemeMode(mode) {
      if (mode === "dark" || mode === "light") return mode;
      return prefersDarkMedia && prefersDarkMedia.matches ? "dark" : "light";
    }

    function applyThemeMode(mode, persist = true) {
      const normalized = (mode === "dark" || mode === "light") ? mode : resolveThemeMode(mode);
      selectedThemeMode = normalized;
      const resolved = resolveThemeMode(normalized);
      document.body.classList.toggle("theme-dark", resolved === "dark");
      document.body.classList.toggle("theme-light", resolved !== "dark");
      if (themeToggleBtn) {
        const isDark = resolved === "dark";
        themeToggleBtn.textContent = isDark ? "Dark Mode: On" : "Dark Mode: Off";
        themeToggleBtn.title = isDark ? "Switch to light mode" : "Switch to dark mode";
      }
      if (persist) {
        hasExplicitThemePreference = true;
        persistThemePreference(normalized);
      }
    }

    function applyDestOrientation() {
      document.body.classList.add("dest-orientation-vertical");
      document.body.classList.remove("dest-orientation-horizontal");
    }

    function normalizePatchMode(mode) {
      const token = String(mode || "").trim().toLowerCase();
      return PATCH_MODE_OPTIONS.has(token) ? token : DEFAULT_PATCH_MODE;
    }

    function normalizePairCount(value) {
      const numeric = Math.floor(Number(value));
      if (!Number.isFinite(numeric)) return DEFAULT_PAIR_COUNT;
      return Math.max(2, Math.min(64, numeric));
    }

    function updatePatchModeHint() {
      if (!(patchModeHint instanceof HTMLElement)) return;
      const hints = {
        single: "Single: click one crosspoint.",
        paint: "Paint: click-drag to connect/disconnect multiple cells.",
        range: "Range: click start cell, then end cell to patch a block.",
        stereo: "Stereo Pair: patches 2 aligned channels from clicked cell.",
        multi: `Multi Pair: patches ${pairCount} aligned channels from clicked cell.`,
      };
      patchModeHint.textContent = hints[selectedPatchMode] || hints.single;
    }

    function setPatchMode(mode) {
      selectedPatchMode = normalizePatchMode(mode);
      if (patchModeSelect && patchModeSelect.value !== selectedPatchMode) {
        patchModeSelect.value = selectedPatchMode;
      }
      if (selectedPatchMode !== "range") {
        rangeSelectionAnchor = null;
        if (matrixContainer instanceof HTMLElement) {
          for (const cell of Array.from(matrixContainer.querySelectorAll("td.cell.range-anchor"))) {
            cell.classList.remove("range-anchor");
          }
        }
      }
      updatePatchModeHint();
    }

    function setPairCount(value) {
      pairCount = normalizePairCount(value);
      if (pairCountInput && pairCountInput.value !== String(pairCount)) {
        pairCountInput.value = String(pairCount);
      }
      updatePatchModeHint();
    }

    function clampNumber(value, minValue, maxValue, fallback) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return fallback;
      return Math.max(minValue, Math.min(maxValue, numeric));
    }

    function readScaleControls() {
      return {
        sourceWidth: clampNumber(matrixScale?.sourceWidth, 80, 420, DEFAULT_MATRIX_SCALE.sourceWidth),
        destWidth: clampNumber(matrixScale?.destWidth, 40, 420, DEFAULT_MATRIX_SCALE.destWidth),
        rowHeight: clampNumber(matrixScale?.rowHeight, 18, 64, DEFAULT_MATRIX_SCALE.rowHeight),
        firstRowHeight: clampNumber(matrixScale?.firstRowHeight, FIRST_ROW_HEIGHT_MIN, FIRST_ROW_HEIGHT_MAX, DEFAULT_MATRIX_SCALE.firstRowHeight),
        destHeaderHeight: clampNumber(matrixScale?.destHeaderHeight, 120, 340, DEFAULT_MATRIX_SCALE.destHeaderHeight),
      };
    }

    function writeScaleControls(scale) {
      const incoming = scale || DEFAULT_MATRIX_SCALE;
      matrixScale = {
        sourceWidth: clampNumber(incoming?.sourceWidth, 80, 420, DEFAULT_MATRIX_SCALE.sourceWidth),
        destWidth: clampNumber(incoming?.destWidth, 40, 420, DEFAULT_MATRIX_SCALE.destWidth),
        rowHeight: clampNumber(incoming?.rowHeight, 18, 64, DEFAULT_MATRIX_SCALE.rowHeight),
        firstRowHeight: clampNumber(incoming?.firstRowHeight, FIRST_ROW_HEIGHT_MIN, FIRST_ROW_HEIGHT_MAX, DEFAULT_MATRIX_SCALE.firstRowHeight),
        destHeaderHeight: clampNumber(incoming?.destHeaderHeight, 120, 340, DEFAULT_MATRIX_SCALE.destHeaderHeight),
      };
    }

    function persistScale(scale) {
      try {
        window.localStorage.setItem(MATRIX_SCALE_STORAGE_KEY, JSON.stringify(scale));
      } catch (error) {
        // Ignore storage failures.
      }
    }

    function loadPersistedScale() {
      try {
        const raw = window.localStorage.getItem(MATRIX_SCALE_STORAGE_KEY);
        if (!raw) return { ...DEFAULT_MATRIX_SCALE };
        const parsed = JSON.parse(raw);
        return {
          sourceWidth: clampNumber(parsed?.sourceWidth, 80, 420, DEFAULT_MATRIX_SCALE.sourceWidth),
          destWidth: clampNumber(parsed?.destWidth, 40, 420, DEFAULT_MATRIX_SCALE.destWidth),
          rowHeight: clampNumber(parsed?.rowHeight, 18, 64, DEFAULT_MATRIX_SCALE.rowHeight),
          firstRowHeight: clampNumber(parsed?.firstRowHeight, FIRST_ROW_HEIGHT_MIN, FIRST_ROW_HEIGHT_MAX, DEFAULT_MATRIX_SCALE.firstRowHeight),
          destHeaderHeight: clampNumber(parsed?.destHeaderHeight, 120, 340, DEFAULT_MATRIX_SCALE.destHeaderHeight),
        };
      } catch (error) {
        return { ...DEFAULT_MATRIX_SCALE };
      }
    }

    function applyMatrixScale(shouldPersist = true) {
      const scale = readScaleControls();
      writeScaleControls(scale);
      const root = document.documentElement;
      root.style.setProperty("--matrix-source-col-width", `${scale.sourceWidth}px`);
      root.style.setProperty("--matrix-cell-width", `${scale.destWidth}px`);
      root.style.setProperty("--matrix-row-height", `${scale.rowHeight}px`);
      root.style.setProperty("--matrix-first-row-height", `${scale.firstRowHeight}px`);
      root.style.setProperty("--matrix-dest-header-height", `${scale.destHeaderHeight}px`);
      root.style.setProperty("--matrix-dest-inner-height", `${Math.max(90, scale.destHeaderHeight - 6)}px`);
      root.style.setProperty("--matrix-dest-label-max-height", `${Math.max(52, scale.destHeaderHeight - 34)}px`);
      if (shouldPersist) persistScale(scale);
    }

    function updateMatrixViewportHeight() {
      // Keep matrix/table viewport filling remaining screen height from their current top edge.
      const reference = (matrixContainer && !matrixContainer.classList.contains("hidden"))
        ? matrixContainer
        : ((connectionList && !connectionList.classList.contains("hidden")) ? connectionList : null);
      if (!(reference instanceof HTMLElement)) return;
      if (!reference.offsetParent) return;
      const viewportHeight = Number(window.innerHeight || document.documentElement.clientHeight || 0);
      if (!Number.isFinite(viewportHeight) || viewportHeight <= 0) return;
      const rect = reference.getBoundingClientRect();
      const available = Math.floor(viewportHeight - Number(rect.top || 0) - MATRIX_VIEWPORT_MARGIN_PX);
      const nextHeight = Math.max(MATRIX_VIEWPORT_MIN_HEIGHT_PX, available);
      document.documentElement.style.setProperty("--matrix-viewport-height", `${nextHeight}px`);
    }

    function scheduleMatrixViewportHeightUpdate() {
      if (matrixViewportRaf) return;
      matrixViewportRaf = window.requestAnimationFrame(() => {
        matrixViewportRaf = 0;
        updateMatrixViewportHeight();
      });
    }

    // ----- Pointer-drag matrix scaling -----
    function beginMatrixScaleDrag(kind, event) {
      if (!event) return;
      event.preventDefault();
      const startX = Number(event.clientX || 0);
      const startY = Number(event.clientY || 0);
      const startScale = readScaleControls();
      const handle = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
      if (handle) handle.classList.add("active");

      const move = (moveEvent) => {
        const dx = Number(moveEvent.clientX || 0) - startX;
        const dy = Number(moveEvent.clientY || 0) - startY;
        const next = { ...startScale };
        if (kind === "sourceWidth") {
          next.sourceWidth = clampNumber(startScale.sourceWidth + dx, 80, 420, DEFAULT_MATRIX_SCALE.sourceWidth);
        } else if (kind === "destWidth") {
          next.destWidth = clampNumber(startScale.destWidth + dx, 40, 420, DEFAULT_MATRIX_SCALE.destWidth);
        } else if (kind === "firstRowHeight") {
          next.firstRowHeight = clampNumber(
            startScale.firstRowHeight + dy,
            FIRST_ROW_HEIGHT_MIN,
            FIRST_ROW_HEIGHT_MAX,
            DEFAULT_MATRIX_SCALE.firstRowHeight,
          );
        } else if (kind === "rowHeight") {
          next.rowHeight = clampNumber(startScale.rowHeight + dy, 18, 64, DEFAULT_MATRIX_SCALE.rowHeight);
        } else if (kind === "destHeaderHeight") {
          next.destHeaderHeight = clampNumber(startScale.destHeaderHeight + dy, 120, 340, DEFAULT_MATRIX_SCALE.destHeaderHeight);
        }
        writeScaleControls(next);
        applyMatrixScale(true);
      };

      const finish = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", finish);
        window.removeEventListener("pointercancel", finish);
        if (handle) handle.classList.remove("active");
      };

      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", finish);
      window.addEventListener("pointercancel", finish);
    }

    function createMatrixResizeHandle(kind, axisClass, title, extraClass = "") {
      const handle = document.createElement("div");
      handle.className = `resize-handle ${axisClass}${extraClass ? ` ${extraClass}` : ""}`;
      handle.title = title;
      handle.dataset.resizeKind = kind;
      handle.onpointerdown = (event) => beginMatrixScaleDrag(kind, event);
      return handle;
    }

    function attachMatrixResizeHandles(matrixTable) {
      if (!matrixTable) return;
      for (const existing of Array.from(matrixTable.querySelectorAll(".resize-handle"))) {
        existing.remove();
      }

      const topLeft = matrixTable.querySelector("thead .sticky-left.top-left");
      if (topLeft instanceof HTMLElement) {
        topLeft.style.position = "sticky";
        topLeft.style.left = "0px";
        topLeft.style.top = "0px";
        topLeft.style.zIndex = "40";
        topLeft.appendChild(createMatrixResizeHandle("sourceWidth", "col", "Drag to resize source width"));
        topLeft.appendChild(
          createMatrixResizeHandle("destHeaderHeight", "row", "Drag to resize destination header height", "row-top")
        );
      }

      const firstDestHead = matrixTable.querySelector("thead th.dest-head.dest-port-head");
      if (firstDestHead instanceof HTMLElement) {
        firstDestHead.style.position = "sticky";
        firstDestHead.appendChild(createMatrixResizeHandle("destWidth", "col", "Drag to resize destination width"));
      }

      const sourceRowHeads = Array.from(matrixTable.querySelectorAll("tbody th.sticky-left.source-head"));
      const firstBodyRowHead = sourceRowHeads[0] instanceof HTMLElement ? sourceRowHeads[0] : null;
      if (firstBodyRowHead) {
        firstBodyRowHead.style.position = "sticky";
        firstBodyRowHead.appendChild(
          createMatrixResizeHandle("firstRowHeight", "row", "Drag to resize first source row height")
        );
      }

      const firstDeviceRowHead = matrixTable.querySelector("tbody th.sticky-left.source-head.device-folder");
      let rowHeightAnchor = null;
      if (firstDeviceRowHead instanceof HTMLElement) {
        const deviceIndex = sourceRowHeads.findIndex((row) => row === firstDeviceRowHead);
        const movedDown = deviceIndex >= 0 ? sourceRowHeads[deviceIndex + 1] : null;
        rowHeightAnchor = movedDown instanceof HTMLElement ? movedDown : firstDeviceRowHead;
      } else {
        rowHeightAnchor = sourceRowHeads[1] instanceof HTMLElement
          ? sourceRowHeads[1]
          : (firstBodyRowHead || null);
      }
      if (rowHeightAnchor instanceof HTMLElement) {
        rowHeightAnchor.style.position = "sticky";
        rowHeightAnchor.appendChild(
          createMatrixResizeHandle("rowHeight", "row", "Drag to resize source device row height", "edge-right")
        );
      }
    }

    function bindMatrixHorizontalScroll() {
      if (!(matrixContainer instanceof HTMLElement)) return;
      if (matrixContainer.dataset.hScrollBound === "1") return;
      matrixContainer.dataset.hScrollBound = "1";
      matrixContainer.addEventListener("wheel", (event) => {
        if (!(event instanceof WheelEvent)) return;
        const canScrollX = matrixContainer.scrollWidth > (matrixContainer.clientWidth + 1);
        if (!canScrollX) return;
        // Keep native horizontal gesture behavior (trackpad inertia/momentum).
        // Only add Shift+wheel fallback for mice without horizontal wheel.
        const deltaYAbs = Math.abs(Number(event.deltaY || 0));
        const usesShiftFallback = Boolean(event.shiftKey) && deltaYAbs > 0;
        if (!usesShiftFallback) return;
        const delta = Number(event.deltaY || 0);
        if (!Number.isFinite(delta) || delta === 0) return;
        const previous = matrixContainer.scrollLeft;
        matrixContainer.scrollLeft = previous + delta;
        if (matrixContainer.scrollLeft !== previous) {
          event.preventDefault();
        }
      }, { passive: false });
    }

    function syncMatrixHorizontalScroller() {
      if (!(matrixContainer instanceof HTMLElement)) return;
      if (!(matrixXScroll instanceof HTMLElement)) return;
      if (!(matrixXScrollInner instanceof HTMLElement)) return;

      const showForPatch = selectedMatrixSubTab === "patch"
        && !matrixContainer.classList.contains("hidden");
      if (!showForPatch) {
        matrixXScroll.classList.add("hidden");
        return;
      }

      const contentWidth = Math.max(
        Number(matrixContainer.scrollWidth || 0),
        Number(matrixContainer.clientWidth || 0),
      );
      const viewWidth = Number(matrixContainer.clientWidth || 0);
      matrixXScrollInner.style.width = `${Math.max(1, contentWidth)}px`;
      const hasHorizontal = contentWidth > (viewWidth + 1);
      matrixXScroll.classList.toggle("hidden", !hasHorizontal);
      if (!hasHorizontal) return;

      if (!matrixScrollSyncBusy) {
        matrixScrollSyncBusy = true;
        matrixXScroll.scrollLeft = matrixContainer.scrollLeft;
        matrixScrollSyncBusy = false;
      }
    }

    function bindMatrixHorizontalScrollerSync() {
      if (!(matrixContainer instanceof HTMLElement)) return;
      if (!(matrixXScroll instanceof HTMLElement)) return;
      if (matrixContainer.dataset.hScrollSyncBound === "1") return;
      matrixContainer.dataset.hScrollSyncBound = "1";

      matrixContainer.addEventListener("scroll", () => {
        if (matrixScrollSyncBusy) return;
        matrixScrollSyncBusy = true;
        matrixXScroll.scrollLeft = matrixContainer.scrollLeft;
        matrixScrollSyncBusy = false;
      }, { passive: true });

      matrixXScroll.addEventListener("scroll", () => {
        if (matrixScrollSyncBusy) return;
        matrixScrollSyncBusy = true;
        matrixContainer.scrollLeft = matrixXScroll.scrollLeft;
        matrixScrollSyncBusy = false;
      }, { passive: true });
    }

    function loadAutoSavePreference() {
      try {
        return window.localStorage.getItem(AUTO_SAVE_STORAGE_KEY) === "1";
      } catch (error) {
        return false;
      }
    }

    function persistAutoSavePreference(enabled) {
      try {
        window.localStorage.setItem(AUTO_SAVE_STORAGE_KEY, enabled ? "1" : "0");
      } catch (error) {
        // Ignore storage failures.
      }
    }

    function applySaveControlsState() {
      if (regeneratePreviewsBtn) regeneratePreviewsBtn.disabled = !(saveApiEnabled && regenerateApiEnabled);
      if (openRouteDebugBtn) openRouteDebugBtn.disabled = !String(routeDebugPath || "").trim();
      if (saveDeviceConfigAsBtn) saveDeviceConfigAsBtn.disabled = !saveApiEnabled;
      if (savePatchConfigAsBtn) savePatchConfigAsBtn.disabled = !saveApiEnabled;
    }

    // ----- Visual preview pipeline (single source of truth for preview files) -----
    function setPreviewStatus(message, warn = false) {
      if (!previewStatus) return;
      previewStatus.textContent = String(message || "");
      previewStatus.classList.toggle("warn", Boolean(warn));
    }

    function mergePreviewPathsFromConfig(configPayload) {
      const candidate = configPayload && typeof configPayload === "object" ? configPayload.preview_paths : null;
      if (!candidate || typeof candidate !== "object") return;
      previewPaths = {
        ...previewPaths,
        ...Object.fromEntries(
          Object.entries(candidate)
            .filter(([_, value]) => typeof value === "string" && String(value).trim())
            .map(([key, value]) => [key, String(value).trim()])
        ),
      };
    }

    function mergeRouteDebugPathFromConfig(configPayload) {
      const candidate = configPayload && typeof configPayload === "object"
        ? String(configPayload.route_debug_path || "").trim()
        : "";
      if (candidate) routeDebugPath = candidate;
    }

    function resolvePreviewBasePath(key) {
      return String(previewPaths?.[key] || DEFAULT_PREVIEW_PATHS?.[key] || "").trim();
    }

    function appendCacheBuster(url, paramName, token) {
      const join = String(url).includes("?") ? "&" : "?";
      return `${url}${join}${paramName}=${token}`;
    }

    function setPreviewMediaSource(node, url) {
      if (!node || !url) return;
      if (node instanceof HTMLImageElement) {
        node.src = url;
      } else {
        node.setAttribute("data", url);
      }
    }

    function refreshVisualPreviews(reason = "refresh") {
      const ts = Date.now();
      for (const spec of PREVIEW_SPECS) {
        const base = resolvePreviewBasePath(spec.key);
        if (!base) continue;
        const dom = PREVIEW_DOM[spec.key] || {};
        const url = appendCacheBuster(base, "_pv", ts);
        setPreviewMediaSource(dom.img, url);
        if (dom.link) dom.link.href = url;
      }
      setPreviewStatus(`Visual previews refreshed (${reason})`);
    }

    async function downloadPreviewSvg(path, filename) {
      const base = String(path || "").trim();
      if (!base) return false;
      const join = base.includes("?") ? "&" : "?";
      const url = `${base}${join}_dl=${Date.now()}`;
      try {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = objectUrl;
        a.download = String(filename || "diagram.svg");
        a.click();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        return true;
      } catch (error) {
        return false;
      }
    }

    async function downloadAllSvgPreviews() {
      if (!saveApiEnabled || !selectedProjectKey) {
        setStatus("Saving an SVG folder requires the local server and a selected project.", true);
        return false;
      }
      if (typeof window.showDirectoryPicker !== "function") {
        setStatus("Saving a folder requires a browser with folder access, such as Chrome or Edge.", true);
        return false;
      }
      let parentDirectory;
      try {
        parentDirectory = await window.showDirectoryPicker({
          id: "studio-wiring-svg-export",
          mode: "readwrite",
        });
      } catch (error) {
        if (error && error.name === "AbortError") {
          setStatus("SVG folder save cancelled.");
          return false;
        }
        setStatus(`Could not open the folder picker: ${String(error)}`, true);
        return false;
      }
      setStatus("Updating visuals and preparing SVG folder…");
      const current = await saveJsonToDisk("save-svg-folder", true, true);
      if (!current) {
        setStatus("Could not update visuals before saving the SVG folder.", true);
        return false;
      }
      try {
        const query = new URLSearchParams({
          project_key: selectedProjectKey,
          _dl: String(Date.now()),
        });
        const response = await fetch(`/api/svg-files?${query.toString()}`, { cache: "no-store" });
        if (!response.ok) {
          const details = await parseApiError(response, `HTTP ${response.status}`);
          throw new Error(details);
        }
        const payload = await response.json();
        const files = Array.isArray(payload.files) ? payload.files : [];
        if (!files.length) throw new Error("No SVG files were returned");
        const folderName = String(payload.folder_name || "studio-project-svgs");
        const outputDirectory = await parentDirectory.getDirectoryHandle(folderName, { create: true });
        for (const file of files) {
          const fileHandle = await outputDirectory.getFileHandle(String(file.name), { create: true });
          const writable = await fileHandle.createWritable();
          await writable.write(String(file.content || ""));
          await writable.close();
        }
        setStatus(`Saved ${folderName} (${files.length} SVGs).`);
        return true;
      } catch (error) {
        setStatus(`Failed to save SVG folder: ${String(error)}`, true);
        return false;
      }
    }

    function openRouteDebugJson() {
      const base = String(routeDebugPath || "").trim();
      if (!base) {
        setStatus("Route debug JSON path is unavailable.", true);
        return;
      }
      const url = appendCacheBuster(base, "_dbg", Date.now());
      window.open(url, "_blank", "noopener,noreferrer");
      setStatus(`Opened route debug JSON: ${base}`);
    }

    async function regenerateVisualPreviews(reason = "manual", silent = false) {
      if (!(saveApiEnabled && regenerateApiEnabled)) {
        refreshVisualPreviews("refresh-only");
        return false;
      }
      try {
        const response = await fetch("/api/regenerate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason }),
        });
        if (!response.ok) {
          const details = await parseApiError(response, `HTTP ${response.status}`);
          throw new Error(`Regenerate failed: ${details}`);
        }
        const payload = await response.json();
        if (!payload || payload.ok !== true) {
          throw new Error(String(payload?.error || "Regenerate failed"));
        }
        if (payload.preview_paths && typeof payload.preview_paths === "object") {
          mergePreviewPathsFromConfig(payload);
        }
        mergeRouteDebugPathFromConfig(payload);
        refreshVisualPreviews(`regenerated: ${reason}`);
        if (!silent) {
          setStatus(`Regenerated visual outputs (${reason})`);
        }
        return true;
      } catch (error) {
        if (!silent) {
          setStatus(`Visual regenerate failed: ${String(error)}`, true);
        } else {
          setPreviewStatus(`Visual regenerate failed: ${String(error)}`, true);
        }
        return false;
      }
    }

    function setAutoSaveEnabled(enabled, announce = false) {
      autoSaveEnabled = Boolean(enabled);
      persistAutoSavePreference(autoSaveEnabled);
      applySaveControlsState();
      if (announce) {
        if (!saveApiEnabled && autoSaveEnabled) {
          setStatus("Auto-save requested, but save API is unavailable. Start routing_matrix_server.py.", true);
        } else {
          setStatus(autoSaveEnabled ? "Auto-save enabled" : "Auto-save disabled");
        }
      }
      if (autoSaveEnabled && saveApiEnabled) {
        scheduleAutoSave("auto-save enabled");
      }
    }

    function computeSaveHashes() {
      const modelHash = JSON.stringify(cloneJson(MODEL) || {});
      const connHash = JSON.stringify(buildConnectionsPayload(false).connections || []);
      return { modelHash, connHash };
    }

    function canonicalizeJsonForHash(value) {
      if (Array.isArray(value)) return value.map((item) => canonicalizeJsonForHash(item));
      if (value && typeof value === "object") {
        const out = {};
        for (const key of Object.keys(value).sort()) out[key] = canonicalizeJsonForHash(value[key]);
        return out;
      }
      return value;
    }

    async function computeJsonPayloadHash(payload) {
      const text = JSON.stringify(canonicalizeJsonForHash(payload));
      if (!(window.crypto && window.crypto.subtle && typeof TextEncoder !== "undefined")) return "";
      const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
      return Array.from(new Uint8Array(digest))
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("");
    }

    async function parseApiError(response, fallbackMessage) {
      const fallback = String(fallbackMessage || `HTTP ${response?.status ?? "?"}`);
      if (!response) return fallback;
      let text = "";
      try {
        text = await response.text();
      } catch (error) {
        return fallback;
      }
      if (!text) return fallback;
      try {
        const payload = JSON.parse(text);
        if (payload && typeof payload === "object" && payload.error) {
          return String(payload.error);
        }
      } catch (error) {
        // Keep raw response body as fallback.
      }
      return text;
    }

    function stripJsonExtension(value) {
      const token = String(value || "").trim();
      if (!token) return "";
      return token.toLowerCase().endsWith(".json") ? token.slice(0, -5) : token;
    }

    function escapeRegExp(value) {
      const text = String(value == null ? "" : value);
      let output = "";
      const specials = "\\\\^$.*+?()[]{}|";
      for (const ch of text) {
        output += specials.includes(ch) ? `\\\\${ch}` : ch;
      }
      return output;
    }

    function labelFromPath(pathValue) {
      const token = String(pathValue || "").trim().split("\\\\").join("/");
      if (!token) return "(none)";
      const parts = token.split("/");
      const basename = parts[parts.length - 1] || token;
      return stripJsonExtension(basename) || basename;
    }

    function toApiAbsolutePath(pathValue) {
      const token = String(pathValue || "").trim();
      if (!token) return "";
      if (hasUrlScheme(token) || token.startsWith("//")) return token;
      if (token.startsWith("/")) return token;
      if (token.startsWith("./") || token.startsWith("../")) {
        try {
          return new URL(token, window.location.href).toString();
        } catch (error) {
          // Fall through to rooted path fallback.
        }
      }
      return `/${token.replace(/^\/+/, "")}`;
    }

    async function fetchJsonAtPath(pathValue) {
      const basePath = toApiAbsolutePath(pathValue);
      if (!basePath) throw new Error("Missing JSON path");
      const requestUrl = appendCacheBuster(basePath, "_ts", Date.now());
      const response = await fetch(requestUrl, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status} for ${basePath}`);
      }
      return response.json();
    }

    function setSelectOptions(selectNode, options, preferredValue = "") {
      if (!(selectNode instanceof HTMLSelectElement)) return "";
      const rows = Array.isArray(options) ? options : [];
      const normalizedRows = rows
        .map((row) => ({
          value: String(row?.value || "").trim(),
          label: String(row?.label || row?.value || "").trim(),
          title: String(row?.title || row?.value || "").trim(),
        }))
        .filter((row) => row.value);
      if (!normalizedRows.length) {
        selectNode.innerHTML = `<option value="">(none)</option>`;
        selectNode.value = "";
        selectNode.disabled = true;
        return "";
      }
      const html = normalizedRows.map((row) =>
        `<option value="${esc(row.value)}" title="${esc(row.title || row.value)}">${esc(row.label || row.value)}</option>`
      ).join("");
      selectNode.innerHTML = html;
      const preferred = String(preferredValue || "").trim();
      const chosen = normalizedRows.some((row) => row.value === preferred)
        ? preferred
        : normalizedRows[0].value;
      selectNode.value = chosen;
      selectNode.disabled = normalizedRows.length <= 1;
      return chosen;
    }

    function showProjectToolsFallback(reason = "") {
      if (!(projectTools instanceof HTMLElement)) return;
      if (!(projectSelect instanceof HTMLSelectElement)
        || !(deviceConfigSelect instanceof HTMLSelectElement)
        || !(patchConfigSelect instanceof HTMLSelectElement)) {
        projectTools.classList.add("hidden");
        return;
      }
      const modelPath = String(selectedModelPath || "").trim();
      const patchPath = String(selectedConnectionsPath || "").trim();
      setSelectOptions(projectSelect, [{
        value: "current",
        label: "Current",
        title: reason || "Current loaded context",
      }], "current");
      setSelectOptions(deviceConfigSelect, modelPath ? [{
        value: modelPath,
        label: labelFromPath(modelPath) || "device-config",
        title: modelPath,
      }] : []);
      setSelectOptions(patchConfigSelect, patchPath ? [{
        value: patchPath,
        label: labelFromPath(patchPath) || "patch-config",
        title: patchPath,
      }] : []);
      projectSelect.disabled = true;
      deviceConfigSelect.disabled = !saveApiEnabled;
      patchConfigSelect.disabled = !saveApiEnabled;
      projectTools.classList.remove("hidden");
    }

    function findProjectByKey(projectKey) {
      const targetKey = String(projectKey || "").trim();
      if (!targetKey) return null;
      return projectCatalog.find((project) => String(project?.key || "").trim() === targetKey) || null;
    }

    function inferProjectKeyFromPaths(modelPathValue, patchPathValue) {
      const modelPath = String(modelPathValue || "").trim();
      const patchPath = String(patchPathValue || "").trim();
      if (!projectCatalog.length) return "";
      for (const project of projectCatalog) {
        const modelFiles = Array.isArray(project?.device_configs) ? project.device_configs : [];
        const patchFiles = Array.isArray(project?.patch_configs) ? project.patch_configs : [];
        if (modelPath && modelFiles.includes(modelPath)) return String(project?.key || "");
        if (patchPath && patchFiles.includes(patchPath)) return String(project?.key || "");
      }
      return "";
    }

    function normalizePathOptions(paths) {
      const values = [];
      const seen = new Set();
      for (const raw of Array.isArray(paths) ? paths : []) {
        const value = String(raw || "").trim();
        if (!value || seen.has(value)) continue;
        seen.add(value);
        values.push(value);
      }
      const duplicateCountByLabel = new Map();
      for (const value of values) {
        const label = labelFromPath(value);
        duplicateCountByLabel.set(label, (duplicateCountByLabel.get(label) || 0) + 1);
      }
      return values.map((value) => {
        const parts = value.split("/");
        const parent = parts.length > 1 ? parts[parts.length - 2] : "";
        const label = labelFromPath(value);
        const duplicate = (duplicateCountByLabel.get(label) || 0) > 1;
        return {
          value,
          label: duplicate && parent ? `${parent}/${label}` : label,
          title: value,
        };
      });
    }

    function stripQueryHash(pathValue) {
      const token = String(pathValue || "").trim();
      if (!token) return "";
      const hashIndex = token.indexOf("#");
      const queryIndex = token.indexOf("?");
      let cutIndex = -1;
      if (hashIndex >= 0 && queryIndex >= 0) cutIndex = Math.min(hashIndex, queryIndex);
      else if (hashIndex >= 0) cutIndex = hashIndex;
      else if (queryIndex >= 0) cutIndex = queryIndex;
      return cutIndex >= 0 ? token.slice(0, cutIndex) : token;
    }

    function toRelativeProjectPath(pathValue) {
      const raw = stripQueryHash(pathValue);
      if (!raw) return "";
      if (hasUrlScheme(raw)) {
        try {
          const parsed = new URL(raw);
          return String(parsed.pathname || "").replace(/^\/+/, "");
        } catch (error) {
          return "";
        }
      }
      let token = raw.replace(/^file:\/+/, "");
      token = token.replace(/^\/+/, "");
      if (token.startsWith("./")) token = token.slice(2);
      return token;
    }

    function parentDirectory(pathValue) {
      const token = toRelativeProjectPath(pathValue);
      if (!token) return "";
      const slash = token.lastIndexOf("/");
      return slash >= 0 ? token.slice(0, slash) : "";
    }

    function joinPathParts(baseDir, fileName) {
      const dir = String(baseDir || "").trim().replace(/\/+$/, "");
      const name = String(fileName || "").trim().replace(/^\/+/, "");
      if (!dir) return name;
      if (!name) return dir;
      return `${dir}/${name}`;
    }

    function sanitizeJsonFileName(inputValue, fallbackStem) {
      const fallback = String(fallbackStem || "config").trim() || "config";
      let stem = stripJsonExtension(String(inputValue || "").trim());
      if (!stem) stem = stripJsonExtension(fallback);
      stem = stem
        .replace(/[^A-Za-z0-9._-]+/g, "-")
        .replace(/-{2,}/g, "-")
        .replace(/^-+/, "")
        .replace(/-+$/, "");
      if (!stem) stem = "config";
      return `${stem}.json`;
    }

    let activeConfigDialogRequest = null;
    let configDialogReturnFocus = null;

    function clientProjectSlug(value) {
      const base = String(value || "")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
      if (!base) return "project";
      const used = new Set(projectCatalog.map((project) => String(project?.key || "").trim()));
      if (!used.has(base)) return base;
      let index = 2;
      while (used.has(`${base}-${String(index).padStart(2, "0")}`)) index += 1;
      return `${base}-${String(index).padStart(2, "0")}`;
    }

    function configDialogFocusableElements() {
      if (!(configDialog instanceof HTMLDialogElement)) return [];
      return Array.from(configDialog.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((node) => node instanceof HTMLElement && !node.closest(".hidden"));
    }

    function closeConfigurationDialog(result) {
      const request = activeConfigDialogRequest;
      activeConfigDialogRequest = null;
      if (configDialog instanceof HTMLDialogElement && configDialog.open) configDialog.close();
      if (request) request.resolve(result);
      const focusTarget = configDialogReturnFocus;
      configDialogReturnFocus = null;
      window.setTimeout(() => {
        if (focusTarget instanceof HTMLElement && focusTarget.isConnected) focusTarget.focus();
      }, 0);
    }

    function updateConfigurationDialog() {
      const request = activeConfigDialogRequest;
      if (!request || !(configDialogInput instanceof HTMLInputElement)) return;
      const rawValue = configDialogInput.value;
      const details = request.options.resolve(rawValue);
      request.details = details;
      const target = String(details?.target || "").trim();
      const error = String(details?.error || "").trim();
      const exists = Boolean(details?.exists);
      configDialogTarget.textContent = target || "Target will appear after entering a valid name.";
      configDialogError.textContent = error;
      configDialogInput.setAttribute("aria-invalid", error ? "true" : "false");
      configDialogOverwrite.classList.toggle("hidden", !exists);
      configDialogOverwriteCheck.checked = false;
      configDialogOverwriteText.textContent = exists
        ? `Replace the existing file at ${target}. The previous contents will be overwritten.`
        : "";
      configDialogSubmit.disabled = Boolean(error) || !target || exists;
    }

    function requestConfiguration(options) {
      if (!(configDialog instanceof HTMLDialogElement)) {
        setStatus("Configuration dialog is unavailable in this browser.", true);
        return Promise.resolve(null);
      }
      if (activeConfigDialogRequest) closeConfigurationDialog(null);
      configDialogReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      configDialogTitle.textContent = String(options?.title || "Configuration");
      configDialogDescription.textContent = String(options?.description || "");
      configDialogInputLabel.textContent = String(options?.inputLabel || "Name");
      configDialogConsequence.textContent = String(options?.consequence || "");
      configDialogSubmit.textContent = String(options?.submitLabel || "Continue");
      configDialogInput.value = String(options?.initialValue || "");
      configDialogOverwrite.classList.add("hidden");
      configDialogOverwriteCheck.checked = false;
      configDialogError.textContent = "";
      return new Promise((resolve) => {
        activeConfigDialogRequest = { options, resolve, details: null };
        updateConfigurationDialog();
        configDialog.showModal();
        window.setTimeout(() => {
          configDialogInput.focus();
          configDialogInput.select();
        }, 0);
      });
    }

    if (configDialogInput) configDialogInput.addEventListener("input", updateConfigurationDialog);
    if (configDialogOverwriteCheck) configDialogOverwriteCheck.addEventListener("change", () => {
      const details = activeConfigDialogRequest?.details;
      configDialogSubmit.disabled = Boolean(details?.error)
        || !String(details?.target || "").trim()
        || (Boolean(details?.exists) && !configDialogOverwriteCheck.checked);
    });
    if (configDialogCancel) configDialogCancel.addEventListener("click", () => closeConfigurationDialog(null));
    if (configDialog) configDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeConfigurationDialog(null);
    });
    if (configDialog) configDialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusable = configDialogFocusableElements();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    if (configDialogForm) configDialogForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const request = activeConfigDialogRequest;
      const details = request?.details;
      if (!request || !details) return;
      if (details.error || !String(details.target || "").trim()) {
        updateConfigurationDialog();
        configDialogInput.focus();
        return;
      }
      if (details.exists && !configDialogOverwriteCheck.checked) {
        configDialogError.textContent = "Confirm that the existing file may be replaced.";
        configDialogOverwriteCheck.focus();
        return;
      }
      closeConfigurationDialog({ ...details, input: configDialogInput.value.trim() });
    });

    function suggestIncrementedName(currentPathValue, existingPaths, fallbackStem) {
      const existingStems = Array.from(
        new Set(
          (Array.isArray(existingPaths) ? existingPaths : [])
            .map((path) => stripJsonExtension(labelFromPath(path)))
            .filter(Boolean)
        )
      );
      const currentStem = stripJsonExtension(labelFromPath(currentPathValue)) || String(fallbackStem || "config");
      const numberMatch = currentStem.match(/^(.*?)([-_ ]?)(\d+)$/);
      let prefix = currentStem;
      let separator = "-";
      let width = 3;
      let pattern = null;

      if (numberMatch) {
        prefix = String(numberMatch[1] || "").trim() || currentStem;
        separator = numberMatch[2] || "-";
        width = Math.max(width, String(numberMatch[3] || "").length);
        const escapedPrefix = escapeRegExp(prefix);
        const escapedSep = escapeRegExp(separator);
        pattern = new RegExp(`^${escapedPrefix}${escapedSep}(\\d+)$`, "i");
      } else {
        const escapedCurrent = escapeRegExp(currentStem);
        pattern = new RegExp(`^${escapedCurrent}[-_ ](\\d+)$`, "i");
      }

      let maxNumber = 0;
      let maxDigits = width;
      for (const stem of existingStems) {
        const match = stem.match(pattern);
        if (!match) continue;
        const value = Number(match[1]);
        if (Number.isFinite(value)) maxNumber = Math.max(maxNumber, value);
        maxDigits = Math.max(maxDigits, String(match[1] || "").length);
      }

      const nextNumber = Math.max(1, maxNumber + 1);
      const nextWidth = Math.max(width, maxDigits);
      return `${prefix}${separator}${String(nextNumber).padStart(nextWidth, "0")}`;
    }

    function requestJsonConfigTarget(options) {
      const existingPaths = Array.isArray(options?.existingPaths) ? options.existingPaths : [];
      const baseDir = String(options?.baseDir || "").trim();
      const fallbackStem = String(options?.fallbackStem || "config").trim() || "config";
      return requestConfiguration({
        title: options?.title,
        description: options?.description,
        consequence: options?.consequence,
        inputLabel: "Filename",
        initialValue: `${fallbackStem}.json`,
        submitLabel: options?.submitLabel || "Save",
        resolve(rawValue) {
          const value = String(rawValue || "").trim();
          let error = "";
          if (!value) error = "Enter a filename.";
          else if (value.includes("/") || value.includes("\\\\")) error = "Enter a filename only, without folders.";
          else if (value === "." || value === "..") error = "Choose a different filename.";
          const fileName = error ? "" : sanitizeJsonFileName(value, fallbackStem);
          const target = fileName ? joinPathParts(baseDir, fileName) : "";
          return {
            value: fileName,
            target,
            exists: Boolean(target && fileExistsInList(target, existingPaths)),
            error,
          };
        },
      });
    }

    function projectOutputPath(baseDir, filename) {
      const dir = String(baseDir || "").trim().replace(/\/+$/, "");
      const file = String(filename || "").trim().replace(/^\/+/, "");
      if (!dir || !file) return "";
      return `${dir}/${file}`;
    }

    function loadProjectSelectionPreference() {
      try {
        const payload = JSON.parse(window.localStorage.getItem(PROJECT_SELECTION_STORAGE_KEY) || "null");
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
        return {
          project_key: String(payload.project_key || "").trim(),
          model_path: String(payload.model_path || "").trim(),
          connections_path: String(payload.connections_path || "").trim(),
        };
      } catch (_error) {
        return {};
      }
    }

    function persistProjectSelectionPreference(projectKey, modelPath, connectionsPath) {
      const payload = {
        project_key: String(projectKey || "").trim(),
        model_path: String(modelPath || "").trim(),
        connections_path: String(connectionsPath || "").trim(),
      };
      if (!payload.project_key) return;
      try {
        window.localStorage.setItem(PROJECT_SELECTION_STORAGE_KEY, JSON.stringify(payload));
      } catch (_error) {
        // Selection remains active for this page when storage is unavailable.
      }
    }

    function updateProjectSelectorsFromConfig(configPayload) {
      const payload = (configPayload && typeof configPayload === "object") ? configPayload : {};
      projectCatalog = Array.isArray(payload.projects) ? payload.projects : [];
      const storedSelection = loadProjectSelectionPreference();
      const configuredProjectKey = String(payload.active_project_key || "").trim();
      selectedProjectKey = String(selectedProjectKey || storedSelection.project_key || configuredProjectKey).trim()
        || inferProjectKeyFromPaths(storedSelection.model_path, storedSelection.connections_path)
        || (projectCatalog[0] && String(projectCatalog[0].key || "").trim())
        || "";
      const storedMatchesProject = String(storedSelection.project_key || "").trim() === selectedProjectKey;
      selectedModelPath = String(
        selectedModelPath
        || (storedMatchesProject ? storedSelection.model_path : "")
        || payload.model_path
        || ""
      ).trim();
      selectedConnectionsPath = String(
        selectedConnectionsPath
        || (storedMatchesProject ? storedSelection.connections_path : "")
        || payload.connections_path
        || ""
      ).trim();

      if (!(projectTools instanceof HTMLElement)) return;
      if (!(projectSelect instanceof HTMLSelectElement)
        || !(deviceConfigSelect instanceof HTMLSelectElement)
        || !(patchConfigSelect instanceof HTMLSelectElement)) {
        projectTools.classList.add("hidden");
        return;
      }

      applyingProjectSelectors = true;
      if (!projectCatalog.length) {
        showProjectToolsFallback("No project catalog from API");
        applyingProjectSelectors = false;
        return;
      }

      const projectOptions = projectCatalog.map((project) => ({
        value: String(project?.key || "").trim(),
        label: String(project?.name || project?.key || "").trim() || String(project?.key || ""),
        title: String(project?.base_path || project?.key || "").trim(),
      })).filter((row) => row.value);
      selectedProjectKey = setSelectOptions(projectSelect, projectOptions, selectedProjectKey);

      const activeProject = findProjectByKey(selectedProjectKey) || projectCatalog[0] || null;
      const deviceOptions = normalizePathOptions(activeProject?.device_configs || []);
      const patchOptions = normalizePathOptions(activeProject?.patch_configs || []);
      selectedModelPath = setSelectOptions(
        deviceConfigSelect,
        deviceOptions,
        selectedModelPath || String(activeProject?.default_device_config || ""),
      );
      selectedConnectionsPath = setSelectOptions(
        patchConfigSelect,
        patchOptions,
        selectedConnectionsPath || String(activeProject?.default_patch_config || ""),
      );

      projectTools.classList.remove("hidden");
      applyingProjectSelectors = false;
      persistProjectSelectionPreference(selectedProjectKey, selectedModelPath, selectedConnectionsPath);
    }

    function buildProjectTargetPayload(project, modelPathValue, patchPathValue) {
      const payload = {};
      const modelPath = String(modelPathValue || "").trim();
      const patchPath = String(patchPathValue || "").trim();
      if (modelPath) payload.model_path = modelPath;
      if (patchPath) payload.connections_path = patchPath;

      const projectBase = String(project?.base_path || "").trim();
      const previewSvgDir = String(project?.output_svg_directory || "").trim()
        || joinPathParts(projectBase, "outputs/svgs");
      const previewDebugDir = String(project?.output_debug_directory || "").trim()
        || joinPathParts(projectBase, "outputs/debug");
      const previewHtmlDir = String(project?.output_html_directory || "").trim()
        || joinPathParts(projectBase, "outputs/html");
      payload.preview_svg_dir = previewSvgDir;
      payload.route_debug_path = projectOutputPath(previewDebugDir, "route-debug.json");
      payload.preview_html = projectOutputPath(previewHtmlDir, "studio_wiring_point_to_point.html");
      payload.routing_rules_path = String(saveApiConfig?.routing_rules_path || "").trim();
      return payload;
    }

    function buildSaveTransactionPayload(project, modelPathValue, patchPathValue, options = {}) {
      const targets = buildProjectTargetPayload(project, modelPathValue, patchPathValue);
      return {
        project_key: String(project?.key || selectedProjectKey || "").trim(),
        targets,
        changes: {
          ...(Object.prototype.hasOwnProperty.call(options, "model") ? { model: options.model } : {}),
          ...(Object.prototype.hasOwnProperty.call(options, "connections") ? { connections: options.connections } : {}),
        },
        expected_hashes: {
          ...(Object.prototype.hasOwnProperty.call(options, "model")
            ? { model: String(options.expectedModelHash || "").trim() } : {}),
          ...(Object.prototype.hasOwnProperty.call(options, "connections")
            ? { connections: String(options.expectedConnectionsHash || "").trim() } : {}),
        },
        reason: String(options.reason || "save"),
        regenerate: options.regenerate !== false,
      };
    }

    async function loadModelAndConnectionsFromSelection(reason = "selection") {
      const modelPath = String(selectedModelPath || "").trim();
      const patchPath = String(selectedConnectionsPath || "").trim();
      if (!modelPath || !patchPath) {
        setStatus("Select both a device config and patch config.", true);
        return false;
      }
      try {
        const [modelPayload, matrixPayloadRaw] = await Promise.all([
          fetchJsonAtPath(modelPath),
          fetchJsonAtPath(patchPath),
        ]);
        if (!modelPayload || typeof modelPayload !== "object") {
          throw new Error("Device config JSON must be an object");
        }
        let nextModel = cloneJson(modelPayload) || {};
        let nextMatrix = normalizeMatrixPayload(cloneJson(matrixPayloadRaw));
        const filtered = filterPatchbaysFromPayloads(nextModel, nextMatrix);
        nextModel = filtered.model;
        nextMatrix = filtered.matrix;

        MODEL = nextModel;
        MATRIX = nextMatrix;
        connections = Array.isArray(nextMatrix?.connections)
          ? nextMatrix.connections
            .map(normalizeConnection)
            .filter((row) => row.source_device && row.source_port && row.dest_device && row.dest_port)
          : [];

        autoOrganizeAllDevicePorts();
        updateDeviceOrderMetadata();
        rebuildModelCaches();
        const removed = pruneConnectionsToKnownPorts();
        setBaselineConnections(connections);
        selectedDeviceName = "";

        const preferredFamily = String(familySelect?.value || "");
        applyUiConfigFromModel();
        initFamilySelect(preferredFamilyFromConfig || preferredFamily || "AUDIO");
        applyDestOrientation();
        applyThemeMode(selectedThemeMode, false);
        applyMatrixScale(false);
        setDevicePortTab(devicePortTab);
        renderDeviceList();
        renderDeviceEditor();
        renderRackEditor();
        renderVisibilityPanel();
        showMatrixSubTab(selectedMatrixSubTab);
        renderMatrix();
        const hashes = computeSaveHashes();
        lastSavedModelHash = hashes.modelHash;
        lastSavedConnectionsHash = hashes.connHash;
        loadedModelVersionHash = await computeJsonPayloadHash(modelPayload);
        loadedConnectionsVersionHash = await computeJsonPayloadHash(matrixPayloadRaw);
        pendingModelEditSave = false;

        const removedSuffix = removed > 0 ? ` (${removed} invalid connection(s) removed)` : "";
        const modelLabel = labelFromPath(modelPath);
        const patchLabel = labelFromPath(patchPath);
        setStatus(`Loaded ${modelLabel} + ${patchLabel}${removedSuffix} [${reason}]`, removed > 0);
        return true;
      } catch (error) {
        setStatus(`Load failed: ${String(error)}`, true);
        return false;
      }
    }

    async function applySelectorTargets(options = {}) {
      if (!saveApiEnabled) return false;
      const modelPath = String(options.modelPath || selectedModelPath || "").trim();
      const patchPath = String(options.patchPath || selectedConnectionsPath || "").trim();
      const projectKey = String(options.projectKey || selectedProjectKey || "").trim();
      const reason = String(options.reason || "selection");
      if (!modelPath || !patchPath) {
        setStatus("Select both a device config and patch config.", true);
        return false;
      }
      try {
        selectedProjectKey = projectKey;
        selectedModelPath = modelPath;
        selectedConnectionsPath = patchPath;
        persistProjectSelectionPreference(selectedProjectKey, selectedModelPath, selectedConnectionsPath);
        return await loadModelAndConnectionsFromSelection(reason);
      } catch (error) {
        setStatus(`Selection load failed: ${String(error)}`, true);
        return false;
      }
    }

    async function postJsonApi(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const details = await parseApiError(response, `HTTP ${response.status}`);
        throw new Error(details);
      }
      const data = await response.json();
      if (!data || data.ok !== true) {
        throw new Error(String(data?.error || `Request failed: ${url}`));
      }
      return data;
    }

    async function fetchApiConfigOnce() {
      const response = await fetch("/api/config", { cache: "no-store" });
      if (!response.ok) {
        const details = await parseApiError(response, `HTTP ${response.status}`);
        throw new Error(details);
      }
      const payload = await response.json();
      if (!payload || payload.ok !== true) {
        throw new Error(String(payload?.error || "Invalid API config response"));
      }
      return payload;
    }

    async function refreshApiConfigState() {
      const payload = await fetchApiConfigOnce();
      saveApiEnabled = true;
      saveApiConfig = payload;
      regenerateApiEnabled = Boolean(payload?.regenerate_available);
      mergePreviewPathsFromConfig(payload);
      mergeRouteDebugPathFromConfig(payload);
      updateProjectSelectorsFromConfig(payload);
      applySaveControlsState();
      return payload;
    }

    function resolveProjectForSelection() {
      const inferred = selectedProjectKey || inferProjectKeyFromPaths(selectedModelPath, selectedConnectionsPath);
      if (inferred) {
        selectedProjectKey = inferred;
      }
      return findProjectByKey(selectedProjectKey);
    }

    function fileExistsInList(pathValue, pathList) {
      const candidate = toRelativeProjectPath(pathValue);
      if (!candidate) return false;
      for (const raw of Array.isArray(pathList) ? pathList : []) {
        if (toRelativeProjectPath(raw) === candidate) return true;
      }
      return false;
    }

    async function expectedHashForTarget(pathValue, pathList) {
      if (!fileExistsInList(pathValue, pathList)) return "";
      return await computeJsonPayloadHash(await fetchJsonAtPath(pathValue));
    }

    function buildEmptyModelPayload(project) {
      const payload = cloneJson(EMPTY_MODEL_TEMPLATE) || {};
      payload.version = Number.isFinite(Number(payload?.version)) ? Number(payload.version) : 1;
      payload.title = String(project?.name || payload?.title || "Studio").trim() || "Studio";
      if (!Array.isArray(payload.devices)) payload.devices = [];
      return payload;
    }

    function buildEmptyPatchPayload() {
      return { version: 1, generated_on: new Date().toISOString(), connections: [] };
    }

    async function createNewProject() {
      if (!saveApiEnabled) {
        setStatus("Create project unavailable. Start routing_matrix_server.py.", true);
        return false;
      }
      const choice = await requestConfiguration({
        title: "Create Project",
        description: "Create a new project from the standard empty project template.",
        consequence: "A new project folder, device configuration, and patch configuration will be created. Existing projects are not changed.",
        inputLabel: "Project name",
        initialValue: "New Project",
        submitLabel: "Create Project",
        resolve(rawValue) {
          const value = String(rawValue || "").trim();
          const error = !value
            ? "Enter a project name."
            : (value.length > 100 ? "Project names must be 100 characters or fewer." : "");
          const key = value ? clientProjectSlug(value) : "";
          return {
            value,
            target: key ? `projects/${key}/` : "",
            exists: false,
            error,
          };
        },
      });
      if (!choice) return false;
      try {
        const payload = await postJsonApi("/api/create-project", { name: choice.value });
        saveApiConfig = payload;
        regenerateApiEnabled = Boolean(payload?.regenerate_available);
        mergePreviewPathsFromConfig(payload);
        mergeRouteDebugPathFromConfig(payload);
        selectedProjectKey = String(payload?.created_project_key || payload?.active_project_key || "").trim();
        selectedModelPath = String(payload?.model_path || "").trim();
        selectedConnectionsPath = String(payload?.connections_path || "").trim();
        updateProjectSelectorsFromConfig(payload);
        if (selectedModelPath && selectedConnectionsPath) {
          await applySelectorTargets({
            projectKey: selectedProjectKey,
            modelPath: selectedModelPath,
            patchPath: selectedConnectionsPath,
            reason: "create-project",
          });
        }
        setStatus(`Created project: ${choice.value}`);
        return true;
      } catch (error) {
        setStatus(`Create project failed: ${String(error)}`, true);
        return false;
      }
    }

    async function createNewDeviceConfig() {
      if (!saveApiEnabled) {
        setStatus("Create device config unavailable. Start routing_matrix_server.py.", true);
        return false;
      }
      const project = resolveProjectForSelection();
      if (!project) {
        setStatus("Select a project first.", true);
        return false;
      }
      const existingPaths = Array.isArray(project?.device_configs) ? project.device_configs : [];
      const baseDir = parentDirectory(project?.default_device_config)
        || joinPathParts(String(project?.base_path || "").trim(), "device-configurations");
      const suggestionStem = suggestIncrementedName("", existingPaths, "device-config");
      const choice = await requestJsonConfigTarget({
        title: "Create Device Configuration",
        description: "Create an empty device-and-port configuration in the selected project.",
        consequence: "The new configuration becomes selected. Your current in-memory devices are not copied; use Save Device Config As for that.",
        submitLabel: "Create Device Config",
        fallbackStem: suggestionStem,
        baseDir,
        existingPaths,
      });
      if (!choice) return false;
      const patchTarget = selectedConnectionsPath || String(project?.default_patch_config || "").trim();
      if (!patchTarget) {
        setStatus("Create a patch configuration for this project first.", true);
        return false;
      }
      try {
        const response = await postJsonApi("/api/save-transaction", buildSaveTransactionPayload(
          project,
          choice.target,
          patchTarget,
          {
            model: buildEmptyModelPayload(project),
            expectedModelHash: await expectedHashForTarget(choice.target, existingPaths),
            reason: "create-device-config",
          },
        ));
        loadedModelVersionHash = String(response?.saved?.hashes?.model || "").trim();
        const configPayload = await fetchApiConfigOnce();
        saveApiConfig = configPayload;
        selectedModelPath = choice.target;
        selectedConnectionsPath = patchTarget;
        updateProjectSelectorsFromConfig({
          ...configPayload,
          active_project_key: selectedProjectKey,
          model_path: choice.target,
          connections_path: patchTarget,
        });
        await applySelectorTargets({
          projectKey: selectedProjectKey,
          modelPath: choice.target,
          patchPath: patchTarget,
          reason: "create-device-config",
        });
        setStatus(`Created device config: ${choice.target}`);
        return true;
      } catch (error) {
        setStatus(`Create device config failed: ${String(error)}`, true);
        return false;
      }
    }

    async function createNewPatchConfig() {
      if (!saveApiEnabled) {
        setStatus("Create patch config unavailable. Start routing_matrix_server.py.", true);
        return false;
      }
      const project = resolveProjectForSelection();
      if (!project || !selectedModelPath) {
        setStatus("Select a project and device config first.", true);
        return false;
      }
      const existingPaths = Array.isArray(project?.patch_configs) ? project.patch_configs : [];
      const modelStem = stripJsonExtension(labelFromPath(selectedModelPath)) || "studio-model";
      const baseDir = joinPathParts(
        joinPathParts(String(project?.base_path || "").trim(), "patch-configurations"),
        modelStem,
      );
      const suggestionStem = suggestIncrementedName(selectedConnectionsPath, existingPaths, "patch-config");
      const choice = await requestJsonConfigTarget({
        title: "Create Patch Configuration",
        description: `Create an empty patch configuration for ${labelFromPath(selectedModelPath)}.`,
        consequence: "The new patch becomes selected with no connections. The device configuration is not changed.",
        submitLabel: "Create Patch Config",
        fallbackStem: suggestionStem,
        baseDir,
        existingPaths,
      });
      if (!choice) return false;
      try {
        const response = await postJsonApi("/api/save-transaction", buildSaveTransactionPayload(
          project,
          selectedModelPath,
          choice.target,
          {
            connections: buildEmptyPatchPayload(),
            expectedConnectionsHash: await expectedHashForTarget(choice.target, existingPaths),
            reason: "create-patch-config",
          },
        ));
        loadedConnectionsVersionHash = String(response?.saved?.hashes?.connections || "").trim();
        const configPayload = await fetchApiConfigOnce();
        saveApiConfig = configPayload;
        selectedConnectionsPath = choice.target;
        updateProjectSelectorsFromConfig({
          ...configPayload,
          active_project_key: selectedProjectKey,
          model_path: selectedModelPath,
          connections_path: choice.target,
        });
        await applySelectorTargets({
          projectKey: selectedProjectKey,
          modelPath: selectedModelPath,
          patchPath: choice.target,
          reason: "create-patch-config",
        });
        setStatus(`Created patch config: ${choice.target}`);
        return true;
      } catch (error) {
        setStatus(`Create patch config failed: ${String(error)}`, true);
        return false;
      }
    }

    async function saveCurrentModelAs() {
      if (!saveApiEnabled) {
        setStatus("Save API unavailable. Start routing_matrix_server.py.", true);
        return false;
      }
      writeUiConfigToModel();
      const project = resolveProjectForSelection();
      const existingPaths = Array.isArray(project?.device_configs) ? project.device_configs : (selectedModelPath ? [selectedModelPath] : []);
      const baseDir = parentDirectory(selectedModelPath)
        || parentDirectory(project?.default_device_config)
        || joinPathParts(String(project?.base_path || "").trim(), "device-configurations");
      const suggestionStem = suggestIncrementedName(selectedModelPath, existingPaths, "device-config");
      const choice = await requestJsonConfigTarget({
        title: "Save Device Configuration As",
        description: "Save the current devices, ports, visibility, and matrix UI settings under a new filename.",
        consequence: "This creates a copy at the displayed path. Your currently selected device and patch configurations remain active.",
        submitLabel: "Save Device Config",
        fallbackStem: suggestionStem,
        baseDir,
        existingPaths,
      });
      if (!choice) return false;
      const nextPath = choice.target;

      try {
        const modelPayload = cloneJson(MODEL) || {};
        const transactionPayload = buildSaveTransactionPayload(
          project,
          nextPath,
          selectedConnectionsPath,
          {
            model: modelPayload,
            expectedModelHash: await expectedHashForTarget(nextPath, existingPaths),
            reason: "save-model-as",
          },
        );
        const transactionResponse = await postJsonApi("/api/save-transaction", transactionPayload);
        loadedModelVersionHash = String(transactionResponse?.saved?.hashes?.model || "").trim();
        const cfg = await fetchApiConfigOnce().catch(() => saveApiConfig);
        saveApiConfig = cfg;
        regenerateApiEnabled = Boolean(cfg?.regenerate_available);
        mergePreviewPathsFromConfig(cfg);
        mergeRouteDebugPathFromConfig(cfg);
        updateProjectSelectorsFromConfig(cfg);
        const hashes = computeSaveHashes();
        lastSavedModelHash = hashes.modelHash;
        pendingModelEditSave = false;
        setStatus(`Saved device config: ${labelFromPath(nextPath)}`);
        return true;
      } catch (error) {
        setStatus(`Save device config failed: ${String(error)}`, true);
        return false;
      }
    }

    async function saveCurrentPatchAs() {
      if (!saveApiEnabled) {
        setStatus("Save API unavailable. Start routing_matrix_server.py.", true);
        return false;
      }
      const project = resolveProjectForSelection();
      const existingPaths = Array.isArray(project?.patch_configs) ? project.patch_configs : (selectedConnectionsPath ? [selectedConnectionsPath] : []);
      const baseDir = parentDirectory(selectedConnectionsPath)
        || parentDirectory(project?.default_patch_config)
        || joinPathParts(String(project?.base_path || "").trim(), "patch-configurations");
      const suggestionStem = suggestIncrementedName(selectedConnectionsPath, existingPaths, "patch-config");
      const choice = await requestJsonConfigTarget({
        title: "Save Patch Configuration As",
        description: "Save the current routing connections under a new filename.",
        consequence: "This creates a copy at the displayed path. Your currently selected device and patch configurations remain active.",
        submitLabel: "Save Patch Config",
        fallbackStem: suggestionStem,
        baseDir,
        existingPaths,
      });
      if (!choice) return false;
      const nextPath = choice.target;

      try {
        const connectionsPayload = buildConnectionsPayload(true);
        const transactionPayload = buildSaveTransactionPayload(
          project,
          selectedModelPath,
          nextPath,
          {
            connections: connectionsPayload,
            expectedConnectionsHash: await expectedHashForTarget(nextPath, existingPaths),
            reason: "save-patch-as",
          },
        );
        const transactionResponse = await postJsonApi("/api/save-transaction", transactionPayload);
        loadedConnectionsVersionHash = String(transactionResponse?.saved?.hashes?.connections || "").trim();
        const cfg = await fetchApiConfigOnce().catch(() => saveApiConfig);
        saveApiConfig = cfg;
        regenerateApiEnabled = Boolean(cfg?.regenerate_available);
        mergePreviewPathsFromConfig(cfg);
        mergeRouteDebugPathFromConfig(cfg);
        updateProjectSelectorsFromConfig(cfg);
        const hashes = computeSaveHashes();
        lastSavedConnectionsHash = hashes.connHash;
        setStatus(`Saved patch config: ${labelFromPath(nextPath)}`);
        return true;
      } catch (error) {
        setStatus(`Save patch config failed: ${String(error)}`, true);
        return false;
      }
    }

    async function detectSaveApi() {
      if (apiDetectInFlight) return;
      apiDetectInFlight = true;
      try {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!payload || payload.ok !== true) throw new Error("Invalid API config response");
        saveApiEnabled = true;
        saveApiConfig = payload;
        regenerateApiEnabled = Boolean(payload?.regenerate_available);
        mergePreviewPathsFromConfig(payload);
        mergeRouteDebugPathFromConfig(payload);
        updateProjectSelectorsFromConfig(payload);
        await loadModelAndConnectionsFromSelection("saved-selection-restore");
      } catch (error) {
        saveApiEnabled = false;
        saveApiConfig = null;
        regenerateApiEnabled = false;
        showProjectToolsFallback("Save API unavailable");
      } finally {
        apiDetectInFlight = false;
      }
      applySaveControlsState();
      refreshVisualPreviews("api-config");
      if (saveApiEnabled) {
        setStatus("Ready. Embedded snapshot loaded + Save API linked. Click matrix cells to create/remove links.");
      } else {
        setStatus("Ready (read-only). Embedded snapshot loaded. Start routing_matrix_server.py for project/device/patch switching and disk save.", true);
      }
      if (autoSaveEnabled && saveApiEnabled) {
        scheduleAutoSave("api-ready");
      }
      if (saveApiEnabled && pendingModelEditSave) {
        saveJsonToDisk("pending-model-edit", true, false).then((ok) => {
          if (ok) pendingModelEditSave = false;
        });
      }
      if (!saveApiEnabled && !apiRetryTimer && window.location.protocol !== "file:") {
        apiRetryTimer = window.setTimeout(() => {
          apiRetryTimer = 0;
          detectSaveApi();
        }, 2500);
      }
    }

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function compactText(value, maxLen = 16) {
      const text = String(value ?? "").trim();
      if (text.length <= maxLen) return text;
      if (maxLen <= 1) return text.slice(0, 1);
      return `${text.slice(0, maxLen - 1).trimEnd()}…`;
    }

    function normalizeSearch(value) {
      return String(value || "").trim().toLowerCase();
    }

    function isGenericSinglePortLabel(value) {
      const normalized = String(value || "")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "");
      if (!normalized) return true;
      return normalized === "in"
        || normalized === "input"
        || normalized === "out"
        || normalized === "output"
        || normalized === "io"
        || normalized === "i/o";
    }

    function entryMatchesFilter(entry, query) {
      if (!query) return true;
      if (entry.kind === "device") {
        const deviceHaystack = `${entry.device} ${entry.memberCount}`.toLowerCase();
        return deviceHaystack.includes(query);
      }
      if (entry.kind === "group") {
        const groupHaystack = `${entry.device} ${entry.groupName} ${entry.memberCount}`.toLowerCase();
        return groupHaystack.includes(query);
      }
      const port = entry.port || {};
      const haystack = `${port.device || ""} ${port.port || ""} ${port.group_name || ""} ${port.transport || ""}`.toLowerCase();
      return haystack.includes(query);
    }

    function normalizeFamily(value) {
      const token = String(value || "").trim().toUpperCase();
      if (token === "ANALOG" || token === "ANALOG AUDIO" || token === "SPEAKER") return "AUDIO";
      if (["DIGITAL", "DIGITAL AUDIO", "MADI", "ADAT", "AES", "SPDIF", "MIDI", "CLOCK", "SYNC"].includes(token)) return "DIGI";
      if (["COMPUTER", "DATA", "VIDEO", "USB", "THUNDERBOLT"].includes(token)) return "COMP";
      if (["NETWORK", "NET", "ETHERNET", "RJ45", "CAT", "CAT5", "CAT6"].includes(token)) return "NETWORK";
      return token;
    }

    function parseBoolLike(value, defaultValue = false) {
      if (typeof value === "boolean") return value;
      if (value == null) return defaultValue;
      const token = String(value).trim().toLowerCase();
      if (!token) return defaultValue;
      if (["1", "true", "yes", "y", "on"].includes(token)) return true;
      if (["0", "false", "no", "n", "off"].includes(token)) return false;
      return defaultValue;
    }

    function normalizeConnection(row) {
      const source = row?.source || {};
      const dest = row?.dest || {};
      const normalizedFamily = normalizeFamily(row?.family || "");
      let normalizedType = String(row?.connection_type || row?.type || row?.tag || "").trim();
      if (normalizedFamily === "AUDIO") {
        const token = normalizedType.toUpperCase();
        if (!token || token === "ST" || token === "STEREO") {
          normalizedType = "MONO";
        }
      }
      return {
        cable_id: String(row?.cable_id || "").trim(),
        family: normalizedFamily,
        source_device: String(row?.source_device || source?.device || "").trim(),
        source_port: String(row?.source_port || source?.port || "").trim(),
        dest_device: String(row?.dest_device || dest?.device || "").trim(),
        dest_port: String(row?.dest_port || dest?.port || "").trim(),
        connection_type: normalizedType,
        status: String(row?.status || "Connected").trim() || "Connected",
        notes: String(row?.notes || "").trim(),
        override_1to1: Boolean(row?.override_1to1 || row?.allow_override || row?.allow_multi),
      };
    }

    const initialConnections = Array.isArray(MATRIX?.connections)
      ? MATRIX.connections.map(normalizeConnection).filter((row) => row.source_device && row.source_port && row.dest_device && row.dest_port)
      : [];
    let baselineConnections = initialConnections.map((row) => ({ ...row }));
    let connections = baselineConnections.map((row) => ({ ...row }));
    const collapsedSourceGroups = new Set();
    const collapsedDestGroups = new Set();
    const collapsedSourceDevices = new Set();
    const collapsedDestDevices = new Set();
    const autoCollapsedFamilies = new Set();
    const UI_CONFIG_VERSION = 1;
    let preferredFamilyFromConfig = "";

    function carryCollapsedKeysBetweenFamilies(keys, previousFamily, nextFamily) {
      const previous = String(previousFamily || "").trim();
      const next = String(nextFamily || "").trim();
      if (!(keys instanceof Set) || !previous || !next || previous === next) return;
      const previousPrefix = `${previous}::`;
      const nextPrefix = `${next}::`;
      const carriedSuffixes = Array.from(keys)
        .filter((key) => String(key || "").startsWith(previousPrefix))
        .map((key) => String(key).slice(previousPrefix.length));
      for (const key of Array.from(keys)) {
        if (String(key || "").startsWith(nextPrefix)) keys.delete(key);
      }
      for (const suffix of carriedSuffixes) keys.add(`${nextPrefix}${suffix}`);
    }

    function carryCollapseStateBetweenFamilies(previousFamily, nextFamily) {
      const previous = String(previousFamily || "").trim();
      const next = String(nextFamily || "").trim();
      if (!previous || !next || previous === next) return;
      carryCollapsedKeysBetweenFamilies(collapsedSourceDevices, previous, next);
      carryCollapsedKeysBetweenFamilies(collapsedDestDevices, previous, next);
      carryCollapsedKeysBetweenFamilies(collapsedSourceGroups, previous, next);
      carryCollapsedKeysBetweenFamilies(collapsedDestGroups, previous, next);
      // Treat the copied state as the destination family's explicit initial
      // state so its first render cannot collapse folders the user left open.
      autoCollapsedFamilies.add(next);
    }

    function setBaselineConnections(rows) {
      baselineConnections = Array.isArray(rows)
        ? rows.map((row) => ({ ...row }))
        : [];
    }

    function toUniqueStringArray(values) {
      if (!Array.isArray(values)) return [];
      const out = [];
      const seen = new Set();
      for (const raw of values) {
        const token = String(raw || "").trim();
        if (!token || seen.has(token)) continue;
        seen.add(token);
        out.push(token);
      }
      return out;
    }

    function readUiConfigFromModel() {
      const candidate = MODEL && typeof MODEL === "object" ? MODEL.ui_config : null;
      return candidate && typeof candidate === "object" ? candidate : null;
    }

    function writeUiConfigToModel() {
      if (!MODEL || typeof MODEL !== "object") MODEL = {};
      const devices = Array.isArray(MODEL.devices) ? MODEL.devices : [];
      const hiddenDeviceNames = [];
      const orderedDeviceNames = [];
      for (const device of devices) {
        const name = String(device?.name || "").trim();
        if (!name) continue;
        orderedDeviceNames.push(name);
        if (!deviceVisibleForTarget(device, "wiring_matrix")) hiddenDeviceNames.push(name);
      }
      MODEL.ui_config = {
        version: UI_CONFIG_VERSION,
        updated_at: new Date().toISOString(),
        theme: {
          mode: String(selectedThemeMode || "light"),
          explicit: Boolean(hasExplicitThemePreference),
        },
        matrix: {
          patch_mode: String(selectedPatchMode || DEFAULT_PATCH_MODE),
          pair_count: normalizePairCount(pairCount),
          type_tag: String(typeTagInput?.value || ""),
          allow_double_patching: Boolean(overrideToggle?.checked),
          sub_tab: String(selectedMatrixSubTab || "patch"),
          collapsed_source_groups: Array.from(collapsedSourceGroups).sort(),
          collapsed_dest_groups: Array.from(collapsedDestGroups).sort(),
          collapsed_source_devices: Array.from(collapsedSourceDevices).sort(),
          collapsed_dest_devices: Array.from(collapsedDestDevices).sort(),
          auto_collapsed_families: Array.from(autoCollapsedFamilies).sort(),
          scale: readScaleControls(),
        },
        editor: {
          selected_device_name: String(selectedDeviceName || ""),
          selected_port_tab: devicePortTab === "out" ? "out" : "in",
        },
        visibility: {
          hidden_devices: hiddenDeviceNames,
          device_order: orderedDeviceNames,
        },
      };
    }

    function applyUiConfigFromModel() {
      const cfg = readUiConfigFromModel();
      if (!cfg) return;

      const matrixCfg = (cfg.matrix && typeof cfg.matrix === "object") ? cfg.matrix : {};
      const editorCfg = (cfg.editor && typeof cfg.editor === "object") ? cfg.editor : {};
      const themeCfg = (cfg.theme && typeof cfg.theme === "object") ? cfg.theme : {};

      preferredFamilyFromConfig = "";
      if (sourceFilterInput) sourceFilterInput.value = "";
      if (destFilterInput) destFilterInput.value = "";
      setPatchMode(matrixCfg.patch_mode || DEFAULT_PATCH_MODE);
      setPairCount(matrixCfg.pair_count);
      if (typeTagInput) typeTagInput.value = String(matrixCfg.type_tag || "");
      if (overrideToggle) overrideToggle.checked = Boolean(matrixCfg.allow_double_patching);

      selectedMatrixSubTab = normalizeMatrixSubTab(matrixCfg.sub_tab || "patch");

      collapsedSourceGroups.clear();
      collapsedDestGroups.clear();
      collapsedSourceDevices.clear();
      collapsedDestDevices.clear();
      autoCollapsedFamilies.clear();
      for (const key of toUniqueStringArray(matrixCfg.collapsed_source_groups)) collapsedSourceGroups.add(key);
      for (const key of toUniqueStringArray(matrixCfg.collapsed_dest_groups)) collapsedDestGroups.add(key);
      for (const key of toUniqueStringArray(matrixCfg.collapsed_source_devices)) collapsedSourceDevices.add(key);
      for (const key of toUniqueStringArray(matrixCfg.collapsed_dest_devices)) collapsedDestDevices.add(key);
      for (const key of toUniqueStringArray(matrixCfg.auto_collapsed_families)) autoCollapsedFamilies.add(key);

      if (matrixCfg.scale && typeof matrixCfg.scale === "object") {
        writeScaleControls(matrixCfg.scale);
      }

      if (themeCfg.mode === "dark" || themeCfg.mode === "light") {
        selectedThemeMode = themeCfg.mode;
      }
      if (typeof themeCfg.explicit === "boolean") {
        hasExplicitThemePreference = Boolean(themeCfg.explicit);
      }

      selectedDeviceName = String(editorCfg.selected_device_name || selectedDeviceName || "");
      devicePortTab = String(editorCfg.selected_port_tab || devicePortTab || "in").toLowerCase() === "out"
        ? "out"
        : "in";
    }

    function familyDefinitions() {
      const modelDefs = (MODEL && typeof MODEL === "object" && MODEL.families && typeof MODEL.families === "object")
        ? MODEL.families
        : {};
      const defaults = {
        AUDIO: { prefix: "AUDIO" },
        COMP: { prefix: "COMP" },
        DIGI: { prefix: "DIGI" },
        NETWORK: { prefix: "NETWORK" },
        POWER: { prefix: "POWER" },
      };
      const merged = { ...defaults };
      for (const [key, cfg] of Object.entries(modelDefs)) {
        const token = normalizeFamily(key);
        if (!merged[token]) merged[token] = {};
        if (cfg && typeof cfg === "object") merged[token] = { ...merged[token], ...cfg };
      }
      return merged;
    }

    function normalizeDirectionToken(directionValue, portName = "") {
      const text = String(directionValue || "").trim().toLowerCase();
      if (text === "in" || text === "input") return "in";
      if (text === "out" || text === "output") return "out";
      if (text === "io" || text === "i/o" || text === "inout" || text === "in/out" || text.includes("bidirectional")) return "io";

      const haystack = `${text} ${String(portName || "").toLowerCase()}`;
      const hasIn = /\bin\b|input|return|recv|receive/.test(haystack);
      const hasOut = /\bout\b|output|send|tx|transmit/.test(haystack);
      if (hasIn && hasOut) return "io";
      if (hasIn) return "in";
      if (hasOut) return "out";
      return "io";
    }

    function compareTextNatural(a, b) {
      return String(a || "").localeCompare(String(b || ""), undefined, { numeric: true, sensitivity: "base" });
    }

    function extractPortSortNumber(name) {
      const text = String(name || "");
      const rangeMatch = text.match(/(\d+)\s*-\s*(\d+)/);
      if (rangeMatch) return Number(rangeMatch[1]);
      const singleMatch = text.match(/(\d+)/);
      if (singleMatch) return Number(singleMatch[1]);
      return 100000;
    }

    function extractRangeSortParts(name) {
      const text = String(name || "");
      const rangeMatch = text.match(/(\d+)\s*-\s*(\d+)/);
      if (!rangeMatch) return [0, 100000, 100000];
      let start = Number(rangeMatch[1]);
      let end = Number(rangeMatch[2]);
      if (end < start) {
        const tmp = start;
        start = end;
        end = tmp;
      }
      const span = Math.max(1, (end - start + 1));
      const broad = span > 4 ? 1 : 0;
      return [broad, start, end];
    }

    function resolvePortName(port) {
      if (port && typeof port === "object" && typeof port.port === "string") return port.port.trim();
      if (port && typeof port === "object" && typeof port.name === "string") return port.name.trim();
      return "";
    }

    function resolvePortOrder(port, fallback = 0) {
      const value = Number(port?.order);
      return Number.isFinite(value) ? value : fallback;
    }

    function resolvePortGroupIndex(port, fallback = 0) {
      const direct = Number(port?.group_index);
      if (Number.isFinite(direct)) return direct;
      const nested = Number(port?.group?.index);
      if (Number.isFinite(nested)) return nested;
      return fallback;
    }

    function genericSignalFlowRank(name, side) {
      const text = String(name || "").toLowerCase();
      const digitalTokens = ["adat", "aes", "madi", "spdif", "s/pdif", "optical", "toslink", "word clock", "clock", "sync", "midi", "digital"];
      const computerTokens = ["usb", "thunderbolt", "ethernet", "rj45", "hdmi", "network"];

      if (side === "in") {
        const comboMicLine = text.includes("mic/line in")
          || text.includes("mic line in")
          || (text.includes("combo") && text.includes("mic") && text.includes("line in"));
        if (comboMicLine) return 2.5;
        if (text.includes("mic")) return 0;
        if (text.includes("inst") || text.includes("instrument") || text.includes("hi-z") || text.includes("hiz") || text.includes("di")) return 1;
        if (text.includes("line in") || text.includes("an in") || text.includes("analog in")) return 2;
        if (text.includes("tape in")) return 3;
        if (text.includes("insert ret") || text.includes("insert return")) return 4;
        if (text.includes("aux in") || text.includes("st aux in") || text.includes("return")) return 5;
        if (
          text.includes("monitor in")
          || text.includes("mon in")
          || text.includes("main in")
          || text.includes("speaker input")
          || text.includes("spk in")
          || text.includes("headphone in")
          || text.includes("phones in")
          || text.includes("ha in")
        ) return 6;
        if (digitalTokens.some((token) => text.includes(token))) return 7;
        if (computerTokens.some((token) => text.includes(token))) return 8;
        return 9;
      }

      if (text.includes("mic out")) return 0;
      if (text.includes("line out") || text.includes("an out") || text.includes("analog out")) return 1;
      if (text.includes("direct out")) return 2;
      if (text.includes("insert send")) return 3;
      if (text.includes("group out") || text.includes("tube out") || (text.includes("aux") && text.includes("out"))) return 4;
      if (text.includes("tape out")) return 5;
      if (text.includes("master out")) return 6;
      if (text.includes("monitor out") || text.includes("mon out")) return 7;
      if (text.includes("headphone out") || text.includes("phones out") || text.includes("speaker out") || text.includes("spk out") || text.includes("hp out")) return 8;
      if (digitalTokens.some((token) => text.includes(token))) return 9;
      if (computerTokens.some((token) => text.includes(token))) return 10;
      return 11;
    }

    function comparePortsForSide(a, b, side, fallbackA = 0, fallbackB = 0) {
      const nameA = resolvePortName(a);
      const nameB = resolvePortName(b);
      const rankA = genericSignalFlowRank(nameA, side);
      const rankB = genericSignalFlowRank(nameB, side);
      if (rankA !== rankB) return rankA - rankB;

      const numA = extractPortSortNumber(nameA);
      const numB = extractPortSortNumber(nameB);
      if (numA !== numB) return numA - numB;

      const groupA = resolvePortGroupIndex(a, numA);
      const groupB = resolvePortGroupIndex(b, numB);
      if (groupA !== groupB) return groupA - groupB;

      const [broadA, startA, endA] = extractRangeSortParts(nameA);
      const [broadB, startB, endB] = extractRangeSortParts(nameB);
      if (broadA !== broadB) return broadA - broadB;
      if (startA !== startB) return startA - startB;
      if (endA !== endB) return endA - endB;

      const orderA = resolvePortOrder(a, fallbackA);
      const orderB = resolvePortOrder(b, fallbackB);
      if (orderA !== orderB) return orderA - orderB;
      return compareTextNatural(nameA, nameB);
    }

    function comparePortsForModel(a, b, fallbackA = 0, fallbackB = 0) {
      const dirA = normalizeDirectionToken(a?.direction, a?.name);
      const dirB = normalizeDirectionToken(b?.direction, b?.name);
      const rankByDir = { in: 0, io: 1, out: 2 };
      const dirRankA = Number(rankByDir[dirA] ?? 3);
      const dirRankB = Number(rankByDir[dirB] ?? 3);
      if (dirRankA !== dirRankB) return dirRankA - dirRankB;

      const side = dirA === "out" && dirB === "out" ? "out" : "in";
      const sideCmp = comparePortsForSide(a, b, side, fallbackA, fallbackB);
      if (sideCmp !== 0) return sideCmp;
      return compareTextNatural(String(a?.name || ""), String(b?.name || ""));
    }

    function autoOrganizePortsOnDevice(device) {
      const ports = ensureDevicePortsArray(device);
      if (!ports.length) return false;
      for (const port of ports) {
        port.direction = normalizeDirectionToken(port?.direction, port?.name);
      }
      const indexed = ports.map((port, idx) => ({ port, idx }));
      indexed.sort((a, b) => comparePortsForModel(a.port, b.port, a.idx, b.idx));
      const reordered = indexed.map((entry) => entry.port);
      let changed = false;
      if (reordered.length === ports.length) {
        for (let idx = 0; idx < reordered.length; idx += 1) {
          if (ports[idx] !== reordered[idx]) {
            changed = true;
            break;
          }
        }
      }
      device.ports = reordered;
      for (let idx = 0; idx < device.ports.length; idx += 1) {
        const port = device.ports[idx];
        if (Number(port?.order) !== idx) {
          port.order = idx;
          changed = true;
        }
      }
      return changed;
    }

    function autoOrganizeAllDevicePorts() {
      const devices = ensureModelDeviceArray();
      let changed = false;
      for (const device of devices) {
        if (autoOrganizePortsOnDevice(device)) changed = true;
      }
      return changed;
    }

    function buildNormalizedPort(deviceName, port, order) {
      const families = Array.isArray(port?.families)
        ? port.families.map((f) => normalizeFamily(f)).filter(Boolean)
        : [];
      const hidden = parseBoolLike(port?.hidden, false);
      const visible = !hidden && parseBoolLike(port?.visible, true);
      const enabled = parseBoolLike(port?.enabled, true) && !parseBoolLike(port?.disabled, false);
      const group = (port && typeof port === "object" && port.group && typeof port.group === "object")
        ? port.group
        : {};
      const groupIndexRaw = Number(group.index);
      const groupSizeRaw = Number(group.size);
      return {
        device: deviceName,
        port: String(port?.name || "").trim(),
        direction: normalizeDirectionToken(port?.direction, port?.name),
        families,
        transport: String(port?.transport || "").trim(),
        order: Number.isFinite(Number(port?.order)) ? Number(port.order) : order,
        group_name: String(group?.name || "").trim(),
        group_member: String(group?.member || "").trim(),
        group_index: Number.isFinite(groupIndexRaw) ? groupIndexRaw : order + 1,
        group_size: Number.isFinite(groupSizeRaw) ? groupSizeRaw : null,
        visible,
        enabled,
      };
    }

    let famDefs = {};
    let modelDevices = [];
    let portsByDevice = new Map();

    function rebuildModelCaches() {
      famDefs = familyDefinitions();
      modelDevices = Array.isArray(MODEL?.devices) ? MODEL.devices : [];
      portsByDevice = new Map();
      for (const device of modelDevices) {
        const name = String(device?.name || "").trim();
        if (!name) continue;
        const ports = Array.isArray(device?.ports) ? device.ports : [];
        portsByDevice.set(
          name,
          ports
            .map((port, order) => buildNormalizedPort(name, port, order))
            .filter((port) => port.port),
        );
      }
    }

    autoOrganizeAllDevicePorts();
    updateDeviceOrderMetadata();
    rebuildModelCaches();

    function portsForFamily(family, sourceSide) {
      const output = [];
      for (const device of modelDevices) {
        if (!deviceVisibleForTarget(device, "wiring_matrix")) continue;
        const deviceName = String(device?.name || "").trim();
        if (!deviceName) continue;
        if (isPatchbayDeviceName(deviceName)) continue;
        const ports = portsByDevice.get(deviceName) || [];
        const selected = [];
        for (const port of ports) {
          if (!port.visible) continue;
          const supportsFamily = family === FAMILY_ALL
            ? port.families.length > 0
            : port.families.includes(family);
          if (!supportsFamily) continue;
          const dir = port.direction;
          if (sourceSide) {
            if (!(dir === "out" || dir === "io")) continue;
          } else if (!(dir === "in" || dir === "io")) {
            continue;
          }
          selected.push(port);
        }
        selected.sort((a, b) => comparePortsForSide(a, b, sourceSide ? "out" : "in"));
        output.push(...selected);
      }
      return output;
    }

    function sharedFamiliesForPorts(sourcePort, destPort) {
      const src = Array.isArray(sourcePort?.families) ? sourcePort.families : [];
      const dst = Array.isArray(destPort?.families) ? destPort.families : [];
      return FAMILY_ORDER.filter((family) => src.includes(family) && dst.includes(family));
    }

    function digitalProtocolForPort(port) {
      const transport = String(port?.transport || "").trim().toUpperCase();
      const portName = String(port?.port || port?.name || "").trim().toUpperCase();
      const classify = (value) => {
        const token = String(value || "").toUpperCase();
        if (/MADI/.test(token)) {
          if (/OPT|OPTICAL|FIBER|FIBRE/.test(token)) return "MADI-OPTICAL";
          if (/COAX|BNC/.test(token) || token.trim() === "MADI") return "MADI-COAX";
          return "MADI";
        }
        if (/S\/?P[.-]?DIF|SPDIF/.test(token)) return "SPDIF";
        if (/ADAT|SMUX/.test(token)) return "ADAT";
        if (/AES(?:\/EBU)?/.test(token)) return "AES";
        if (/WORD\s*CLOCK|(^|[^A-Z])CLOCK([^A-Z]|$)/.test(token)) return "WORD-CLOCK";
        if (/MIDI/.test(token)) return "MIDI";
        if (/DANTE/.test(token)) return "DANTE";
        if (/HDMI/.test(token)) return "HDMI";
        return "";
      };
      return classify(transport) || classify(portName);
    }

    function supportsTransportCompatibilityForFamily(family, sourcePort, destPort) {
      if (normalizeFamily(family) !== "DIGI") return true;
      const sourceProtocol = digitalProtocolForPort(sourcePort);
      const destProtocol = digitalProtocolForPort(destPort);
      return Boolean(sourceProtocol && destProtocol && sourceProtocol === destProtocol);
    }

    function resolveLinkFamily(selectedFamily, sourcePort, destPort) {
      const shared = sharedFamiliesForPorts(sourcePort, destPort);
      if (!shared.length) return "";
      const candidates = selectedFamily === FAMILY_ALL
        ? shared
        : (shared.includes(selectedFamily) ? [selectedFamily] : []);
      return candidates.find((family) => supportsTransportCompatibilityForFamily(family, sourcePort, destPort)) || "";
    }

    function linkCompatibilityReason(selectedFamily, sourcePort, destPort) {
      const shared = sharedFamiliesForPorts(sourcePort, destPort);
      if (!shared.length) return "Incompatible port families";
      const candidates = selectedFamily === FAMILY_ALL
        ? shared
        : (shared.includes(selectedFamily) ? [selectedFamily] : []);
      if (!candidates.length) return `No shared family for ${selectedFamily}`;
      if (candidates.includes("DIGI")) {
        const sourceProtocol = digitalProtocolForPort(sourcePort) || "unknown";
        const destProtocol = digitalProtocolForPort(destPort) || "unknown";
        return `DIGI protocol mismatch: ${sourceProtocol} -> ${destProtocol}`;
      }
      return "Unavailable connection";
    }

    function powerConnectorAdvisory(linkFamily, sourcePort, destPort) {
      if (linkFamily !== "POWER") return "";
      const sourceType = String(sourcePort?.transport || "").trim();
      const destType = String(destPort?.transport || "").trim();
      if (
        !sourceType
        || !destType
        || normalizedPowerSourceTransport(sourceType) === normalizedPowerSourceTransport(destType)
      ) return "";
      return `POWER connector check: ${sourceType} → ${destType}. Connection allowed; use the correct power cable or adapter.`;
    }

    function powerPortBadge(port) {
      const families = Array.isArray(port?.families) ? port.families : [];
      if (!families.includes("POWER")) return "";
      return '<span class="port-family-badge" title="POWER family port" aria-label="POWER family port">PWR</span>';
    }

    function buildGroupKey(family, axis, device, groupName) {
      const normalizedGroup = String(groupName || "").trim().toLowerCase();
      return `${family}::${axis}::${device}::${normalizedGroup}`;
    }

    function buildDeviceKey(family, axis, device) {
      return `${family}::${axis}::${String(device || "").trim()}`;
    }

    function buildAxisEntries(family, ports, axis, collapsedGroupSet, collapsedDeviceSet) {
      const groups = new Map();
      const deviceMembers = new Map();
      const deviceGroupNames = new Map();
      for (const port of ports) {
        const deviceName = String(port?.device || "").trim();
        if (!deviceMembers.has(deviceName)) deviceMembers.set(deviceName, []);
        deviceMembers.get(deviceName).push(port);

        const groupName = String(port?.group_name || "").trim();
        if (!groupName) continue;
        if (!deviceGroupNames.has(deviceName)) deviceGroupNames.set(deviceName, new Set());
        deviceGroupNames.get(deviceName).add(groupName);
        const key = buildGroupKey(family, axis, port.device, groupName);
        if (!groups.has(key)) {
          groups.set(key, {
            key,
            axis,
            device: port.device,
            groupName,
            members: [],
          });
        }
        groups.get(key).members.push(port);
      }
      for (const group of groups.values()) {
        group.members.sort((a, b) => {
          const idxA = Number.isFinite(Number(a.group_index)) ? Number(a.group_index) : Number(a.order);
          const idxB = Number.isFinite(Number(b.group_index)) ? Number(b.group_index) : Number(b.order);
          if (idxA !== idxB) return idxA - idxB;
          return Number(a.order) - Number(b.order);
        });
      }
      const singleGroupDevices = new Set();
      for (const [deviceName, groupNames] of deviceGroupNames.entries()) {
        if ((groupNames?.size || 0) <= 1) singleGroupDevices.add(deviceName);
      }

      const entries = [];
      const emittedCollapsed = new Set();
      for (const port of ports) {
        const groupName = String(port?.group_name || "").trim();
        if (!groupName) {
          entries.push({ kind: "port", axis, port });
          continue;
        }
        const key = buildGroupKey(family, axis, port.device, groupName);
        const group = groups.get(key);
        const memberCount = Array.isArray(group?.members) ? group.members.length : 0;
        const collapsible = memberCount > 1 && !singleGroupDevices.has(String(port?.device || "").trim());
        if (!collapsible) {
          entries.push({ kind: "port", axis, port });
          continue;
        }
        if (collapsedGroupSet.has(key)) {
          if (emittedCollapsed.has(key)) continue;
          emittedCollapsed.add(key);
          entries.push({
            kind: "group",
            axis,
            groupKey: key,
            groupName,
            device: port.device,
            members: group.members,
            memberCount,
          });
          continue;
        }
        entries.push({
          kind: "port",
          axis,
          port,
          groupKey: key,
          groupName,
          memberCount,
          collapsible,
        });
      }

      const deviceCollapsedEntries = [];
      const emittedCollapsedDevices = new Set();
      for (const entry of entries) {
        const deviceName = entry.kind === "port" ? entry.port.device : entry.device;
        const deviceKey = buildDeviceKey(family, axis, deviceName);
        const members = Array.isArray(deviceMembers.get(deviceName)) ? deviceMembers.get(deviceName) : [];
        const deviceMemberCount = members.length;
        const deviceCollapsible = deviceMemberCount > 1;

        if (deviceCollapsible && collapsedDeviceSet.has(deviceKey)) {
          if (emittedCollapsedDevices.has(deviceKey)) continue;
          emittedCollapsedDevices.add(deviceKey);
          deviceCollapsedEntries.push({
            kind: "device",
            axis,
            device: deviceName,
            deviceKey,
            members,
            memberCount: deviceMemberCount,
            deviceCollapsible: true,
          });
          continue;
        }

        deviceCollapsedEntries.push({
          ...entry,
          device: deviceName,
          deviceKey,
          deviceMemberCount,
          deviceCollapsible,
        });
      }
      return deviceCollapsedEntries;
    }

    function collectCollapsibleGroupKeys(family, sourceSide) {
      const axis = sourceSide ? "source" : "dest";
      const counts = new Map();
      const keyToDevice = new Map();
      const deviceGroupNames = new Map();
      for (const port of portsForFamily(family, sourceSide)) {
        const deviceName = String(port?.device || "").trim();
        const groupName = String(port?.group_name || "").trim();
        if (!groupName) continue;
        const key = buildGroupKey(family, axis, port.device, groupName);
        counts.set(key, (counts.get(key) || 0) + 1);
        keyToDevice.set(key, deviceName);
        if (!deviceGroupNames.has(deviceName)) deviceGroupNames.set(deviceName, new Set());
        deviceGroupNames.get(deviceName).add(groupName);
      }
      const keys = [];
      for (const [key, count] of counts.entries()) {
        const deviceName = keyToDevice.get(key) || "";
        const groupCount = deviceGroupNames.get(deviceName)?.size || 0;
        if (count > 1 && groupCount > 1) keys.push(key);
      }
      return keys;
    }

    function collectCollapsibleDeviceKeys(family, sourceSide) {
      const axis = sourceSide ? "source" : "dest";
      const counts = new Map();
      for (const port of portsForFamily(family, sourceSide)) {
        const key = buildDeviceKey(family, axis, port.device);
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      const keys = [];
      for (const [key, count] of counts.entries()) {
        if (count > 1) keys.push(key);
      }
      return keys;
    }

    function clearCollapsedGroupsForFamily(family) {
      if (family === FAMILY_ALL) {
        collapsedSourceGroups.clear();
        collapsedDestGroups.clear();
        return;
      }
      const prefix = `${family}::`;
      for (const key of Array.from(collapsedSourceGroups)) {
        if (key.startsWith(prefix)) collapsedSourceGroups.delete(key);
      }
      for (const key of Array.from(collapsedDestGroups)) {
        if (key.startsWith(prefix)) collapsedDestGroups.delete(key);
      }
    }

    function clearCollapsedDevicesForFamily(family) {
      if (family === FAMILY_ALL) {
        collapsedSourceDevices.clear();
        collapsedDestDevices.clear();
        return;
      }
      const prefix = `${family}::`;
      for (const key of Array.from(collapsedSourceDevices)) {
        if (key.startsWith(prefix)) collapsedSourceDevices.delete(key);
      }
      for (const key of Array.from(collapsedDestDevices)) {
        if (key.startsWith(prefix)) collapsedDestDevices.delete(key);
      }
    }

    function ensureDefaultCollapsedForFamily(family) {
      if (autoCollapsedFamilies.has(family)) return;
      for (const key of collectCollapsibleGroupKeys(family, true)) collapsedSourceGroups.add(key);
      for (const key of collectCollapsibleGroupKeys(family, false)) collapsedDestGroups.add(key);
      autoCollapsedFamilies.add(family);
    }

    function endpointKey(device, port) {
      return `${device}::${port}`;
    }

    function findConnectionIndex(family, sourceDevice, sourcePort, destDevice, destPort) {
      return connections.findIndex((conn) =>
        (family === FAMILY_ALL || conn.family === family)
        && conn.source_device === sourceDevice
        && conn.source_port === sourcePort
        && conn.dest_device === destDevice
        && conn.dest_port === destPort
      );
    }

    function findStrictConflict(sourceDevice, sourcePort, destDevice, destPort) {
      const srcKey = endpointKey(sourceDevice, sourcePort);
      const dstKey = endpointKey(destDevice, destPort);
      for (const conn of connections) {
        if (conn.override_1to1) continue;
        const cSrc = endpointKey(conn.source_device, conn.source_port);
        const cDst = endpointKey(conn.dest_device, conn.dest_port);
        if (cSrc === srcKey || cDst === srcKey || cSrc === dstKey || cDst === dstKey) {
          return conn;
        }
      }
      return null;
    }

    function connectionsForEndpoint(device, port) {
      const key = endpointKey(device, port);
      return connections.filter((conn) => (
        endpointKey(conn.source_device, conn.source_port) === key
        || endpointKey(conn.dest_device, conn.dest_port) === key
      ));
    }

    function normalizedPowerSourceTransport(value) {
      const compact = String(value || "").trim().toUpperCase().replace(/[^A-Z0-9]+/g, "");
      if (/^(SCHUKO|TYPEF|CEE7|CEE77)$/.test(compact)) return "SCHUKO";
      const iecMatch = compact.match(/^(?:IEC)?C(5|6|7|8|13|14|19|20)$/);
      if (iecMatch) return `C${iecMatch[1]}`;
      return compact;
    }

    function deviceSuppliesPower(deviceName) {
      const ports = portsByDevice.get(String(deviceName || "").trim()) || [];
      return ports.some((port) => (
        port.visible
        && port.enabled
        && (port.direction === "out" || port.direction === "io")
        && Array.isArray(port.families)
        && port.families.includes("POWER")
      ));
    }

    function movablePowerConnection(source, dest) {
      if (!source?.enabled || !dest?.enabled || deviceSuppliesPower(dest.device)) return null;
      const sourceConflicts = connectionsForEndpoint(source.device, source.port);
      const destConflicts = connectionsForEndpoint(dest.device, dest.port);
      if (sourceConflicts.length !== 0 || destConflicts.length !== 1) return null;
      const row = destConflicts[0];
      if (
        normalizeFamily(row?.family) !== "POWER"
        || Boolean(row?.override_1to1)
        || row?.dest_device !== dest.device
        || row?.dest_port !== dest.port
      ) return null;
      return row;
    }

    function powerMoveHoverTitle(source, dest, linkFamily) {
      if (selectedPatchMode !== "single" || linkFamily !== "POWER") return "";
      const row = movablePowerConnection(source, dest);
      if (!row) return "";
      return `Click to move POWER from ${row.source_device} [${row.source_port}] to ${source.device} [${source.port}] for ${dest.device}`;
    }

    function ensureModelDeviceArray() {
      if (!MODEL || typeof MODEL !== "object") MODEL = {};
      if (!Array.isArray(MODEL.devices)) MODEL.devices = [];
      return MODEL.devices;
    }

    function updateDeviceOrderMetadata() {
      const devices = ensureModelDeviceArray();
      for (let idx = 0; idx < devices.length; idx += 1) {
        const device = devices[idx];
        if (!device || typeof device !== "object") continue;
        device.matrix_order = idx;
      }
    }

    function devicesInMatrixOrder() {
      const devices = ensureModelDeviceArray().slice();
      devices.sort((a, b) => {
        const orderA = Number(a?.matrix_order);
        const orderB = Number(b?.matrix_order);
        const hasA = Number.isFinite(orderA);
        const hasB = Number.isFinite(orderB);
        if (hasA && hasB && orderA !== orderB) return orderA - orderB;
        if (hasA && !hasB) return -1;
        if (!hasA && hasB) return 1;
        return 0;
      });
      return devices;
    }

    function sortDevicesInModel() {
      const devices = ensureModelDeviceArray();
      devices.sort((a, b) => {
        const orderA = Number(a?.matrix_order);
        const orderB = Number(b?.matrix_order);
        const hasA = Number.isFinite(orderA);
        const hasB = Number.isFinite(orderB);
        if (hasA && hasB && orderA !== orderB) return orderA - orderB;
        if (hasA && !hasB) return -1;
        if (!hasA && hasB) return 1;
        return String(a?.name || "").localeCompare(String(b?.name || ""), undefined, { numeric: true });
      });
      updateDeviceOrderMetadata();
    }

    function getDeviceByName(name) {
      const devices = ensureModelDeviceArray();
      const needle = String(name || "").trim();
      return devices.find((device) => String(device?.name || "").trim() === needle) || null;
    }

    function isDeviceVisible(device) {
      if (!device || typeof device !== "object") return false;
      if (Boolean(device.hidden)) return false;
      if (device.visible === false) return false;
      return true;
    }

    function deviceVisibleForTarget(device, targetName) {
      if (!device || typeof device !== "object") return false;
      const visibility = device.visibility;
      if (
        visibility
        && typeof visibility === "object"
        && !Array.isArray(visibility)
        && typeof visibility[targetName] === "boolean"
      ) {
        return visibility[targetName];
      }
      return isDeviceVisible(device);
    }

    function setDeviceVisibilityForTarget(device, targetName, visible) {
      if (!device || typeof device !== "object") return false;
      if (!DEVICE_VISIBILITY_TARGETS.some((target) => target.key === targetName)) return false;
      if (!device.visibility || typeof device.visibility !== "object" || Array.isArray(device.visibility)) {
        device.visibility = {};
      }
      const next = Boolean(visible);
      const changed = deviceVisibleForTarget(device, targetName) !== next
        || device.visibility[targetName] !== next;
      device.visibility[targetName] = next;
      return changed;
    }

    function visibleDeviceNameSet(targetName = "wiring_matrix") {
      const visible = new Set();
      for (const device of modelDevices) {
        if (!deviceVisibleForTarget(device, targetName)) continue;
        const name = String(device?.name || "").trim();
        if (name) visible.add(name);
      }
      return visible;
    }

    function ensureDevicePortsArray(device) {
      if (!device || typeof device !== "object") return [];
      if (!Array.isArray(device.ports)) device.ports = [];
      return device.ports;
    }

    function endpointExists(deviceName, portName) {
      const ports = portsByDevice.get(String(deviceName || "").trim()) || [];
      return ports.some((port) => port.port === String(portName || "").trim());
    }

    function findPortMeta(deviceName, portName) {
      const deviceToken = String(deviceName || "").trim();
      const portToken = String(portName || "").trim();
      const ports = portsByDevice.get(deviceToken) || [];
      for (const port of ports) {
        if (String(port?.port || "").trim() === portToken) return port;
      }
      return null;
    }

    function endpointVisible(deviceName, portName) {
      const port = findPortMeta(deviceName, portName);
      return Boolean(port && port.visible);
    }

    function endpointEnabled(deviceName, portName) {
      const port = findPortMeta(deviceName, portName);
      return Boolean(port && port.visible && port.enabled);
    }

    function pruneConnectionsToKnownPorts() {
      const before = connections.length;
      connections = connections.filter((conn) =>
        endpointExists(conn.source_device, conn.source_port)
        && endpointExists(conn.dest_device, conn.dest_port)
      );
      return before - connections.length;
    }

    function guessTypeTag(family, sourcePort, destPort) {
      const srcTransport = String(sourcePort?.transport || "").trim().toUpperCase();
      const dstTransport = String(destPort?.transport || "").trim().toUpperCase();
      if (srcTransport && srcTransport === dstTransport) return srcTransport;
      const both = `${srcTransport} ${dstTransport}`;
      if (family === "POWER") {
        const sourceConnector = normalizedPowerSourceTransport(srcTransport);
        const destinationConnector = normalizedPowerSourceTransport(dstTransport);
        const inletToCableConnector = {
          C6: "C5",
          C8: "C7",
          C14: "C13",
          C20: "C19",
        };
        const cableDestination = inletToCableConnector[destinationConnector] || destinationConnector;
        if (sourceConnector && cableDestination) {
          return sourceConnector === cableDestination
            ? sourceConnector
            : `${sourceConnector}–${cableDestination}`;
        }
        return sourceConnector || cableDestination || "";
      }
      if (family === "COMP") {
        if (both.includes("HDMI")) return "HDMI";
        if (both.includes("THUNDERBOLT") || both.includes("TB")) return "TB4";
        if (both.includes("USB")) return "USB";
      }
      if (family === "NETWORK") {
        if (both.includes("CAT6")) return "CAT6";
        if (both.includes("CAT5")) return "CAT5";
        if (both.includes("ETH") || both.includes("RJ45")) return "ETH";
      }
      if (family === "DIGI") {
        if (both.includes("MADI")) return "MADI";
        if (both.includes("SPDIF")) return "SPDIF";
        if (both.includes("ADAT")) return "ADAT";
        if (both.includes("AES")) return "AES";
      }
      if (family === "AUDIO") {
        return "MONO";
      }
      return "";
    }

    function parseCableIdParts(cableId) {
      const match = String(cableId || "").trim().match(/^([A-Za-z]+)-?(\d+)$/);
      if (!match) return null;
      return { prefix: match[1].toUpperCase(), number: Number(match[2]), width: match[2].length };
    }

    function familySortIndex(family) {
      const token = normalizeFamily(family || "");
      const idx = FAMILY_ORDER.indexOf(token);
      return idx >= 0 ? idx : (FAMILY_ORDER.length + 1);
    }

    function sortedConnectionsForIdSequence(rows) {
      const indexed = rows.map((row, index) => ({ row: { ...row }, index }));
      indexed.sort((a, b) => {
        const rowA = a.row || {};
        const rowB = b.row || {};

        const famCmp = familySortIndex(rowA.family) - familySortIndex(rowB.family);
        if (famCmp !== 0) return famCmp;

        const srcDevCmp = compareTextNatural(rowA.source_device, rowB.source_device);
        if (srcDevCmp !== 0) return srcDevCmp;

        const srcMetaA = findPortMeta(rowA.source_device, rowA.source_port) || { port: rowA.source_port, direction: "out" };
        const srcMetaB = findPortMeta(rowB.source_device, rowB.source_port) || { port: rowB.source_port, direction: "out" };
        const srcPortCmp = comparePortsForModel(srcMetaA, srcMetaB, a.index, b.index);
        if (srcPortCmp !== 0) return srcPortCmp;

        const dstDevCmp = compareTextNatural(rowA.dest_device, rowB.dest_device);
        if (dstDevCmp !== 0) return dstDevCmp;

        const dstMetaA = findPortMeta(rowA.dest_device, rowA.dest_port) || { port: rowA.dest_port, direction: "in" };
        const dstMetaB = findPortMeta(rowB.dest_device, rowB.dest_port) || { port: rowB.dest_port, direction: "in" };
        const dstPortCmp = comparePortsForModel(dstMetaA, dstMetaB, a.index, b.index);
        if (dstPortCmp !== 0) return dstPortCmp;

        return a.index - b.index;
      });
      return indexed.map((entry) => entry.row);
    }

    function resolveCableIds(rows) {
      const list = sortedConnectionsForIdSequence(rows);
      const nextByPrefix = {};
      const widthByPrefix = {};
      for (const row of list) {
        const family = normalizeFamily(row.family || "");
        const prefix = String(famDefs?.[family]?.prefix || family || "WIRE").toUpperCase();
        const parsed = parseCableIdParts(row.cable_id);
        const parsedWidth = parsed && parsed.prefix === prefix ? Number(parsed.width || 0) : 0;
        widthByPrefix[prefix] = Math.max(3, widthByPrefix[prefix] || 3, parsedWidth);
      }
      for (const row of list) {
        const family = normalizeFamily(row.family || "");
        const prefix = String(famDefs?.[family]?.prefix || family || "WIRE").toUpperCase();
        const nextNum = (nextByPrefix[prefix] || 0) + 1;
        nextByPrefix[prefix] = nextNum;
        const width = Math.max(3, Number(widthByPrefix[prefix] || 3));
        row.cable_id = `${prefix}-${String(nextNum).padStart(width, "0")}`;
      }
      return list;
    }

    function normalizeConnectionIdsInState() {
      const resolved = resolveCableIds(connections);
      if (!Array.isArray(resolved) || resolved.length !== connections.length) {
        connections = Array.isArray(resolved) ? resolved : [];
        return;
      }
      let changed = false;
      for (let idx = 0; idx < resolved.length; idx += 1) {
        const prev = connections[idx] || {};
        const next = resolved[idx] || {};
        if (
          String(prev.cable_id || "") !== String(next.cable_id || "")
          || String(prev.family || "") !== String(next.family || "")
          || String(prev.source_device || "") !== String(next.source_device || "")
          || String(prev.source_port || "") !== String(next.source_port || "")
          || String(prev.dest_device || "") !== String(next.dest_device || "")
          || String(prev.dest_port || "") !== String(next.dest_port || "")
        ) {
          changed = true;
          break;
        }
      }
      if (changed) {
        connections = resolved;
      }
    }

    function normalizePatchAction(action) {
      const token = String(action || "toggle").trim().toLowerCase();
      if (token === "connect" || token === "disconnect" || token === "toggle") return token;
      return "toggle";
    }

    function performPatchAction(selectedFamily, sourcePort, destPort, requestedAction = "toggle", options = {}) {
      const source = sourcePort && typeof sourcePort === "object" ? sourcePort : null;
      const dest = destPort && typeof destPort === "object" ? destPort : null;
      if (!source || !dest) {
        return { changed: false, error: "Invalid source/destination port." };
      }
      if (!source.enabled || !dest.enabled) {
        return {
          changed: false,
          error: `Disabled port: ${source.device} [${source.port}] -> ${dest.device} [${dest.port}] is read-only.`,
        };
      }
      const sourceDirection = String(source.direction || "").trim().toLowerCase();
      const destDirection = String(dest.direction || "").trim().toLowerCase();
      if (!(sourceDirection === "out" || sourceDirection === "io") || !(destDirection === "in" || destDirection === "io")) {
        return {
          changed: false,
          error: `Invalid port directions: ${source.device} [${source.port}] must be an output and ${dest.device} [${dest.port}] must be an input.`,
        };
      }
      const linkFamily = resolveLinkFamily(selectedFamily, source, dest);
      let action = normalizePatchAction(requestedAction);
      const existingIdx = findConnectionIndex(
        selectedFamily,
        source.device,
        source.port,
        dest.device,
        dest.port,
      );
      const connected = existingIdx >= 0;
      if (action === "toggle") {
        action = connected ? "disconnect" : "connect";
      }

      if (action === "disconnect") {
        if (!connected) return { changed: false, action, linkFamily };
        const removed = connections[existingIdx];
        connections.splice(existingIdx, 1);
        return { changed: true, action, linkFamily, row: removed };
      }

      if (!linkFamily) {
        return {
          changed: false,
          action,
          error: `${linkCompatibilityReason(selectedFamily, source, dest)}: ${source.device} [${source.port}] -> ${dest.device} [${dest.port}]`,
        };
      }
      if (connected) {
        return { changed: false, action, linkFamily, duplicate: true };
      }

      const override = Object.prototype.hasOwnProperty.call(options, "override")
        ? Boolean(options.override)
        : Boolean(overrideToggle?.checked);
      if (!override) {
        const movablePowerRow = selectedFamily === "POWER"
          && linkFamily === "POWER"
          && options.reassignOccupiedPowerDestination === true
          ? movablePowerConnection(source, dest)
          : null;
        if (movablePowerRow) {
          const previousSource = {
            device: movablePowerRow.source_device,
            port: movablePowerRow.source_port,
          };
          const previousPort = findPortMeta(movablePowerRow.source_device, movablePowerRow.source_port);
          const sourceConnectorChanged = (
            normalizedPowerSourceTransport(previousPort?.transport)
            !== normalizedPowerSourceTransport(source.transport)
          );
          movablePowerRow.source_device = source.device;
          movablePowerRow.source_port = source.port;
          if (sourceConnectorChanged) {
            const movedConnectionType = guessTypeTag(linkFamily, source, dest);
            if (movedConnectionType) movablePowerRow.connection_type = movedConnectionType;
          }
          movablePowerRow.override_1to1 = false;
          return {
            changed: true,
            action: "move",
            linkFamily,
            row: movablePowerRow,
            previousSource,
            currentSource: { device: source.device, port: source.port },
          };
        }
        const conflict = findStrictConflict(source.device, source.port, dest.device, dest.port);
        if (conflict) {
          return {
            changed: false,
            action,
            linkFamily,
            error: `1:1 blocked: ${source.device} [${source.port}] or ${dest.device} [${dest.port}] already used by ${conflict.cable_id || "(auto)"}. Enable override to allow.`,
          };
        }
      }

      const customType = String(
        Object.prototype.hasOwnProperty.call(options, "connectionType")
          ? options.connectionType
          : (typeTagInput?.value || "")
      ).trim();
      const inferredType = guessTypeTag(linkFamily, source, dest);
      const nextRow = {
        cable_id: "",
        family: linkFamily,
        source_device: source.device,
        source_port: source.port,
        dest_device: dest.device,
        dest_port: dest.port,
        connection_type: customType || inferredType,
        status: "Connected",
        notes: "",
        override_1to1: override,
      };
      connections.push(nextRow);
      return { changed: true, action, linkFamily, row: nextRow };
    }

    function deriveBulkPatchAction(selectedFamily, pairContexts) {
      let hasConnected = false;
      let hasDisconnected = false;
      for (const ctx of Array.isArray(pairContexts) ? pairContexts : []) {
        if (!ctx || !ctx.source || !ctx.dest) continue;
        if (!ctx.source.enabled || !ctx.dest.enabled) continue;
        const linkFamily = resolveLinkFamily(selectedFamily, ctx.source, ctx.dest);
        if (!linkFamily) continue;
        const existingIdx = findConnectionIndex(
          selectedFamily,
          ctx.source.device,
          ctx.source.port,
          ctx.dest.device,
          ctx.dest.port,
        );
        if (existingIdx >= 0) hasConnected = true;
        else hasDisconnected = true;
      }
      if (hasDisconnected) return "connect";
      if (hasConnected) return "disconnect";
      return "connect";
    }

    function setStatus(message, warn = false) {
      const text = String(message || "");
      if (statusEl instanceof HTMLElement) {
        statusEl.textContent = text;
        statusEl.classList.toggle("warn", Boolean(warn));
      } else {
        console.warn(`Status unavailable: ${text}`);
      }
      pushStatusHistory(text, warn);
      refreshDebugReportPanel();
    }

    function hideMatrixHoverTooltip() {
      if (!(matrixHoverTooltip instanceof HTMLElement)) return;
      matrixHoverTooltip.classList.add("hidden");
      matrixHoverTooltip.innerHTML = "";
    }

    function showMatrixHoverTooltip(title, lines, x, y) {
      if (!(matrixHoverTooltip instanceof HTMLElement)) return;
      const safeTitle = esc(String(title || "").trim());
      const safeLines = Array.isArray(lines) ? lines : [];
      const body = safeLines
        .map((line) => `<span class="line">${esc(String(line || ""))}</span>`)
        .join("");
      matrixHoverTooltip.innerHTML = `<span class="title">${safeTitle}</span>${body}`;
      matrixHoverTooltip.classList.remove("hidden");

      const viewportW = Number(window.innerWidth || document.documentElement.clientWidth || 0);
      const viewportH = Number(window.innerHeight || document.documentElement.clientHeight || 0);
      const padding = 12;
      const offset = 14;
      const rect = matrixHoverTooltip.getBoundingClientRect();
      let left = Number(x || 0) + offset;
      let top = Number(y || 0) + offset;
      if ((left + rect.width + padding) > viewportW) {
        left = Math.max(padding, Number(x || 0) - rect.width - offset);
      }
      if ((top + rect.height + padding) > viewportH) {
        top = Math.max(padding, Number(y || 0) - rect.height - offset);
      }
      matrixHoverTooltip.style.left = `${Math.max(padding, left)}px`;
      matrixHoverTooltip.style.top = `${Math.max(padding, top)}px`;
    }

    function listFamilyOptions(selected) {
      const current = normalizeFamily(selected || "AUDIO");
      return PORT_FAMILY_OPTIONS
        .map((token) => `<option value="${esc(token)}"${token === current ? " selected" : ""}>${esc(token)}</option>`)
        .join("");
    }

    function listPortPresetOptions(direction, selectedId = "") {
      const presets = Array.isArray(PORT_TYPE_PRESETS?.[direction]) ? PORT_TYPE_PRESETS[direction] : [];
      const active = String(selectedId || presets?.[0]?.id || "");
      return presets
        .map((preset) => {
          const selected = String(preset.id) === active ? " selected" : "";
          return `<option value="${esc(preset.id)}" data-base="${esc(preset.base || "")}" data-family="${esc(normalizeFamily(preset.family || "AUDIO"))}" data-transport="${esc(preset.transport || "")}"${selected}>${esc(preset.label || preset.id || "Port")}</option>`;
        })
        .join("");
    }

    function getActivePreset(direction) {
      const select = document.getElementById("addPortTypeSelect");
      const presetId = String(select?.value || "");
      const presets = Array.isArray(PORT_TYPE_PRESETS?.[direction]) ? PORT_TYPE_PRESETS[direction] : [];
      return presets.find((preset) => String(preset.id) === presetId) || presets[0] || null;
    }

    function getDirectionLabel(direction) {
      const token = String(direction || "").toLowerCase();
      if (token === "in") return "IN";
      if (token === "out") return "OUT";
      return "I/O";
    }

    function tabMatchesDirection(direction, tab) {
      const token = String(direction || "io").toLowerCase();
      if (tab === "in") return token === "in" || token === "io";
      return token === "out" || token === "io";
    }

    function normalizeDeviceLocation(value) {
      return String(value || "").trim() === "Rack" ? "Rack" : "Desk";
    }

    function isRackMountableDevice(device) {
      return Boolean(device && device.rack_mountable === true);
    }

    function normalizeRackUnits(value) {
      const units = Number(value);
      return Number.isInteger(units) && units >= 1 && units <= 16 ? units : 1;
    }

    function normalizeRackPosition(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const rack = Number(value.rack);
      const startU = Number(value.start_u);
      if (!Number.isInteger(rack) || rack < 1 || rack > 4) return null;
      if (!Number.isInteger(startU) || startU < 1 || startU > 16) return null;
      return { rack, start_u: startU };
    }

    function rackPlacementForDevice(device) {
      if (!isRackMountableDevice(device) || normalizeDeviceLocation(device.location) !== "Rack") return null;
      const position = normalizeRackPosition(device.rack_position);
      if (!position) return null;
      const units = normalizeRackUnits(device.rack_units);
      if ((position.start_u + units - 1) > 16) return null;
      return { ...position, units };
    }

    function canPlaceRackDevice(deviceName, rackValue, startUValue, unitsValue, options = null) {
      const device = getDeviceByName(deviceName);
      const mountable = isRackMountableDevice(device) || options?.rackMountable === true;
      if (!mountable) {
        return { ok: false, message: `${String(deviceName || "Device")} is not marked as rack mountable.` };
      }
      const rack = Number(rackValue);
      const startU = Number(startUValue);
      const units = Number(unitsValue);
      if (!Number.isInteger(rack) || rack < 1 || rack > 4) {
        return { ok: false, message: "Choose Rack 1, 2, 3, or 4." };
      }
      if (!Number.isInteger(startU) || startU < 1 || startU > 16) {
        return { ok: false, message: "Start U must be between U1 and U16." };
      }
      if (!Number.isInteger(units) || units < 1 || units > 16) {
        return { ok: false, message: "Height must be a whole number from 1 to 16 U/HE." };
      }
      const rackUnits = normalizeRackUnits(units);
      const endU = startU + rackUnits - 1;
      if (endU > 16) {
        return { ok: false, message: `${units}U at U${startU} extends beyond U16.` };
      }
      for (const other of ensureModelDeviceArray()) {
        const otherName = String(other?.name || "").trim();
        if (!otherName || otherName === String(deviceName || "").trim()) continue;
        const placement = rackPlacementForDevice(other);
        if (!placement || placement.rack !== rack) continue;
        const otherEndU = placement.start_u + placement.units - 1;
        if (startU <= otherEndU && endU >= placement.start_u) {
          return {
            ok: false,
            message: `${String(deviceName || "Device")} overlaps ${otherName} in Rack ${rack} (U${placement.start_u}-U${otherEndU}).`,
          };
        }
      }
      return { ok: true, message: `Rack ${rack}, U${startU}-U${endU}` };
    }

    function setRackEditorStatus(message, warn = false) {
      if (!rackEditorStatus) return;
      rackEditorStatus.textContent = String(message || "");
      rackEditorStatus.classList.toggle("warn", Boolean(warn));
    }

    function rackDeviceListHtml(devices, emptyText) {
      if (!devices.length) return `<li class="muted-note">${esc(emptyText)}</li>`;
      return devices.map((device) => {
        const name = String(device?.name || "").trim();
        const units = normalizeRackUnits(device?.rack_units);
        return `<li><button type="button" draggable="true" data-rack-drag-device="${esc(name)}" data-rack-select-device="${esc(name)}" aria-label="Drag ${esc(name)}, ${units} U, to a rack"><span class="rack-drag-affordance" aria-hidden="true">⠿</span>${esc(name)} <span class="muted-note">(${units}U)</span></button></li>`;
      }).join("");
    }

    function syncRackEditorControlsFromDevice(device) {
      if (!device) return;
      const location = normalizeDeviceLocation(device.location);
      const units = normalizeRackUnits(device.rack_units);
      const position = normalizeRackPosition(device.rack_position);
      if (rackEditorLocationSelect) rackEditorLocationSelect.value = location;
      if (rackEditorUnitsInput) rackEditorUnitsInput.value = String(units);
      if (rackEditorRackSelect) rackEditorRackSelect.value = String(position?.rack || 1);
      if (rackEditorStartUSelect) rackEditorStartUSelect.value = String(position?.start_u || 1);
      const rackFieldsDisabled = location !== "Rack";
      if (rackEditorRackSelect) rackEditorRackSelect.disabled = rackFieldsDisabled;
      if (rackEditorStartUSelect) rackEditorStartUSelect.disabled = rackFieldsDisabled;
      if (removeRackPlacementBtn) removeRackPlacementBtn.disabled = !position || location !== "Rack";
    }

    function renderRackEditor(options) {
      if (!rackEditorDeviceSelect || !rackEditorRacks) return;
      const rackDevices = sortedDevicesForEditor().filter(
        (device) => isRackMountableDevice(device)
      );
      const previousSelection = String(options?.deviceName || rackEditorDeviceSelect.value || selectedDeviceName || "").trim();
      const selected = rackDevices.find((device) => String(device?.name || "").trim() === previousSelection) || rackDevices[0] || null;
      const selectedName = String(selected?.name || "").trim();

      rackEditorDeviceSelect.innerHTML = rackDevices.length
        ? rackDevices.map((device) => {
          const name = String(device?.name || "").trim();
          return `<option value="${esc(name)}"${name === selectedName ? " selected" : ""}>${esc(name)}</option>`;
        }).join("")
        : '<option value="">No Rack devices</option>';
      rackEditorDeviceSelect.disabled = !rackDevices.length;
      if (applyRackPlacementBtn) applyRackPlacementBtn.disabled = !rackDevices.length;
      if (removeRackPlacementBtn) removeRackPlacementBtn.disabled = !rackDevices.length;

      if (rackEditorStartUSelect && !rackEditorStartUSelect.options.length) {
        rackEditorStartUSelect.innerHTML = Array.from({ length: 16 }, (_, index) => {
          const unit = index + 1;
          return `<option value="${unit}">U${unit}</option>`;
        }).join("");
      }
      if (selected) syncRackEditorControlsFromDevice(selected);

      const unplacedDevices = rackDevices.filter((device) => !rackPlacementForDevice(device));
      if (rackUnplacedList) rackUnplacedList.innerHTML = rackDeviceListHtml(unplacedDevices, "No unplaced Rack devices.");

      rackEditorRacks.innerHTML = Array.from({ length: 4 }, (_, rackIndex) => {
        const rack = rackIndex + 1;
        const placed = rackDevices
          .map((device) => ({ device, placement: rackPlacementForDevice(device) }))
          .filter((entry) => entry.placement?.rack === rack)
          .sort((a, b) => b.placement.start_u - a.placement.start_u);
        const unitRows = Array.from({ length: 16 }, (_, rowIndex) => {
          const unit = 16 - rowIndex;
          const gridRow = rowIndex + 1;
          return `<span class="rack-unit-label" style="grid-row:${gridRow}" aria-hidden="true">U${unit}</span><span class="rack-unit-slot" data-rack-drop-unit="${unit}" style="grid-row:${gridRow}" aria-hidden="true"></span>`;
        }).join("");
        const deviceBlocks = placed.map(({ device, placement }) => {
          const name = String(device?.name || "").trim();
          const endU = placement.start_u + placement.units - 1;
          const firstRow = 17 - endU;
          const lastRowExclusive = firstRow + placement.units;
          const active = name === selectedName ? " active" : "";
          const range = placement.units === 1 ? `U${placement.start_u}` : `U${placement.start_u}-U${endU}`;
          return `<button type="button" draggable="true" class="rack-device-block${active}" data-rack-drag-device="${esc(name)}" data-rack-select-device="${esc(name)}" style="grid-row:${firstRow} / ${lastRowExclusive}" aria-label="Drag ${esc(name)}, Rack ${rack}, ${range}, ${placement.units} U"><span class="rack-drag-affordance" aria-hidden="true">⠿</span>${esc(name)}<span>${range} · ${placement.units}U</span></button>`;
        }).join("");
        const usedUnits = placed.reduce((sum, entry) => sum + entry.placement.units, 0);
        return `<section class="panel rack-card" aria-labelledby="rack${rack}Heading"><h3 id="rack${rack}Heading"><span>Rack ${rack}</span><span class="muted-note">${usedUnits}/16U</span></h3><div class="rack-grid" data-rack-drop-rack="${rack}" aria-label="Rack ${rack}, 16 units, U16 at top through U1 at bottom">${unitRows}${deviceBlocks}</div></section>`;
      }).join("");

      if (!rackDevices.length) setRackEditorStatus("No rack-mountable devices. Mark eligible gear as Rack mountable in Devices & Ports.", true);
    }

    function rackDropPlacementFromPointer(grid, clientY, unitsValue, grabOffsetU = 0) {
      if (!(grid instanceof HTMLElement)) return null;
      const rect = grid.getBoundingClientRect();
      if (!Number.isFinite(rect.height) || rect.height <= 0 || !Number.isFinite(Number(clientY))) return null;
      const units = normalizeRackUnits(unitsValue);
      const offset = Number.isInteger(Number(grabOffsetU)) ? Number(grabOffsetU) : 0;
      const relativeY = Math.max(0, Math.min(rect.height - 0.001, Number(clientY) - rect.top));
      const rowIndex = Math.max(0, Math.min(15, Math.floor((relativeY / rect.height) * 16)));
      const pointerU = 16 - rowIndex;
      return { pointer_u: pointerU, start_u: pointerU - offset, units };
    }

    function moveRackDeviceToPosition(deviceName, rackValue, startUValue) {
      const name = String(deviceName || "").trim();
      const device = getDeviceByName(name);
      if (!isRackMountableDevice(device)) {
        setRackEditorStatus("Only rack-mountable devices can be placed in a rack.", true);
        return false;
      }
      const rack = Number(rackValue);
      const startU = Number(startUValue);
      const units = normalizeRackUnits(device.rack_units);
      const check = canPlaceRackDevice(name, rack, startU, units);
      if (!check.ok) {
        setRackEditorStatus(check.message, true);
        return false;
      }
      device.location = "Rack";
      device.rack_position = { rack, start_u: startU };
      selectedDeviceName = name;
      refreshFromModelEdit(`Moved ${name} to Rack ${rack}`);
      renderRackEditor({ deviceName: name });
      setRackEditorStatus(`${name}: ${check.message} (${units}U/HE).`);
      return true;
    }

    function clearRackDragIndicators() {
      if (!panelRack) return;
      for (const element of Array.from(panelRack.querySelectorAll(".rack-dragging, .rack-drop-target, .rack-drop-valid, .rack-drop-invalid"))) {
        element.classList.remove("rack-dragging", "rack-drop-target", "rack-drop-valid", "rack-drop-invalid");
      }
    }

    function finishRackDrag(cancelled = false) {
      clearRackDragIndicators();
      const draggedName = String(rackDragState?.deviceName || "");
      rackDragState = null;
      if (cancelled && draggedName) setRackEditorStatus(`Placement unchanged for ${draggedName}.`);
    }

    function applyRackEditorPlacement() {
      const name = String(rackEditorDeviceSelect?.value || "").trim();
      const device = getDeviceByName(name);
      if (!isRackMountableDevice(device)) {
        setRackEditorStatus("Choose gear marked as Rack mountable.", true);
        return false;
      }
      const units = Number(rackEditorUnitsInput?.value);
      if (!Number.isInteger(units) || units < 1 || units > 16) {
        setRackEditorStatus("Height must be a whole number from 1 to 16 U/HE.", true);
        return false;
      }
      const location = normalizeDeviceLocation(rackEditorLocationSelect?.value);
      if (location === "Desk") {
        device.location = "Desk";
        device.rack_units = units;
        delete device.rack_position;
        refreshFromModelEdit(`Moved ${name} to Desk`);
        renderRackEditor({ deviceName: name });
        setRackEditorStatus(`${name} is on Desk (${units}U/HE).`);
        return true;
      }

      const rack = Number(rackEditorRackSelect?.value);
      const startU = Number(rackEditorStartUSelect?.value);
      const check = canPlaceRackDevice(name, rack, startU, units);
      if (!check.ok) {
        setRackEditorStatus(check.message, true);
        return false;
      }
      device.location = "Rack";
      device.rack_units = units;
      device.rack_position = { rack, start_u: startU };
      refreshFromModelEdit(`Placed ${name} in Rack ${rack}`);
      renderRackEditor({ deviceName: name });
      setRackEditorStatus(`${name}: ${check.message} (${units}U/HE).`);
      return true;
    }

    function sortedDevicesForEditor() {
      const devices = ensureModelDeviceArray().slice();
      devices.sort((a, b) => String(a?.name || "").localeCompare(String(b?.name || ""), undefined, { numeric: true }));
      return devices;
    }

    function ensureSelectedDevice() {
      const selected = getDeviceByName(selectedDeviceName);
      if (selected) return selected;
      const devices = sortedDevicesForEditor();
      const fallback = devices[0] || null;
      selectedDeviceName = fallback ? String(fallback.name || "") : "";
      return fallback;
    }

    function refreshFromModelEdit(statusMessage, options = {}) {
      const skipImmediateSave = Boolean(options?.skipImmediateSave);
      const previousFamily = String(familySelect?.value || "");
      const editorViewState = captureDeviceEditorViewState();
      collapsedSourceGroups.clear();
      collapsedDestGroups.clear();
      collapsedSourceDevices.clear();
      collapsedDestDevices.clear();
      autoCollapsedFamilies.clear();
      autoOrganizeAllDevicePorts();
      updateDeviceOrderMetadata();
      rebuildModelCaches();
      const pruned = pruneConnectionsToKnownPorts();
      initFamilySelect(previousFamily);
      renderDeviceList();
      renderDeviceEditor();
      renderRackEditor();
      restoreDeviceEditorViewState(editorViewState);
      renderVisibilityPanel();
      renderMatrix();
      const base = String(statusMessage || "Model updated");
      const suffix = pruned > 0 ? ` (${pruned} invalid connection(s) removed)` : "";
      pendingModelEditSave = true;
      writeUiConfigToModel();
      setStatus(`${base}${suffix}`, pruned > 0);
      if (!skipImmediateSave) {
        scheduleAutoSave(base);
      }
    }

    function createUniquePortNames(baseName, count, existingNames) {
      const cleanBase = String(baseName || "").trim() || "Port";
      const amount = Math.max(1, Number(count) || 1);
      const used = new Set(Array.from(existingNames || []).map((name) => String(name || "").trim()));
      const output = [];

      if (amount === 1) {
        if (!used.has(cleanBase)) return [cleanBase];
        let index = 1;
        while (used.has(`${cleanBase} ${index}`)) index += 1;
        return [`${cleanBase} ${index}`];
      }

      let index = 1;
      while (output.length < amount) {
        const candidate = `${cleanBase} ${index}`;
        if (!used.has(candidate)) {
          output.push(candidate);
          used.add(candidate);
        }
        index += 1;
      }
      return output;
    }

    function renderDeviceList() {
      const devices = sortedDevicesForEditor();
      const selected = ensureSelectedDevice();
      if (!devices.length) {
        deviceListPanel.innerHTML = `<div style="padding:8px;" class="muted-note">No devices in model.</div>`;
        return;
      }
      const listHtml = devices.map((device) => {
        const name = String(device?.name || "").trim();
        const ports = Array.isArray(device?.ports) ? device.ports : [];
        const inCount = ports.filter((port) => tabMatchesDirection(port?.direction, "in")).length;
        const outCount = ports.filter((port) => tabMatchesDirection(port?.direction, "out")).length;
        const activeClass = selected && String(selected.name || "") === name ? " active" : "";
        const location = normalizeDeviceLocation(device?.location);
        const rackUnits = normalizeRackUnits(device?.rack_units);
        const placement = rackPlacementForDevice(device);
        const rackCapability = isRackMountableDevice(device) ? "Rack mountable" : "Not rack mountable";
        const placeLabel = location === "Rack"
          ? (placement ? `Rack ${placement.rack}, U${placement.start_u}` : "Rack, unplaced")
          : "Desk";
        return `
          <div class="device-item${activeClass}">
            <button type="button" class="device-title" data-select-device="${esc(name)}">${esc(name)}</button>
            <button type="button" class="device-action-btn" data-remove-device="${esc(name)}">Remove</button>
            <div class="device-sub">${esc(String(device?.device_type || "Other"))} | ${esc(rackCapability)} | ${esc(placeLabel)} | ${rackUnits}U | IN ${inCount} / OUT ${outCount}</div>
            <div></div>
          </div>
        `;
      }).join("");
      deviceListPanel.innerHTML = listHtml;
    }

    function clearVisibilityDragIndicators() {
      if (!visibilityListPanel) return;
      for (const row of Array.from(visibilityListPanel.querySelectorAll(".visibility-item"))) {
        row.classList.remove("drag-over-before", "drag-over-after", "dragging");
      }
    }

    function moveDeviceInModelOrder(sourceName, targetName, placeAfter = false) {
      const devices = ensureModelDeviceArray();
      const fromIndex = devices.findIndex((device) => String(device?.name || "").trim() === String(sourceName || "").trim());
      const toIndex = devices.findIndex((device) => String(device?.name || "").trim() === String(targetName || "").trim());
      if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return false;
      const [moved] = devices.splice(fromIndex, 1);
      let insertIndex = toIndex;
      if (fromIndex < toIndex) insertIndex -= 1;
      if (placeAfter) insertIndex += 1;
      devices.splice(Math.max(0, Math.min(devices.length, insertIndex)), 0, moved);
      updateDeviceOrderMetadata();
      return true;
    }

    function visibilityDeviceNamesInOrder() {
      return devicesInMatrixOrder()
        .map((device) => String(device?.name || "").trim())
        .filter(Boolean);
    }

    function syncVisibilitySelectionClasses() {
      if (!visibilityListPanel) return;
      for (const row of Array.from(visibilityListPanel.querySelectorAll("[data-visibility-row]"))) {
        const name = String(row.getAttribute("data-visibility-row") || "").trim();
        row.classList.toggle("selected", selectedVisibilityDevices.has(name));
        row.setAttribute("aria-selected", selectedVisibilityDevices.has(name) ? "true" : "false");
      }
    }

    function updateVisibilitySelection(deviceName, modifiers = {}) {
      const name = String(deviceName || "").trim();
      const names = visibilityDeviceNamesInOrder();
      if (!name || !names.includes(name)) return;

      const validNames = new Set(names);
      for (const selectedName of Array.from(selectedVisibilityDevices)) {
        if (!validNames.has(selectedName)) selectedVisibilityDevices.delete(selectedName);
      }

      if (modifiers.altKey) {
        selectedVisibilityDevices.clear();
        for (const candidate of names) selectedVisibilityDevices.add(candidate);
      } else if (modifiers.shiftKey && lastVisibilitySelectionAnchor && names.includes(lastVisibilitySelectionAnchor)) {
        const anchorIndex = names.indexOf(lastVisibilitySelectionAnchor);
        const currentIndex = names.indexOf(name);
        const start = Math.min(anchorIndex, currentIndex);
        const end = Math.max(anchorIndex, currentIndex);
        if (!modifiers.metaKey) selectedVisibilityDevices.clear();
        for (let index = start; index <= end; index += 1) {
          selectedVisibilityDevices.add(names[index]);
        }
      } else if (modifiers.metaKey) {
        if (selectedVisibilityDevices.has(name)) selectedVisibilityDevices.delete(name);
        else selectedVisibilityDevices.add(name);
        lastVisibilitySelectionAnchor = name;
      } else if (!selectedVisibilityDevices.has(name) || selectedVisibilityDevices.size <= 1) {
        selectedVisibilityDevices.clear();
        selectedVisibilityDevices.add(name);
        lastVisibilitySelectionAnchor = name;
      }
      if (!lastVisibilitySelectionAnchor) lastVisibilitySelectionAnchor = name;
      syncVisibilitySelectionClasses();
    }

    function renderVisibilityPanel() {
      if (!visibilityListPanel || !visibilitySummary) return;
      const devices = devicesInMatrixOrder();
      if (!devices.length) {
        visibilitySummary.textContent = "No devices in model.";
        visibilitySummary.classList.remove("warn");
        visibilityListPanel.innerHTML = `<div style="padding:8px;" class="muted-note">No devices to toggle.</div>`;
        return;
      }

      const validNames = new Set(devices.map((device) => String(device?.name || "").trim()).filter(Boolean));
      for (const selectedName of Array.from(selectedVisibilityDevices)) {
        if (!validNames.has(selectedName)) selectedVisibilityDevices.delete(selectedName);
      }
      const counts = DEVICE_VISIBILITY_TARGETS.map((target) => {
        const count = devices.filter((device) => deviceVisibleForTarget(device, target.key)).length;
        return `${target.shortLabel} ${count}/${devices.length}`;
      });
      visibilitySummary.textContent = counts.join(" | ");
      visibilitySummary.classList.remove("warn");

      const headerHtml = `
        <div class="visibility-list-header" aria-hidden="true">
          <span></span>
          <span class="visibility-device-heading">Device</span>
          ${DEVICE_VISIBILITY_TARGETS.map((target) => `<span title="Show in ${esc(target.label)}">${esc(target.shortLabel)}</span>`).join("")}
          <span></span>
        </div>
      `;
      const rowsHtml = devices.map((device) => {
        const name = String(device?.name || "").trim();
        const targetStates = DEVICE_VISIBILITY_TARGETS.map((target) => ({
          ...target,
          visible: deviceVisibleForTarget(device, target.key),
        }));
        const allHidden = targetStates.every((target) => !target.visible);
        const selected = selectedVisibilityDevices.has(name);
        const ports = Array.isArray(device?.ports) ? device.ports : [];
        const inCount = ports.filter((port) => tabMatchesDirection(port?.direction, "in")).length;
        const outCount = ports.filter((port) => tabMatchesDirection(port?.direction, "out")).length;
        const typeName = String(device?.device_type || "Other");
        return `
          <div class="visibility-item${allHidden ? " off" : ""}${selected ? " selected" : ""}" data-visibility-row="${esc(name)}" aria-selected="${selected ? "true" : "false"}">
            <span class="visibility-drag-grip" draggable="true" title="Drag to reorder devices in matrix view">::</span>
            <div class="visibility-device-info">
              <div class="visibility-name">${esc(name)}</div>
              <div class="visibility-meta">${esc(typeName)} | IN ${inCount} / OUT ${outCount}</div>
            </div>
            ${targetStates.map((target) => `
              <label class="visibility-toggle-cell" title="Show ${esc(name)} in ${esc(target.label)}">
                <input type="checkbox" data-visibility-device="${esc(name)}" data-visibility-target="${esc(target.key)}" aria-label="Show ${esc(name)} in ${esc(target.label)}"${target.visible ? " checked" : ""} />
              </label>
            `).join("")}
            <button type="button" class="device-action-btn" data-visibility-select="${esc(name)}">Edit</button>
          </div>
        `;
      }).join("");
      visibilityListPanel.innerHTML = headerHtml + rowsHtml;
    }

    function renderDeviceEditor() {
      const device = ensureSelectedDevice();
      if (!device) {
        deviceNameInput.value = "";
        deviceTypeInput.value = "";
        if (deviceLocationInput) deviceLocationInput.value = "Desk";
        if (deviceRackUnitsInput) deviceRackUnitsInput.value = "1";
        if (deviceRackMountableInput) deviceRackMountableInput.checked = false;
        deviceEditorPanel.innerHTML = `<div class="muted-note">Add a device to start editing ports.</div>`;
        return;
      }

      deviceNameInput.value = String(device?.name || "");
      deviceTypeInput.value = String(device?.device_type || "");
      if (deviceLocationInput) deviceLocationInput.value = normalizeDeviceLocation(device?.location);
      if (deviceRackUnitsInput) deviceRackUnitsInput.value = String(normalizeRackUnits(device?.rack_units));
      if (deviceRackMountableInput) deviceRackMountableInput.checked = isRackMountableDevice(device);

      const ports = Array.isArray(device?.ports) ? device.ports : [];
      const indexed = ports
        .map((port, index) => ({ port, index }))
        .filter((entry) => tabMatchesDirection(entry?.port?.direction, devicePortTab));
      const sorted = indexed.sort((a, b) => comparePortsForSide(a.port, b.port, devicePortTab, a.index, b.index));

      const presets = Array.isArray(PORT_TYPE_PRESETS?.[devicePortTab]) ? PORT_TYPE_PRESETS[devicePortTab] : [];
      const defaultPreset = presets[0] || { base: devicePortTab === "in" ? "Input" : "Output", family: "AUDIO", transport: "" };
      const rowHtml = sorted.map((entry) => {
        const idx = Number(entry.index);
        const port = entry.port || {};
        const families = Array.isArray(port?.families) ? port.families : [];
        const selectedFamily = normalizeFamily(families[0] || "AUDIO");
        const familyOptions = listFamilyOptions(selectedFamily);
        const portVisible = !parseBoolLike(port?.hidden, false) && parseBoolLike(port?.visible, true);
        const portEnabled = parseBoolLike(port?.enabled, true) && !parseBoolLike(port?.disabled, false);
        return `
          <tr>
            <td><input type="text" value="${esc(String(port?.name || ""))}" data-port-name="${idx}" /></td>
            <td><select data-port-family="${idx}">${familyOptions}</select></td>
            <td><input type="text" value="${esc(String(port?.transport || ""))}" data-port-transport="${idx}" /></td>
            <td><input type="checkbox" data-port-visible="${idx}"${portVisible ? " checked" : ""} /></td>
            <td><input type="checkbox" data-port-enabled="${idx}"${portEnabled ? " checked" : ""} /></td>
            <td>${esc(getDirectionLabel(port?.direction))}</td>
            <td><button type="button" class="device-action-btn" data-port-remove="${idx}">Remove</button></td>
          </tr>
        `;
      }).join("");

      const typeOptions = listPortPresetOptions(devicePortTab, defaultPreset.id || "");
      const directionLabel = devicePortTab === "in" ? "Input" : "Output";
      deviceEditorPanel.innerHTML = `
        <div class="editor-card">
          <div class="port-add-grid">
            <label>Port Type
              <select id="addPortTypeSelect">${typeOptions}</select>
            </label>
            <label>Amount
              <input id="addPortCountInput" type="number" min="1" value="1" />
            </label>
            <label>Base Name
              <input id="addPortBaseInput" type="text" value="${esc(defaultPreset.base || directionLabel)}" />
            </label>
            <label>Family
              <select id="addPortFamilySelect">${listFamilyOptions(defaultPreset.family || "AUDIO")}</select>
            </label>
            <label>Transport
              <input id="addPortTransportInput" type="text" value="${esc(defaultPreset.transport || "")}" />
            </label>
            <button id="addPortsBtn" type="button">Add ${directionLabel} Port(s)</button>
          </div>
          <div class="muted-note">Use Port Type + Amount for fast bulk creation; each new port is created individually.</div>
        </div>
        <div class="port-table-wrap">
          <table class="port-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Family</th>
                <th>Transport</th>
                <th>Visible</th>
                <th>Enabled</th>
                <th>Dir</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${rowHtml || `<tr><td colspan="7">No ${esc(directionLabel.toLowerCase())} ports.</td></tr>`}
            </tbody>
          </table>
        </div>
      `;
    }

    function commitPendingDeviceEditorEdits(reason = "Saved device") {
      if (deviceEditorCommitInProgress) return true;
      const device = ensureSelectedDevice();
      if (!device) return true;

      deviceEditorCommitInProgress = true;
      try {
        const oldName = String(device?.name || "").trim();
        const nextName = String(deviceNameInput?.value || "").trim();
        const currentType = String(device?.device_type || "").trim() || "Other";
        const nextType = String(deviceTypeInput?.value || "").trim() || "Other";
        const currentLocation = normalizeDeviceLocation(device?.location);
        const nextLocation = normalizeDeviceLocation(deviceLocationInput?.value);
        const currentRackUnits = normalizeRackUnits(device?.rack_units);
        const nextRackUnits = Number(deviceRackUnitsInput?.value);
        const currentRackMountable = isRackMountableDevice(device);
        const nextRackMountable = Boolean(deviceRackMountableInput?.checked);

        if (!nextName) {
          setStatus("Selected device name cannot be empty. Changes were not applied.", true);
          return false;
        }
        if (!Number.isInteger(nextRackUnits) || nextRackUnits < 1 || nextRackUnits > 16) {
          setStatus("Device height must be a whole number from 1 to 16 U/HE. Changes were not applied.", true);
          return false;
        }
        if (nextLocation === "Rack" && !nextRackMountable) {
          setStatus("Mark the device as Rack mountable before assigning it to Rack. Changes were not applied.", true);
          return false;
        }
        if (nextName !== oldName && getDeviceByName(nextName)) {
          setStatus(`Another device already uses name: ${nextName}. Changes were not applied.`, true);
          return false;
        }

        const ports = ensureDevicePortsArray(device);
        const portEdits = [];
        if (deviceEditorPanel) {
          for (const nameInput of Array.from(deviceEditorPanel.querySelectorAll("[data-port-name]"))) {
            if (!(nameInput instanceof HTMLInputElement)) continue;
            const index = Number(nameInput.getAttribute("data-port-name"));
            if (!Number.isInteger(index) || !ports[index]) continue;
            const nextPortName = String(nameInput.value || "").trim();
            if (!nextPortName) {
              setStatus("Port name cannot be empty. Changes were not applied.", true);
              return false;
            }
            const familyInput = deviceEditorPanel.querySelector(`[data-port-family="${index}"]`);
            const transportInput = deviceEditorPanel.querySelector(`[data-port-transport="${index}"]`);
            const visibleInput = deviceEditorPanel.querySelector(`[data-port-visible="${index}"]`);
            const enabledInput = deviceEditorPanel.querySelector(`[data-port-enabled="${index}"]`);
            portEdits.push({
              index,
              name: nextPortName,
              family: normalizeFamily(familyInput?.value || ports[index]?.families?.[0] || "AUDIO"),
              transport: String(transportInput?.value ?? ports[index]?.transport ?? "").trim(),
              visible: visibleInput instanceof HTMLInputElement
                ? Boolean(visibleInput.checked)
                : (!parseBoolLike(ports[index]?.hidden, false) && parseBoolLike(ports[index]?.visible, true)),
              enabled: enabledInput instanceof HTMLInputElement
                ? Boolean(enabledInput.checked)
                : (parseBoolLike(ports[index]?.enabled, true) && !parseBoolLike(ports[index]?.disabled, false)),
            });
          }
        }

        const proposedPortNames = ports.map((port) => String(port?.name || "").trim());
        for (const edit of portEdits) proposedPortNames[edit.index] = edit.name;
        const portNameChanged = portEdits.some((edit) => (
          edit.name !== String(ports[edit.index]?.name || "").trim()
        ));
        if (portNameChanged) {
          const duplicatePortName = proposedPortNames.find((name, index) => (
            Boolean(name) && proposedPortNames.indexOf(name) !== index
          ));
          if (duplicatePortName) {
            setStatus(`Port name already exists on ${oldName}: ${duplicatePortName}. Changes were not applied.`, true);
            return false;
          }
        }

        const currentPlacement = normalizeRackPosition(device.rack_position);
        if (nextLocation === "Rack" && currentPlacement) {
          const placementCheck = canPlaceRackDevice(
            oldName,
            currentPlacement.rack,
            currentPlacement.start_u,
            nextRackUnits,
            { rackMountable: nextRackMountable },
          );
          if (!placementCheck.ok) {
            setStatus(`Cannot save rack height: ${placementCheck.message}`, true);
            return false;
          }
        }

        const metadataChanged = nextName !== oldName
          || nextType !== currentType
          || nextLocation !== currentLocation
          || nextRackUnits !== currentRackUnits
          || nextRackMountable !== currentRackMountable;
        let portChanged = false;
        for (const edit of portEdits) {
          const port = ports[edit.index];
          const currentFamily = normalizeFamily(port?.families?.[0] || "AUDIO");
          const currentTransport = String(port?.transport || "").trim();
          const currentVisible = !parseBoolLike(port?.hidden, false) && parseBoolLike(port?.visible, true);
          const currentEnabled = parseBoolLike(port?.enabled, true) && !parseBoolLike(port?.disabled, false);
          if (
            edit.name !== String(port?.name || "").trim()
            || edit.family !== currentFamily
            || edit.transport !== currentTransport
            || edit.visible !== currentVisible
            || edit.enabled !== currentEnabled
          ) portChanged = true;
        }
        if (!metadataChanged && !portChanged) return true;

        if (nextName !== oldName) {
          for (const row of connections) {
            if (row.source_device === oldName) row.source_device = nextName;
            if (row.dest_device === oldName) row.dest_device = nextName;
          }
        }
        if (nextName !== oldName) device.name = nextName;
        if (nextType !== currentType) device.device_type = nextType;
        if (nextLocation !== currentLocation) device.location = nextLocation;
        if (nextRackMountable !== currentRackMountable) device.rack_mountable = nextRackMountable;
        if (nextRackUnits !== currentRackUnits) device.rack_units = nextRackUnits;
        if (metadataChanged && (!nextRackMountable || nextLocation === "Desk")) delete device.rack_position;
        if (metadataChanged && !String(device.layout_group || "").trim()) device.layout_group = nextType;
        for (const edit of portEdits) {
          const port = ports[edit.index];
          const currentFamily = normalizeFamily(port?.families?.[0] || "AUDIO");
          const currentTransport = String(port?.transport || "").trim();
          const currentVisible = !parseBoolLike(port?.hidden, false) && parseBoolLike(port?.visible, true);
          const currentEnabled = parseBoolLike(port?.enabled, true) && !parseBoolLike(port?.disabled, false);
          if (edit.name !== String(port?.name || "").trim()) port.name = edit.name;
          if (edit.family !== currentFamily) port.families = [edit.family];
          if (edit.transport !== currentTransport) port.transport = edit.transport;
          if (edit.visible !== currentVisible) {
            port.visible = edit.visible;
            port.hidden = !edit.visible;
          }
          if (edit.enabled !== currentEnabled) {
            port.enabled = edit.enabled;
            port.disabled = !edit.enabled;
          }
        }
        selectedDeviceName = nextName;
        sortDevicesInModel();
        refreshFromModelEdit(`${reason}: ${nextName}`);
        return true;
      } finally {
        deviceEditorCommitInProgress = false;
      }
    }

    async function flushPendingDeviceEditorAutoSave(reason = "devices-and-ports-exit") {
      if (!autoSaveEnabled || !saveApiEnabled || !pendingModelEditSave) return true;
      if (saveTimer) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      const ok = await saveJsonToDisk(reason, true, false);
      if (!ok && autoSaveEnabled) scheduleAutoSave(reason);
      return ok;
    }

    async function flushAutoSaveForShell(reason = "shell-navigation") {
      if (!panelDevices.classList.contains("hidden")) {
        if (!commitPendingDeviceEditorEdits("Saved before leaving the application window")) return false;
      }
      if (!autoSaveEnabled) return true;
      if (!saveApiEnabled) {
        setStatus("Auto-save could not run because the save API is unavailable.", true);
        return false;
      }
      if (saveTimer) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      const deadline = Date.now() + 7000;
      while (saveInFlight && Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 40));
      }
      if (saveInFlight) {
        setStatus("Auto-save is still running; navigation was held to protect changes.", true);
        return false;
      }
      const ok = await saveJsonToDisk(String(reason || "shell-navigation"), true, false);
      if (!ok) setStatus("Auto-save failed; navigation was held to protect changes.", true);
      return ok;
    }

    function applyPresetToAddForm() {
      const preset = getActivePreset(devicePortTab);
      if (!preset) return;
      const baseInput = document.getElementById("addPortBaseInput");
      const familySelectEl = document.getElementById("addPortFamilySelect");
      const transportInput = document.getElementById("addPortTransportInput");
      if (baseInput) baseInput.value = String(preset.base || baseInput.value || "");
      if (familySelectEl) familySelectEl.value = normalizeFamily(preset.family || familySelectEl.value || "AUDIO");
      if (transportInput) transportInput.value = String(preset.transport || "");
    }

    function addPortsFromEditor() {
      const device = ensureSelectedDevice();
      if (!device) {
        setStatus("No device selected.", true);
        return;
      }
      const ports = ensureDevicePortsArray(device);
      const countInput = document.getElementById("addPortCountInput");
      const baseInput = document.getElementById("addPortBaseInput");
      const familySelectEl = document.getElementById("addPortFamilySelect");
      const transportInput = document.getElementById("addPortTransportInput");
      const amount = Math.max(1, Number(countInput?.value || 1) || 1);
      const baseName = String(baseInput?.value || "").trim();
      if (!baseName) {
        setStatus("Base name is required.", true);
        return;
      }
      const selectedFamily = normalizeFamily(familySelectEl?.value || "AUDIO");
      const transport = String(transportInput?.value || "").trim();
      const usedNames = new Set(ports.map((port) => String(port?.name || "").trim()));
      const names = createUniquePortNames(baseName, amount, usedNames);
      const currentMaxOrder = ports.reduce((acc, port, index) => {
        const order = Number.isFinite(Number(port?.order)) ? Number(port.order) : index;
        return Math.max(acc, order);
      }, -1);
      for (let idx = 0; idx < names.length; idx += 1) {
        const name = names[idx];
        const modelPort = {
          name,
          direction: devicePortTab,
          families: [selectedFamily],
          transport,
          visible: true,
          hidden: false,
          enabled: true,
          disabled: false,
          order: currentMaxOrder + idx + 1,
        };
        modelPort.group = amount > 1
          ? { name: baseName, member: String(idx + 1), index: idx + 1, size: amount }
          : { name: baseName };
        ports.push(modelPort);
      }
      refreshFromModelEdit(`Added ${names.length} ${devicePortTab === "in" ? "input" : "output"} port(s) on ${String(device.name || "")}`);
    }

    function setDevicePortTab(tab) {
      devicePortTab = tab === "out" ? "out" : "in";
      portTabInputs.classList.toggle("active", devicePortTab === "in");
      portTabOutputs.classList.toggle("active", devicePortTab === "out");
      renderDeviceEditor();
    }

    function showMainTab(tabName) {
      const target = String(tabName || "matrix").toLowerCase();
      const leavingDeviceEditor = !panelDevices.classList.contains("hidden") && target !== "devices";
      if (leavingDeviceEditor && !commitPendingDeviceEditorEdits("Saved before leaving Devices & Ports")) {
        return false;
      }
      if (leavingDeviceEditor) flushPendingDeviceEditorAutoSave("devices-and-ports-tab-exit");
      const showMatrix = target === "matrix";
      const showDevices = target === "devices";
      const showRack = target === "rack" || target === "rack-editor";
      const showVisibility = target === "visibility";
      const showVisuals = target === "visuals";
      panelMatrix.classList.toggle("hidden", !showMatrix);
      panelDevices.classList.toggle("hidden", !showDevices);
      panelRack.classList.toggle("hidden", !showRack);
      panelVisibility.classList.toggle("hidden", !showVisibility);
      panelVisuals.classList.toggle("hidden", !showVisuals);
      mainTabMatrix.classList.toggle("active", showMatrix);
      mainTabDevices.classList.toggle("active", showDevices);
      mainTabRack.classList.toggle("active", showRack);
      mainTabVisibility.classList.toggle("active", showVisibility);
      mainTabVisuals.classList.toggle("active", showVisuals);
      if (!showMatrix) hideMatrixHoverTooltip();
      if (showDevices) {
        renderDeviceList();
        renderDeviceEditor();
      }
      if (showRack) {
        renderRackEditor();
      }
      if (showVisibility) {
        renderVisibilityPanel();
      }
      if (showVisuals) {
        refreshVisualPreviews("visuals-tab");
      }
      if (showMatrix && selectedMatrixSubTab === "patch") {
        syncMatrixHorizontalScroller();
      }
      scheduleMatrixViewportHeightUpdate();
      return true;
    }

    function applyExternalMainTab(tabName, matrixSubTab = "") {
      const target = String(tabName || "matrix").trim().toLowerCase();
      document.body.classList.remove("connection-overview-mode");
      document.body.dataset.activeShellPanel = target;
      document.documentElement.dataset.activeShellPanel = target;
      if (target === "connection-overview") {
        showMainTab("matrix");
        showMatrixSubTab("connections");
        document.body.classList.add("connection-overview-mode");
        return;
      }
      if (target === "rack" || target === "rack-editor") {
        showMainTab("rack-editor");
        return;
      }
      if (["matrix", "devices", "visibility", "visuals"].includes(target)) {
        showMainTab(target);
        if (target === "matrix" && matrixSubTab) showMatrixSubTab(matrixSubTab);
        return;
      }
      showMainTab("matrix");
    }

    function showMatrixSubTab(tabName) {
      const target = normalizeMatrixSubTab(tabName);
      selectedMatrixSubTab = target;
      const showPatch = target === "patch";
      const showConnections = target === "connections";
      if (matrixContainer) matrixContainer.classList.toggle("hidden", !showPatch);
      if (matrixXScroll) matrixXScroll.classList.toggle("hidden", !showPatch);
      if (connectionList) connectionList.classList.toggle("hidden", !showConnections);
      if (matrixSubTabPatch) matrixSubTabPatch.classList.toggle("active", showPatch);
      if (matrixSubTabConnections) matrixSubTabConnections.classList.toggle("active", showConnections);
      if (showPatch) {
        syncMatrixHorizontalScroller();
      } else {
        hideMatrixHoverTooltip();
      }
      scheduleMatrixViewportHeightUpdate();
    }

    function downloadModelJson() {
      writeUiConfigToModel();
      const payload = cloneJson(MODEL) || {};
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "studio_model.json";
      a.click();
      URL.revokeObjectURL(url);
      setStatus("Downloaded studio_model.json");
    }

    function buildConnectionsPayload(includeGeneratedOn = true) {
      const payload = {
        version: 1,
        connections: resolveCableIds(connections).map((row) => ({
          cable_id: row.cable_id,
          family: row.family,
          source_device: row.source_device,
          source_port: row.source_port,
          dest_device: row.dest_device,
          dest_port: row.dest_port,
          connection_type: row.connection_type || "",
          status: row.status || "Connected",
          notes: row.notes || "",
          override_1to1: Boolean(row.override_1to1),
        })),
      };
      if (includeGeneratedOn) {
        payload.generated_on = new Date().toISOString();
      }
      return payload;
    }

    // ----- Save pipeline (model + matrix + optional visual regenerate) -----
    async function saveJsonToDisk(reason = "manual", silent = false, force = false) {
      if (!saveApiEnabled) {
        if (!silent) {
          setStatus("Disk save unavailable. Start routing_matrix_server.py and open routing_matrix.html through that server.", true);
        }
        return false;
      }
      if (saveInFlight) {
        pendingSaveReason = String(reason || pendingSaveReason || "queued");
        return false;
      }

      const hashes = computeSaveHashes();
      if (!force && hashes.modelHash === lastSavedModelHash && hashes.connHash === lastSavedConnectionsHash) {
        return true;
      }

      saveInFlight = true;
      try {
        writeUiConfigToModel();
        const project = findProjectByKey(selectedProjectKey);
        if (!project || !selectedModelPath || !selectedConnectionsPath) {
          throw new Error("Select a project, device config, and patch config before saving.");
        }
        const modelPayload = cloneJson(MODEL) || {};
        const connectionsPayload = buildConnectionsPayload(true);
        const transactionPayload = buildSaveTransactionPayload(
          project,
          selectedModelPath,
          selectedConnectionsPath,
          {
            model: modelPayload,
            connections: connectionsPayload,
            expectedModelHash: loadedModelVersionHash,
            expectedConnectionsHash: loadedConnectionsVersionHash,
            reason,
          },
        );
        const transactionResponse = await postJsonApi("/api/save-transaction", transactionPayload);

        lastSavedModelHash = hashes.modelHash;
        lastSavedConnectionsHash = hashes.connHash;
        loadedModelVersionHash = String(transactionResponse?.saved?.hashes?.model || "").trim();
        loadedConnectionsVersionHash = String(transactionResponse?.saved?.hashes?.connections || "").trim();
        pendingModelEditSave = false;
        mergePreviewPathsFromConfig(transactionResponse);
        mergeRouteDebugPathFromConfig(transactionResponse);
        refreshVisualPreviews(`saved: ${reason}`);
        const visualsUpdated = transactionResponse?.regeneration?.ok !== false;
        if (!silent) {
          if (visualsUpdated) {
            setStatus(`Saved JSON to disk and updated visuals (${reason})`);
          } else {
            setStatus(`Saved JSON to disk (${reason}); visual regenerate failed`, true);
          }
        }
        return true;
      } catch (error) {
        if (!silent) {
          setStatus(`Save failed: ${String(error)}`, true);
        } else {
          setStatus(`Auto-save failed: ${String(error)}`, true);
        }
        return false;
      } finally {
        saveInFlight = false;
      }
    }

    function scheduleAutoSave(reason = "change") {
      if (!autoSaveEnabled || !saveApiEnabled) return;
      pendingSaveReason = String(reason || "change");
      if (saveTimer) {
        clearTimeout(saveTimer);
      }
      saveTimer = setTimeout(async () => {
        saveTimer = null;
        const saveReason = pendingSaveReason || "change";
        pendingSaveReason = "";
        const ok = await saveJsonToDisk(saveReason, true, false);
        if (!ok && autoSaveEnabled) {
          pendingSaveReason = saveReason;
        }
      }, SAVE_DEBOUNCE_MS);
    }

    function renderConnectionList() {
      const visibleDevices = visibleDeviceNameSet("connection_overview");
      const rows = resolveCableIds(
        connections.filter((row) =>
          visibleDevices.has(String(row?.source_device || ""))
          && visibleDevices.has(String(row?.dest_device || ""))
          && endpointVisible(row?.source_device, row?.source_port)
          && endpointVisible(row?.dest_device, row?.dest_port)
        )
      );
      const htmlRows = rows.map((row) => {
        const type = row.connection_type ? ` ${esc(row.connection_type)}` : "";
        const override = row.override_1to1 ? " (override)" : "";
        return `
          <tr>
            <td>${esc(row.cable_id)}</td>
            <td>${esc(row.family)}</td>
            <td>${esc(row.source_device)} [${esc(row.source_port)}]</td>
            <td>${esc(row.dest_device)} [${esc(row.dest_port)}]</td>
            <td>${type}${override}</td>
          </tr>
        `;
      }).join("");

      connectionList.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Family</th>
              <th>Source</th>
              <th>Destination</th>
              <th>Type/Flags</th>
            </tr>
          </thead>
          <tbody>
            ${htmlRows || '<tr><td colspan="5">No connections</td></tr>'}
          </tbody>
        </table>
      `;
    }

    // ----- Matrix renderer -----
    function renderMatrix() {
      try {
        hideMatrixHoverTooltip();
        normalizeConnectionIdsInState();
        rangeSelectionAnchor = null;
        const family = String(familySelect.value || "AUDIO");
        const sources = portsForFamily(family, true);
        const dests = portsForFamily(family, false);
        ensureDefaultCollapsedForFamily(family);
        const sourceEntries = buildAxisEntries(family, sources, "source", collapsedSourceGroups, collapsedSourceDevices);
        const destEntries = buildAxisEntries(family, dests, "dest", collapsedDestGroups, collapsedDestDevices);
        const sourceFilter = normalizeSearch(sourceFilterInput.value);
        const destFilter = normalizeSearch(destFilterInput.value);
        const visibleSourceEntries = sourceEntries.filter((entry) => entryMatchesFilter(entry, sourceFilter));
        const visibleDestEntries = destEntries.filter((entry) => entryMatchesFilter(entry, destFilter));
        if (!visibleSourceEntries.length || !visibleDestEntries.length) {
          const hasFilter = Boolean(sourceFilter || destFilter);
          const message = hasFilter
            ? `No ${esc(family)} ports match current filter.`
            : `No ports available for ${esc(family)}.`;
          matrixContainer.innerHTML = `<div style="padding:12px;color:#475569;">${message}</div>`;
          renderConnectionList();
          writeUiConfigToModel();
          syncMatrixHorizontalScroller();
          scheduleMatrixViewportHeightUpdate();
          return;
        }

        const familyConnIndexByKey = new Map();
        for (let idx = 0; idx < connections.length; idx += 1) {
          const conn = connections[idx];
          if (family !== FAMILY_ALL && conn.family !== family) continue;
          const key = `${conn.source_device}::${conn.source_port}=>${conn.dest_device}::${conn.dest_port}`;
          familyConnIndexByKey.set(key, idx);
        }

        function countConnectionsForEntries(sourceEntry, destEntry) {
          const srcPorts = sourceEntry.kind === "port" ? [sourceEntry.port] : sourceEntry.members;
          const dstPorts = destEntry.kind === "port" ? [destEntry.port] : destEntry.members;
          let count = 0;
          for (const src of srcPorts) {
            for (const dst of dstPorts) {
              const key = `${src.device}::${src.port}=>${dst.device}::${dst.port}`;
              if (familyConnIndexByKey.has(key)) count += 1;
            }
          }
          return count;
        }

        function collectConnectionsForEntries(sourceEntry, destEntry) {
          const srcPorts = sourceEntry.kind === "port" ? [sourceEntry.port] : (Array.isArray(sourceEntry.members) ? sourceEntry.members : []);
          const dstPorts = destEntry.kind === "port" ? [destEntry.port] : (Array.isArray(destEntry.members) ? destEntry.members : []);
          const matches = [];
          const seen = new Set();
          for (const src of srcPorts) {
            for (const dst of dstPorts) {
              const key = `${src.device}::${src.port}=>${dst.device}::${dst.port}`;
              if (!familyConnIndexByKey.has(key)) continue;
              const idx = Number(familyConnIndexByKey.get(key));
              if (!Number.isInteger(idx) || idx < 0 || idx >= connections.length) continue;
              if (seen.has(idx)) continue;
              seen.add(idx);
              matches.push(connections[idx]);
            }
          }
          return resolveCableIds(matches);
        }

        function formatEntryLabel(entry, axis) {
          const side = axis === "dest" ? "Destination" : "Source";
          if (!entry || typeof entry !== "object") return side;
          if (entry.kind === "port" && entry.port) {
            return `${entry.port.device} [${entry.port.port}]`;
          }
          if (entry.kind === "group") {
            return `${entry.device} [${entry.groupName}]`;
          }
          if (entry.kind === "device_header") {
            return `${entry.device} [${entry.memberCount} ports]`;
          }
          if (entry.kind === "device") {
            return `${entry.device || side} [${Array.isArray(entry.members) ? entry.members.length : 0} ports]`;
          }
          return side;
        }

        // Source rows are rendered as:
        // device header row + one or more child rows (groups/ports).
        function buildSourceRows(entries) {
          const rows = [];
          let idx = 0;
          while (idx < entries.length) {
            const first = entries[idx];
            const deviceName = first.kind === "port" ? first.port.device : first.device;
            const deviceKey = first.deviceKey || buildDeviceKey(family, "source", deviceName);
            if (first.kind === "device") {
              rows.push({
                kind: "device_header",
                device: deviceName,
                deviceKey,
                collapsed: true,
                memberCount: Number(first.memberCount) || 0,
                members: Array.isArray(first.members) ? first.members : [],
              });
              idx += 1;
              continue;
            }
            const memberCount = Number(first.deviceMemberCount) || 1;
            const deviceEntries = [];
            while ((idx + deviceEntries.length) < entries.length) {
              const entry = entries[idx + deviceEntries.length];
              const entryDevice = entry.kind === "port" ? entry.port.device : entry.device;
              if (entry.kind === "device" || entryDevice !== deviceName) break;
              deviceEntries.push(entry);
            }
            const isSinglePortDeviceRow = memberCount === 1
              && deviceEntries.length === 1
              && deviceEntries[0]?.kind === "port";
            const aggregateMembers = [];
            for (const entry of deviceEntries) {
              if (entry?.kind === "port" && entry?.port) {
                aggregateMembers.push(entry.port);
              } else if (entry?.kind === "group" && Array.isArray(entry.members)) {
                aggregateMembers.push(...entry.members);
              }
            }
            if (!isSinglePortDeviceRow) {
              rows.push({
                kind: "device_header",
                device: deviceName,
                deviceKey,
                collapsed: false,
                memberCount,
                members: aggregateMembers,
              });
            }
            for (let localIdx = 0; localIdx < deviceEntries.length; localIdx += 1) {
              const entry = deviceEntries[localIdx];
              const entryIndex = idx + localIdx;
              rows.push({
                kind: "entry",
                entry,
                entryIndex,
                inlineDevice: isSinglePortDeviceRow ? deviceName : "",
                suppressPortLabel: isSinglePortDeviceRow && isGenericSinglePortLabel(entry?.port?.port),
              });
            }
            idx += deviceEntries.length;
          }
          return rows;
        }

        function destEntryDeviceName(entry) {
          return entry.kind === "port" ? String(entry?.port?.device || "") : String(entry?.device || "");
        }

        // Destination columns use the same concept as source rows:
        // a device header plus child columns (groups/ports).
        function buildDestDisplayEntries(entries) {
          const output = [];
          let idx = 0;
          while (idx < entries.length) {
            const first = entries[idx];
            const deviceName = destEntryDeviceName(first);
            if (!deviceName) {
              idx += 1;
              continue;
            }
            const isCollapsedDevice = first.kind === "device";
            const deviceKey = first.deviceKey || buildDeviceKey(family, "dest", deviceName);
            const members = isCollapsedDevice
              ? (Array.isArray(first.members) ? first.members : [])
              : [];
            const memberCount = isCollapsedDevice
              ? (Number(first.memberCount) || members.length)
              : (Number(first.deviceMemberCount) || 1);
            output.push({
              kind: "device_header",
              device: deviceName,
              deviceKey,
              members: isCollapsedDevice ? members : [],
              memberCount,
              collapsed: isCollapsedDevice,
            });
            if (isCollapsedDevice) {
              idx += 1;
              continue;
            }
            while (idx < entries.length) {
              const entry = entries[idx];
              const entryDevice = destEntryDeviceName(entry);
              if (entryDevice !== deviceName || entry.kind === "device") break;
              output.push(entry);
              idx += 1;
            }
          }
          return output;
        }

        const displayDestEntries = buildDestDisplayEntries(visibleDestEntries);
        const singlePortDestDevices = new Set(
          displayDestEntries
            .filter((entry) => entry.kind === "port" && Number(entry.deviceMemberCount) === 1)
            .map((entry) => String(entry?.port?.device || ""))
        );

        let table = '<table><thead>';
        table += '<tr class="dest-port-row"><th class="sticky-left source-head top-left" rowspan="1"><span class="dev">Source</span><span class="prt">Destination</span></th>';
        for (let dIdx = 0; dIdx < displayDestEntries.length; dIdx += 1) {
          const entry = displayDestEntries[dIdx];
          const classes = ["dest-head", "dest-port-head"];
          let title = "";
          let destPortText = "";
          let groupToggle = "";
          let deviceToggle = "";
          let deviceText = "";
          let familyBadge = "";

          if (entry.kind === "device_header") {
            classes.push("device-folder");
            if (entry.collapsed) classes.push("collapsed");
            title = `${entry.device} (${entry.memberCount} ports)`;
            deviceText = entry.device;
            const symbol = entry.collapsed ? "▸" : "▾";
            const toggleTitle = entry.collapsed ? "Expand device" : "Collapse device";
            deviceToggle = `<button type="button" class="group-toggle" data-device-toggle="1" data-axis="dest" data-device="${esc(entry.deviceKey)}" title="${toggleTitle}">${symbol}</button>`;
            destPortText = entry.collapsed
              ? compactText(`${entry.memberCount} ports hidden`, 18)
              : compactText(`${entry.memberCount} ports`, 18);
          } else if (entry.kind === "group") {
            classes.push("collapsed");
            const groupLabel = `${entry.groupName} (${entry.memberCount})`;
            title = `${entry.device} [${groupLabel}] (collapsed)`;
            destPortText = compactText(groupLabel, 18);
            groupToggle = `<button type="button" class="group-toggle" data-group-toggle="1" data-axis="dest" data-group="${esc(entry.groupKey)}" title="Expand group">▸</button>`;
          } else {
            title = `${entry.port.device} [${entry.port.port}]`;
            familyBadge = powerPortBadge(entry.port);
            const suppressPortLabel = singlePortDestDevices.has(String(entry.port.device || ""))
              && isGenericSinglePortLabel(entry.port.port);
            destPortText = suppressPortLabel ? "" : compactText(entry.port.port, 18);
            if (entry.collapsible) {
              groupToggle = `<button type="button" class="group-toggle" data-group-toggle="1" data-axis="dest" data-group="${esc(entry.groupKey)}" title="Collapse group">▾</button>`;
            }
          }
          table += `<th class="${classes.join(" ")}" title="${esc(title)}"><div class="dest-col"><div class="dest-controls">${deviceToggle}${groupToggle}</div><div class="dest-labels"><span class="dest-device">${esc(deviceText || " ")}</span><span class="dest-port"><span class="port-label-text">${esc(destPortText || " ")}</span>${familyBadge}</span></div></div></th>`;
        }
        table += "</tr></thead><tbody>";

        const sourceRows = buildSourceRows(visibleSourceEntries);
        const sourceAggregateByDeviceKey = new Map();
        for (const sourceRow of sourceRows) {
          if (sourceRow.kind !== "device_header") continue;
          sourceAggregateByDeviceKey.set(sourceRow.deviceKey, {
            kind: "device",
            device: sourceRow.device,
            members: Array.isArray(sourceRow.members) ? sourceRow.members : [],
            memberCount: Number(sourceRow.memberCount) || 0,
          });
        }
        for (const sourceRow of sourceRows) {
        if (sourceRow.kind === "device_header") {
          const deviceTitle = `${sourceRow.device} (${sourceRow.memberCount} ports)`;
          const deviceHint = sourceRow.collapsed ? `${sourceRow.memberCount} ports hidden` : `${sourceRow.memberCount} ports`;
          const deviceSymbol = sourceRow.collapsed ? "▸" : "▾";
          const deviceToggleTitle = sourceRow.collapsed ? "Expand device" : "Collapse device";
          const sourceAggregateEntry = {
            kind: "device",
            members: Array.isArray(sourceRow.members) ? sourceRow.members : [],
          };
          table += `<tr class="device-folder-row"><th class="sticky-left source-head device-folder" title="${esc(deviceTitle)}"><div class="source-line"><button type="button" class="group-toggle" data-device-toggle="1" data-axis="source" data-device="${esc(sourceRow.deviceKey)}" title="${deviceToggleTitle}">${deviceSymbol}</button><span class="dev">${esc(compactText(sourceRow.device, 18))}</span></div><span class="prt">${esc(compactText(deviceHint, 16))}</span></th>`;
          for (let dIdx = 0; dIdx < displayDestEntries.length; dIdx += 1) {
            const destEntry = displayDestEntries[dIdx];
            const count = countConnectionsForEntries(sourceAggregateEntry, destEntry);
            const cellClasses = ["cell", "group-blocked"];
            if (count > 0) cellClasses.push("group-on");
            const title = count > 0
              ? `${count} routed link(s) in collapsed device row. Expand device to edit individual ports.`
              : "Collapsed device row. Expand device to route individual ports.";
            table += `<td class="${cellClasses.join(" ")}" data-hover-title="${esc(title)}" data-sagg="${esc(sourceRow.deviceKey)}" data-di="${dIdx}">${count > 0 ? String(count) : ""}</td>`;
          }
          table += "</tr>";
          continue;
        }

        const sourceEntry = sourceRow.entry;
        const sIdx = Number(sourceRow.entryIndex);
        const classes = ["sticky-left", "source-head"];
        let sourceTitle = "";
        let sourcePortText = "";
        let groupToggle = "";
        if (sourceEntry.kind === "group") {
          classes.push("collapsed");
          const groupLabel = `${sourceEntry.groupName} (${sourceEntry.memberCount})`;
          sourceTitle = `${sourceEntry.device} [${groupLabel}] (collapsed)`;
          sourcePortText = groupLabel;
          groupToggle = `<button type="button" class="group-toggle" data-group-toggle="1" data-axis="source" data-group="${esc(sourceEntry.groupKey)}" title="Expand group">▸</button>`;
        } else {
          sourceTitle = `${sourceEntry.port.device} [${sourceEntry.port.port}]`;
          sourcePortText = sourceEntry.port.port;
          if (sourceEntry.collapsible) {
            groupToggle = `<button type="button" class="group-toggle" data-group-toggle="1" data-axis="source" data-group="${esc(sourceEntry.groupKey)}" title="Collapse group">▾</button>`;
          }
        }
        const sourceDeviceInline = String(sourceRow.inlineDevice || "");
        const sourceDeviceShort = sourceDeviceInline ? compactText(sourceDeviceInline, 18) : "";
        const sourcePortShort = sourceRow.suppressPortLabel ? "" : compactText(sourcePortText, 16);
        const sourceFamilyBadge = sourceEntry.kind === "port" ? powerPortBadge(sourceEntry.port) : "";
        table += `<tr><th class="${classes.join(" ")}" title="${esc(sourceTitle)}"><div class="source-line">${groupToggle}<span class="dev">${esc(sourceDeviceShort)}</span></div><span class="prt"><span class="port-label-text">${esc(sourcePortShort)}</span>${sourceFamilyBadge}</span></th>`;

        for (let dIdx = 0; dIdx < displayDestEntries.length; dIdx += 1) {
          const destEntry = displayDestEntries[dIdx];
          const editable = sourceEntry.kind === "port" && destEntry.kind === "port";
          if (!editable) {
            const count = countConnectionsForEntries(sourceEntry, destEntry);
            const cellClasses = ["cell", "group-blocked"];
            if (count > 0) cellClasses.push("group-on");
            const title = count > 0
              ? `${count} routed link(s) in collapsed item. Expand group/device to edit.`
              : "Collapsed item. Expand group/device to route individual ports.";
            table += `<td class="${cellClasses.join(" ")}" data-hover-title="${esc(title)}" data-si="${sIdx}" data-di="${dIdx}">${count > 0 ? String(count) : ""}</td>`;
            continue;
          }

          const source = sourceEntry.port;
          const dest = destEntry.port;
          const linkFamily = resolveLinkFamily(family, source, dest);
          const patchEnabled = Boolean(source?.enabled) && Boolean(dest?.enabled);
          const key = `${source.device}::${source.port}=>${dest.device}::${dest.port}`;
          const connIdx = familyConnIndexByKey.has(key) ? Number(familyConnIndexByKey.get(key)) : -1;
          const connected = connIdx >= 0;
          const row = connected ? connections[connIdx] : null;
          const compatible = Boolean(linkFamily);
          const connectorAdvisory = !connected ? powerConnectorAdvisory(linkFamily, source, dest) : "";
          const moveTitle = !connected ? powerMoveHoverTitle(source, dest, linkFamily) : "";
          const cellClasses = ["cell"];
          if (connected) cellClasses.push("on");
          if (connected && row?.override_1to1) cellClasses.push("override");
          if (!patchEnabled) cellClasses.push("disabled-port");
          if (!connected && !compatible) cellClasses.push("incompatible");
          const title = !patchEnabled
            ? (connected
              ? `Linked but disabled for patch changes: ${row?.cable_id || "(auto)"}`
              : "Disabled port (read-only)")
            : (connected
              ? `${row.cable_id || "(auto)"} ${row.connection_type || ""} [${row.family || "?"}]`.trim()
              : (moveTitle || (compatible
                ? `Click to connect (${linkFamily})${connectorAdvisory ? ` — ${connectorAdvisory}` : ""}`
                : linkCompatibilityReason(family, source, dest))));
          table += `<td class="${cellClasses.join(" ")}" data-hover-title="${esc(title)}" data-si="${sIdx}" data-di="${dIdx}"></td>`;
        }
        table += "</tr>";
      }
        table += "</tbody></table>";
        matrixContainer.innerHTML = table;
        const matrixTable = matrixContainer.querySelector("table");
        attachMatrixResizeHandles(matrixTable);

        function clearMatrixAxisHover() {
          if (!matrixTable) return;
          for (const item of matrixTable.querySelectorAll(".axis-hover-row")) {
            item.classList.remove("axis-hover-row");
          }
          for (const item of matrixTable.querySelectorAll(".axis-hover-col")) {
            item.classList.remove("axis-hover-col");
          }
          for (const item of matrixTable.querySelectorAll(".axis-hover-point")) {
            item.classList.remove("axis-hover-point");
          }
        }

        function applyMatrixAxisHover(targetCell) {
          if (!matrixTable) return;
          clearMatrixAxisHover();
          if (!targetCell) return;
          const rowIndex = Number(targetCell.parentElement?.rowIndex ?? -1);
          const colIndex = Number(targetCell.cellIndex ?? -1);
          if (rowIndex < 0 || colIndex < 0) return;

          const row = matrixTable.rows[rowIndex];
          if (row) {
            for (const rowCell of Array.from(row.cells)) {
              rowCell.classList.add("axis-hover-row");
            }
          }
          for (const rowEntry of Array.from(matrixTable.rows)) {
            const colCell = rowEntry.cells[colIndex];
            if (colCell) colCell.classList.add("axis-hover-col");
          }
          targetCell.classList.add("axis-hover-point");
        }

        let suppressNextCellClick = false;
        let rangeAnchorCell = null;

        function clearRangeAnchorVisual() {
          if (rangeAnchorCell instanceof HTMLTableCellElement) {
            rangeAnchorCell.classList.remove("range-anchor");
          }
          rangeAnchorCell = null;
        }

        function buildPatchContextFromIndexes(sourceIndex, destIndex, cell = null) {
          if (!Number.isInteger(sourceIndex) || !Number.isInteger(destIndex)) return null;
          if (sourceIndex < 0 || sourceIndex >= visibleSourceEntries.length) return null;
          if (destIndex < 0 || destIndex >= displayDestEntries.length) return null;
          const sourceEntry = visibleSourceEntries[sourceIndex];
          const destEntry = displayDestEntries[destIndex];
          if (!sourceEntry || !destEntry) return null;
          const editable = sourceEntry.kind === "port" && destEntry.kind === "port";
          const source = editable ? sourceEntry.port : null;
          const dest = editable ? destEntry.port : null;
          const linkFamily = editable ? resolveLinkFamily(family, source, dest) : "";
          const patchEnabled = editable && Boolean(source?.enabled) && Boolean(dest?.enabled);
          const existingIdx = editable
            ? findConnectionIndex(family, source.device, source.port, dest.device, dest.port)
            : -1;
          return {
            sourceIndex,
            destIndex,
            sourceEntry,
            destEntry,
            editable,
            source,
            dest,
            linkFamily,
            patchEnabled,
            existingIdx,
            connected: existingIdx >= 0,
            cell: cell instanceof HTMLTableCellElement ? cell : null,
          };
        }

        function buildPatchContextFromCell(cell) {
          if (!(cell instanceof HTMLTableCellElement)) return null;
          const sourceIndex = Number(cell.dataset.si);
          const destIndex = Number(cell.dataset.di);
          return buildPatchContextFromIndexes(sourceIndex, destIndex, cell);
        }

        function updateCellHoverTitle(cell, context) {
          if (!(cell instanceof HTMLTableCellElement) || !context || !context.editable) return;
          const latestIdx = findConnectionIndex(
            family,
            context.source.device,
            context.source.port,
            context.dest.device,
            context.dest.port,
          );
          const connected = latestIdx >= 0;
          const row = connected ? connections[latestIdx] : null;
          const connectorAdvisory = !connected
            ? powerConnectorAdvisory(context.linkFamily, context.source, context.dest)
            : "";
          const moveTitle = !connected
            ? powerMoveHoverTitle(context.source, context.dest, context.linkFamily)
            : "";
          const title = !context.patchEnabled
            ? (connected
              ? `Linked but disabled for patch changes: ${row?.cable_id || "(auto)"}`
              : "Disabled port (read-only)")
            : (connected
              ? `${row.cable_id || "(auto)"} ${row.connection_type || ""} [${row.family || "?"}]`.trim()
              : (moveTitle || (context.linkFamily
                ? `Click to connect (${context.linkFamily})${connectorAdvisory ? ` — ${connectorAdvisory}` : ""}`
                : linkCompatibilityReason(family, context.source, context.dest))));
          cell.dataset.hoverTitle = title;
        }

        function refreshRenderedCellState(cell) {
          const context = buildPatchContextFromCell(cell);
          if (!context || !context.editable) return;
          const latestIdx = findConnectionIndex(
            family,
            context.source.device,
            context.source.port,
            context.dest.device,
            context.dest.port,
          );
          const connected = latestIdx >= 0;
          const row = connected ? connections[latestIdx] : null;
          cell.classList.toggle("on", connected);
          cell.classList.toggle("override", Boolean(connected && row?.override_1to1));
          cell.classList.toggle("incompatible", !connected && !context.linkFamily);
          updateCellHoverTitle(cell, context);
        }

        function applyPatchContexts(contexts, requestedAction, options = {}) {
          const summary = {
            changed: 0,
            connected: 0,
            disconnected: 0,
            moved: 0,
            blocked: 0,
            firstError: "",
            action: normalizePatchAction(requestedAction),
          };
          for (const context of Array.isArray(contexts) ? contexts : []) {
            if (!context || !context.editable || !context.source || !context.dest) {
              summary.blocked += 1;
              if (!summary.firstError) {
                summary.firstError = "Expand collapsed group/device to route individual ports.";
              }
              continue;
            }
            const result = performPatchAction(family, context.source, context.dest, summary.action, options);
            if (result.changed) {
              summary.changed += 1;
              if (result.action === "connect") summary.connected += 1;
              if (result.action === "disconnect") summary.disconnected += 1;
              if (result.action === "move") summary.moved += 1;
              if (context.cell) refreshRenderedCellState(context.cell);
            } else if (result.error && !summary.firstError) {
              summary.firstError = result.error;
            }
          }
          return summary;
        }

        function collectDiagonalContexts(startSourceIndex, startDestIndex, count) {
          const contexts = [];
          const maxCount = Math.max(1, Number(count || 1));
          let sourceIndex = Number(startSourceIndex);
          let destIndex = Number(startDestIndex);
          while (
            contexts.length < maxCount
            && sourceIndex < visibleSourceEntries.length
            && destIndex < displayDestEntries.length
          ) {
            const context = buildPatchContextFromIndexes(sourceIndex, destIndex, null);
            if (context && context.editable) contexts.push(context);
            sourceIndex += 1;
            destIndex += 1;
          }
          return contexts;
        }

        function buildRangeContexts(anchor, target) {
          const sourceStart = Math.min(Number(anchor?.sourceIndex || 0), Number(target?.sourceIndex || 0));
          const sourceEnd = Math.max(Number(anchor?.sourceIndex || 0), Number(target?.sourceIndex || 0));
          const destStart = Math.min(Number(anchor?.destIndex || 0), Number(target?.destIndex || 0));
          const destEnd = Math.max(Number(anchor?.destIndex || 0), Number(target?.destIndex || 0));

          const sourceIndexes = [];
          for (let idx = sourceStart; idx <= sourceEnd; idx += 1) {
            const entry = visibleSourceEntries[idx];
            if (entry?.kind === "port") sourceIndexes.push(idx);
          }
          const destIndexes = [];
          for (let idx = destStart; idx <= destEnd; idx += 1) {
            const entry = displayDestEntries[idx];
            if (entry?.kind === "port") destIndexes.push(idx);
          }

          if (!sourceIndexes.length || !destIndexes.length) return [];
          const contexts = [];
          if (sourceIndexes.length === 1) {
            for (const destIndex of destIndexes) {
              const context = buildPatchContextFromIndexes(sourceIndexes[0], destIndex, null);
              if (context) contexts.push(context);
            }
            return contexts;
          }
          if (destIndexes.length === 1) {
            for (const sourceIndex of sourceIndexes) {
              const context = buildPatchContextFromIndexes(sourceIndex, destIndexes[0], null);
              if (context) contexts.push(context);
            }
            return contexts;
          }

          const pairCountLocal = Math.min(sourceIndexes.length, destIndexes.length);
          for (let idx = 0; idx < pairCountLocal; idx += 1) {
            const context = buildPatchContextFromIndexes(sourceIndexes[idx], destIndexes[idx], null);
            if (context) contexts.push(context);
          }
          return contexts;
        }

        function finishPaintSession() {
          if (!paintSession) return;
          const summary = paintSession;
          paintSession = null;
          if (summary.changed > 0) {
            scheduleAutoSave(summary.action === "connect" ? "paint-connect" : "paint-disconnect");
            renderMatrix();
            setStatus(
              `Paint ${summary.action}: ${summary.changed} change(s) (${summary.connected} connected, ${summary.disconnected} disconnected)`,
            );
            return;
          }
          if (summary.firstError) {
            setStatus(summary.firstError, true);
            return;
          }
          setStatus("Paint mode: no changes.");
        }

        function applyPaintToCell(cell) {
          if (!(cell instanceof HTMLTableCellElement) || !paintSession) return;
          const context = buildPatchContextFromCell(cell);
          if (!context || !context.editable) return;
          const key = `${context.sourceIndex}:${context.destIndex}`;
          if (paintSession.visited.has(key)) return;
          paintSession.visited.add(key);
          context.cell = cell;
          const result = applyPatchContexts([context], paintSession.action);
          paintSession.changed += result.changed;
          paintSession.connected += result.connected;
          paintSession.disconnected += result.disconnected;
          if (result.firstError && !paintSession.firstError) {
            paintSession.firstError = result.firstError;
          }
        }

        matrixTable.onmousedown = (event) => {
          const cell = event.target.closest("td.cell");
          if (!cell || !matrixTable.contains(cell)) return;
          if (selectedPatchMode !== "paint") return;
          event.preventDefault();
          const context = buildPatchContextFromCell(cell);
          if (!context || !context.editable) {
            setStatus("Expand collapsed group/device to route individual ports.", true);
            return;
          }
          if (!context.patchEnabled) {
            setStatus(`Disabled port: ${context.source.device} [${context.source.port}] -> ${context.dest.device} [${context.dest.port}] is read-only.`, true);
            return;
          }
          suppressNextCellClick = true;
          paintSession = {
            action: context.connected ? "disconnect" : "connect",
            visited: new Set(),
            changed: 0,
            connected: 0,
            disconnected: 0,
            firstError: "",
          };
          applyPaintToCell(cell);
          window.addEventListener("mouseup", finishPaintSession, { once: true });
        };

        matrixTable.onmouseover = (event) => {
          if (!paintSession) return;
          const cell = event.target.closest("td.cell");
          if (!cell || !matrixTable.contains(cell)) return;
          applyPaintToCell(cell);
        };

        matrixTable.onmousemove = (event) => {
          const hoverCell = event.target.closest("td.cell");
          const isCell = hoverCell && matrixTable.contains(hoverCell);
          if (!isCell) {
            clearMatrixAxisHover();
            hideMatrixHoverTooltip();
            return;
          }
          if (hoverCell.dataset.si != null && hoverCell.dataset.di != null) {
            applyMatrixAxisHover(hoverCell);
          } else {
            clearMatrixAxisHover();
          }

          if (paintSession) {
            hideMatrixHoverTooltip();
            return;
          }

          let tooltipSource = null;
          let tooltipDest = null;
          const sourceIndex = Number(hoverCell.dataset.si);
          const destIndex = Number(hoverCell.dataset.di);
          if (Number.isInteger(sourceIndex) && sourceIndex >= 0 && sourceIndex < visibleSourceEntries.length) {
            tooltipSource = visibleSourceEntries[sourceIndex];
          } else {
            const sourceAggregateKey = String(hoverCell.dataset.sagg || "").trim();
            if (sourceAggregateKey && sourceAggregateByDeviceKey.has(sourceAggregateKey)) {
              tooltipSource = sourceAggregateByDeviceKey.get(sourceAggregateKey);
            }
          }
          if (Number.isInteger(destIndex) && destIndex >= 0 && destIndex < displayDestEntries.length) {
            tooltipDest = displayDestEntries[destIndex];
          }
          if (!tooltipSource || !tooltipDest) {
            hideMatrixHoverTooltip();
            return;
          }

          const matches = collectConnectionsForEntries(tooltipSource, tooltipDest);
          const tooltipTitle = `${formatEntryLabel(tooltipSource, "source")} -> ${formatEntryLabel(tooltipDest, "dest")}`;
          const tooltipLines = [];
          if (matches.length > 0) {
            const maxLines = 8;
            for (let idx = 0; idx < Math.min(matches.length, maxLines); idx += 1) {
              const row = matches[idx];
              tooltipLines.push(`${row.cable_id || "(auto)"}: ${row.source_port} -> ${row.dest_port}`);
            }
            if (matches.length > maxLines) {
              tooltipLines.push(`+${matches.length - maxLines} more`);
            }
          } else {
            const fallback = String(hoverCell.dataset.hoverTitle || "").trim();
            if (fallback) {
              tooltipLines.push(fallback);
            } else {
              tooltipLines.push("No active connection.");
            }
          }
          showMatrixHoverTooltip(tooltipTitle, tooltipLines, event.clientX, event.clientY);
        };

        matrixTable.onmouseleave = () => {
          clearMatrixAxisHover();
          hideMatrixHoverTooltip();
        };

        matrixTable.onclick = (event) => {
          const deviceToggleBtn = event.target.closest("[data-device-toggle]");
          if (deviceToggleBtn) {
            const axis = String(deviceToggleBtn.dataset.axis || "").toLowerCase();
            const key = String(deviceToggleBtn.dataset.device || "");
            const targetSet = axis === "dest" ? collapsedDestDevices : collapsedSourceDevices;
            if (targetSet.has(key)) {
              targetSet.delete(key);
              setStatus("Expanded device");
            } else {
              targetSet.add(key);
              setStatus("Collapsed device");
            }
            renderMatrix();
            return;
          }

          const toggleBtn = event.target.closest("[data-group-toggle]");
          if (toggleBtn) {
            const axis = String(toggleBtn.dataset.axis || "").toLowerCase();
            const key = String(toggleBtn.dataset.group || "");
            const targetSet = axis === "dest" ? collapsedDestGroups : collapsedSourceGroups;
            if (targetSet.has(key)) {
              targetSet.delete(key);
              setStatus("Expanded group");
            } else {
              targetSet.add(key);
              setStatus("Collapsed group");
            }
            renderMatrix();
            return;
          }

          const cell = event.target.closest("td.cell");
          if (!cell) return;
          if (suppressNextCellClick) {
            suppressNextCellClick = false;
            return;
          }

          const context = buildPatchContextFromCell(cell);
          if (!context) return;
          if (!context.editable) {
            setStatus("Expand collapsed group/device to route individual ports.", true);
            return;
          }

          if (selectedPatchMode === "paint") {
            // Paint mode is handled by pointer drag.
            return;
          }

          if (selectedPatchMode === "range") {
            if (!rangeSelectionAnchor) {
              rangeSelectionAnchor = {
                sourceIndex: context.sourceIndex,
                destIndex: context.destIndex,
              };
              clearRangeAnchorVisual();
              rangeAnchorCell = cell;
              rangeAnchorCell.classList.add("range-anchor");
              setStatus(`Range anchor set: ${context.source.device} [${context.source.port}] -> ${context.dest.device} [${context.dest.port}]`);
              return;
            }
            const contexts = buildRangeContexts(rangeSelectionAnchor, {
              sourceIndex: context.sourceIndex,
              destIndex: context.destIndex,
            });
            rangeSelectionAnchor = null;
            clearRangeAnchorVisual();
            if (!contexts.length) {
              setStatus("Range mode: no patchable ports in selected block.", true);
              return;
            }
            const bulkAction = deriveBulkPatchAction(family, contexts);
            const summary = applyPatchContexts(contexts, bulkAction);
            if (summary.changed > 0) {
              scheduleAutoSave(bulkAction === "connect" ? "range-connect" : "range-disconnect");
              setStatus(`Range ${bulkAction}: ${summary.changed} change(s)`);
              renderMatrix();
            } else {
              setStatus(summary.firstError || "Range mode: no changes.", Boolean(summary.firstError));
            }
            return;
          }

          let contexts = [context];
          if (selectedPatchMode === "stereo" || selectedPatchMode === "multi") {
            const wanted = selectedPatchMode === "stereo" ? 2 : normalizePairCount(pairCount);
            contexts = collectDiagonalContexts(context.sourceIndex, context.destIndex, wanted);
            if (!contexts.length) {
              setStatus("No patchable ports found for requested pair routing.", true);
              return;
            }
            const bulkAction = deriveBulkPatchAction(family, contexts);
            const summary = applyPatchContexts(contexts, bulkAction);
            if (summary.changed > 0) {
              scheduleAutoSave(bulkAction === "connect" ? "pair-connect" : "pair-disconnect");
              const label = selectedPatchMode === "stereo" ? "Stereo" : `Multi (${normalizePairCount(pairCount)})`;
              setStatus(`${label} ${bulkAction}: ${summary.changed} change(s)`);
              renderMatrix();
            } else {
              setStatus(summary.firstError || `${selectedPatchMode} mode: no changes.`, Boolean(summary.firstError));
            }
            return;
          }

          const summary = applyPatchContexts(contexts, "toggle", {
            reassignOccupiedPowerDestination: family === "POWER",
          });
          if (summary.changed > 0) {
            const saveReason = summary.moved > 0
              ? "power-move"
              : (summary.connected > 0 && summary.disconnected === 0 ? "connect" : "disconnect");
            scheduleAutoSave(saveReason);
            if (summary.moved > 0 && summary.connected === 0 && summary.disconnected === 0) {
              setStatus(`Moved POWER feed to ${context.source.device} [${context.source.port}] for ${context.dest.device} [${context.dest.port}]`);
            } else if (summary.connected > 0 && summary.disconnected === 0) {
              const advisory = powerConnectorAdvisory(context.linkFamily || family, context.source, context.dest);
              setStatus(`Connected ${context.source.device} [${context.source.port}] -> ${context.dest.device} [${context.dest.port}] (${context.linkFamily || family})${advisory ? ` — ${advisory}` : ""}`);
            } else if (summary.disconnected > 0 && summary.connected === 0) {
              setStatus(`Disconnected ${context.source.device} [${context.source.port}] -> ${context.dest.device} [${context.dest.port}]`);
            } else {
              setStatus(`Updated link ${context.source.device} [${context.source.port}] -> ${context.dest.device} [${context.dest.port}]`);
            }
            renderMatrix();
          } else {
            setStatus(summary.firstError || "No changes.", Boolean(summary.firstError));
          }
        };

        renderConnectionList();
        writeUiConfigToModel();
        syncMatrixHorizontalScroller();
        scheduleMatrixViewportHeightUpdate();
      } catch (error) {
        matrixContainer.innerHTML = `<div style="padding:12px;color:#b91c1c;">Matrix render error: ${esc(String(error))}</div>`;
        setStatus(`Matrix render error: ${String(error)}`, true);
        writeUiConfigToModel();
        syncMatrixHorizontalScroller();
        scheduleMatrixViewportHeightUpdate();
      }
    }

    function downloadConnections() {
      const payload = buildConnectionsPayload();
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "routing_matrix_connections.json";
      a.click();
      URL.revokeObjectURL(url);
      setStatus("Downloaded routing_matrix_connections.json");
    }

    function initFamilySelect(preferredFamily = "") {
      const available = new Set();
      for (const device of modelDevices) {
        if (!deviceVisibleForTarget(device, "wiring_matrix")) continue;
        for (const port of (portsByDevice.get(String(device?.name || "").trim()) || [])) {
          if (!port.visible) continue;
          for (const family of port.families) {
            if (FAMILY_ORDER.includes(family)) available.add(family);
          }
        }
      }
      const familyList = [FAMILY_ALL, ...FAMILY_ORDER.filter((family) => available.has(family))];
      familySelect.innerHTML = familyList.map((family) => `<option value="${family}">${family}</option>`).join("");
      const desired = String(preferredFamily || "").trim().toUpperCase();
      const defaultFamily = familyList.includes("AUDIO") ? "AUDIO" : (familyList[0] || "");
      if (desired && familyList.includes(desired)) {
        familySelect.value = desired;
      } else if (defaultFamily) {
        familySelect.value = defaultFamily;
      }
      let collapseStateFamily = String(familySelect.value || "").trim();
      familySelect.onchange = () => {
        const nextFamily = String(familySelect.value || "AUDIO").trim();
        carryCollapseStateBetweenFamilies(collapseStateFamily, nextFamily);
        collapseStateFamily = nextFamily;
        setStatus(`Showing ${familySelect.value} matrix`);
        renderMatrix();
      };
    }

    function loadEmptyModelTemplate() {
      const base = cloneJson(EMPTY_MODEL_TEMPLATE) || {};
      const previousTitle = String(MODEL?.title || "").trim();
      MODEL = (base && typeof base === "object") ? base : {};
      MODEL.version = Number.isFinite(Number(MODEL?.version)) ? Number(MODEL.version) : 1;
      MODEL.title = String(MODEL?.title || previousTitle || "Studio Sidecar").trim() || "Studio Sidecar";
      if (!MODEL.families || typeof MODEL.families !== "object") {
        MODEL.families = {
          AUDIO: { prefix: "AUDIO", layer: "Audio Analog", signal_type: "Analog Audio", default_cable_type: "Analog" },
          COMP: { prefix: "COMP", layer: "Computer/Data", signal_type: "Computer Data", default_cable_type: "Computer/Data" },
          DIGI: { prefix: "DIGI", layer: "Digital Audio", signal_type: "Digital Audio", default_cable_type: "Digital Audio" },
          NETWORK: { prefix: "NETWORK", layer: "Network", signal_type: "Network Data", default_cable_type: "Network" },
          POWER: { prefix: "POWER", layer: "Power", signal_type: "Mains Power", default_cable_type: "AC Power" },
        };
      }
      if (!Array.isArray(MODEL.devices)) MODEL.devices = [];
      connections = [];
      setBaselineConnections([]);
      MATRIX = { version: 1, connections: [] };
      selectedDeviceName = "";
      refreshFromModelEdit("Loaded empty model template");
      applyUiConfigFromModel();
      initFamilySelect(preferredFamilyFromConfig || "AUDIO");
      applyThemeMode(selectedThemeMode, false);
      applyMatrixScale(false);
      setDevicePortTab(devicePortTab);
      showMatrixSubTab(selectedMatrixSubTab);
      renderMatrix();
    }

    const resetBtn = document.getElementById("resetBtn");
    if (exportJsonBtn) exportJsonBtn.onclick = () => {
      downloadModelJson();
      window.setTimeout(() => downloadConnections(), 120);
      setStatus("Exported model + connections JSON");
    };
    if (importJsonBtn) importJsonBtn.onclick = () => {
      if (importModelFile) importModelFile.click();
    };
    if (loadEmptyTemplateBtn) loadEmptyTemplateBtn.onclick = () => {
      const confirmLoad = window.confirm(
        "Load empty model template? This clears all current devices and connections in the UI state.",
      );
      if (!confirmLoad) return;
      loadEmptyModelTemplate();
    };
    if (resetBtn) resetBtn.onclick = () => {
      connections = baselineConnections.map((row) => ({ ...row }));
      collapsedSourceGroups.clear();
      collapsedDestGroups.clear();
      collapsedSourceDevices.clear();
      collapsedDestDevices.clear();
      autoCollapsedFamilies.clear();
      sourceFilterInput.value = "";
      destFilterInput.value = "";
      const removed = pruneConnectionsToKnownPorts();
      const message = removed > 0
        ? `Reset matrix state (${removed} invalid connection(s) removed)`
        : "Reset to initial matrix state";
      setStatus(message, removed > 0);
      scheduleAutoSave("reset");
      renderMatrix();
    };
    if (collapseGroupsBtn) collapseGroupsBtn.onclick = () => {
      const family = String(familySelect.value || "AUDIO");
      for (const key of collectCollapsibleGroupKeys(family, true)) collapsedSourceGroups.add(key);
      for (const key of collectCollapsibleGroupKeys(family, false)) collapsedDestGroups.add(key);
      setStatus(`Collapsed grouped ports for ${family}`);
      renderMatrix();
    };
    if (expandGroupsBtn) expandGroupsBtn.onclick = () => {
      const family = String(familySelect.value || "AUDIO");
      clearCollapsedGroupsForFamily(family);
      setStatus(`Expanded grouped ports for ${family}`);
      renderMatrix();
    };
    if (collapseDevicesBtn) collapseDevicesBtn.onclick = () => {
      const family = String(familySelect.value || "AUDIO");
      for (const key of collectCollapsibleDeviceKeys(family, true)) collapsedSourceDevices.add(key);
      for (const key of collectCollapsibleDeviceKeys(family, false)) collapsedDestDevices.add(key);
      setStatus(`Collapsed devices for ${family}`);
      renderMatrix();
    };
    if (expandDevicesBtn) expandDevicesBtn.onclick = () => {
      const family = String(familySelect.value || "AUDIO");
      clearCollapsedDevicesForFamily(family);
      setStatus(`Expanded devices for ${family}`);
      renderMatrix();
    };
    if (patchModeSelect) patchModeSelect.onchange = () => {
      setPatchMode(patchModeSelect.value);
      const labels = {
        single: "Single",
        paint: "Paint",
        range: "Range",
        stereo: "Stereo Pair",
        multi: "Multi Pair",
      };
      setStatus(`Patch mode: ${labels[selectedPatchMode] || selectedPatchMode}`);
      writeUiConfigToModel();
      scheduleAutoSave("patch-mode");
    };
    if (pairCountInput) pairCountInput.onchange = () => {
      setPairCount(pairCountInput.value);
      if (selectedPatchMode === "multi") {
        setStatus(`Multi pair size: ${pairCount}`);
      }
      writeUiConfigToModel();
      scheduleAutoSave("pair-count");
    };
    if (sourceFilterInput) sourceFilterInput.oninput = () => {
      renderMatrix();
    };
    if (destFilterInput) destFilterInput.oninput = () => {
      renderMatrix();
    };
    if (resetScaleBtn) resetScaleBtn.onclick = () => {
      writeScaleControls({ ...DEFAULT_MATRIX_SCALE });
      applyMatrixScale(true);
      setStatus("Reset matrix scale to default");
    };

    if (importModelFile) importModelFile.onchange = async () => {
      const file = importModelFile.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.devices)) {
          throw new Error("Expected model JSON object with a devices array");
        }
        MODEL = cloneJson(parsed) || { devices: [] };
        refreshFromModelEdit(`Loaded model from ${file.name}`);
        setBaselineConnections(connections);
        applyUiConfigFromModel();
        initFamilySelect(preferredFamilyFromConfig || String(familySelect?.value || ""));
        applyThemeMode(selectedThemeMode, false);
        applyMatrixScale(false);
        setDevicePortTab(devicePortTab);
        showMatrixSubTab(selectedMatrixSubTab);
        renderMatrix();
        if (importConnectionsFile) importConnectionsFile.click();
      } catch (error) {
        setStatus(`Failed to load model JSON: ${String(error)}`, true);
      } finally {
        importModelFile.value = "";
      }
    };

    if (importConnectionsFile) importConnectionsFile.onchange = async () => {
      const file = importConnectionsFile.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        const rows = Array.isArray(parsed?.connections) ? parsed.connections : (Array.isArray(parsed) ? parsed : null);
        if (!rows) throw new Error("No connections array found");
        connections = rows.map(normalizeConnection).filter((row) => row.source_device && row.source_port && row.dest_device && row.dest_port);
        setBaselineConnections(connections);
        const removed = pruneConnectionsToKnownPorts();
        const warn = removed > 0;
        const suffix = removed > 0 ? ` (${removed} invalid connection(s) removed)` : "";
        setStatus(`Loaded ${connections.length} connections from ${file.name}${suffix}`, warn);
        scheduleAutoSave("import-connections");
        renderMatrix();
      } catch (error) {
        setStatus(`Failed to load connections JSON: ${String(error)}`, true);
      } finally {
        importConnectionsFile.value = "";
      }
    };

    if (mainTabMatrix) mainTabMatrix.onclick = () => showMainTab("matrix");
    if (mainTabDevices) mainTabDevices.onclick = () => showMainTab("devices");
    if (mainTabRack) mainTabRack.onclick = () => showMainTab("rack-editor");
    if (mainTabVisibility) mainTabVisibility.onclick = () => showMainTab("visibility");
    if (mainTabVisuals) mainTabVisuals.onclick = () => showMainTab("visuals");
    if (matrixSubTabPatch) matrixSubTabPatch.onclick = () => showMatrixSubTab("patch");
    if (matrixSubTabConnections) matrixSubTabConnections.onclick = () => showMatrixSubTab("connections");
    if (portTabInputs) portTabInputs.onclick = () => {
      if (devicePortTab === "in" || commitPendingDeviceEditorEdits("Saved before switching port view")) {
        setDevicePortTab("in");
      }
    };
    if (portTabOutputs) portTabOutputs.onclick = () => {
      if (devicePortTab === "out" || commitPendingDeviceEditorEdits("Saved before switching port view")) {
        setDevicePortTab("out");
      }
    };
    if (downloadModelBtn) downloadModelBtn.onclick = () => downloadModelJson();
    if (regeneratePreviewsBtn) regeneratePreviewsBtn.onclick = async () => {
      if (!saveApiEnabled) {
        setStatus("Regenerate unavailable. Start routing_matrix_server.py and open via http://127.0.0.1:8000.", true);
        return;
      }
      if (!regenerateApiEnabled) {
        refreshVisualPreviews("manual-refresh-only");
        setStatus("Regenerate endpoint unavailable on server; refreshed previews only.", true);
        return;
      }
      await saveJsonToDisk("manual-regenerate", false, true);
    };
    if (downloadSvgsBtn) downloadSvgsBtn.onclick = async () => {
      await downloadAllSvgPreviews();
    };
    for (const spec of PREVIEW_SPECS) {
      const dom = PREVIEW_DOM[spec.key] || {};
      if (dom.link) {
        dom.link.addEventListener("click", () => {
          const base = resolvePreviewBasePath(spec.key);
          if (base) dom.link.href = appendCacheBuster(base, "_open", Date.now());
        });
      }
      const button = document.querySelector(`[data-preview-download="${spec.key}"]`);
      if (button) {
        button.addEventListener("click", async () => {
          const success = await downloadPreviewSvg(resolvePreviewBasePath(spec.key), spec.file);
          setStatus(success
            ? `Downloaded current ${spec.label} SVG.`
            : `Failed to download ${spec.label} SVG.`, !success);
        });
      }
    }
    if (openRouteDebugBtn) openRouteDebugBtn.onclick = () => {
      openRouteDebugJson();
    };
    if (themeToggleBtn) themeToggleBtn.onclick = () => {
      const next = selectedThemeMode === "dark" ? "light" : "dark";
      applyThemeMode(next, true);
    };
    if (createProjectBtn) createProjectBtn.onclick = async () => {
      await createNewProject();
    };
    if (createDeviceConfigBtn) createDeviceConfigBtn.onclick = async () => {
      await createNewDeviceConfig();
    };
    if (createPatchConfigBtn) createPatchConfigBtn.onclick = async () => {
      await createNewPatchConfig();
    };
    if (saveDeviceConfigAsBtn) saveDeviceConfigAsBtn.onclick = async () => {
      await saveCurrentModelAs();
    };
    if (savePatchConfigAsBtn) savePatchConfigAsBtn.onclick = async () => {
      await saveCurrentPatchAs();
    };
    if (projectSelect) projectSelect.onchange = async () => {
      if (applyingProjectSelectors) return;
      const nextKey = String(projectSelect.value || "").trim();
      const project = findProjectByKey(nextKey);
      if (!project) return;
      selectedProjectKey = nextKey;
      const projectDeviceFiles = Array.isArray(project?.device_configs) ? project.device_configs : [];
      const projectPatchFiles = Array.isArray(project?.patch_configs) ? project.patch_configs : [];
      if (!projectDeviceFiles.includes(selectedModelPath)) {
        selectedModelPath = String(project?.default_device_config || projectDeviceFiles[0] || "").trim();
      }
      if (!projectPatchFiles.includes(selectedConnectionsPath)) {
        selectedConnectionsPath = String(project?.default_patch_config || projectPatchFiles[0] || "").trim();
      }
      updateProjectSelectorsFromConfig({
        projects: projectCatalog,
        active_project_key: selectedProjectKey,
        model_path: selectedModelPath,
        connections_path: selectedConnectionsPath,
      });
      await applySelectorTargets({
        projectKey: selectedProjectKey,
        modelPath: selectedModelPath,
        patchPath: selectedConnectionsPath,
        reason: "project-select",
      });
    };
    if (deviceConfigSelect) deviceConfigSelect.onchange = async () => {
      if (applyingProjectSelectors) return;
      selectedModelPath = String(deviceConfigSelect.value || "").trim();
      if (!selectedProjectKey) {
        selectedProjectKey = inferProjectKeyFromPaths(selectedModelPath, selectedConnectionsPath);
      }
      await applySelectorTargets({
        projectKey: selectedProjectKey,
        modelPath: selectedModelPath,
        patchPath: selectedConnectionsPath,
        reason: "device-config-select",
      });
    };
    if (patchConfigSelect) patchConfigSelect.onchange = async () => {
      if (applyingProjectSelectors) return;
      selectedConnectionsPath = String(patchConfigSelect.value || "").trim();
      if (!selectedProjectKey) {
        selectedProjectKey = inferProjectKeyFromPaths(selectedModelPath, selectedConnectionsPath);
      }
      await applySelectorTargets({
        projectKey: selectedProjectKey,
        modelPath: selectedModelPath,
        patchPath: selectedConnectionsPath,
        reason: "patch-config-select",
      });
    };

    if (visibilityListPanel) {
      visibilityListPanel.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.type !== "checkbox") return;
        const deviceName = String(target.getAttribute("data-visibility-device") || "").trim();
        const targetName = String(target.getAttribute("data-visibility-target") || "").trim();
        if (!deviceName || !targetName) return;
        const metaKey = Boolean(event.metaKey || event.ctrlKey);
        lastVisibilityCheckboxClickMeta = {
          device: deviceName,
          target: targetName,
          shiftKey: Boolean(event.shiftKey),
          metaKey,
          altKey: Boolean(event.altKey),
          ts: Date.now(),
        };
        updateVisibilitySelection(deviceName, {
          shiftKey: Boolean(event.shiftKey),
          metaKey,
          altKey: Boolean(event.altKey),
        });
      }, true);
    }

    if (visibilityListPanel) visibilityListPanel.onchange = (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const toggle = target.closest("[data-visibility-device]");
      if (!(toggle instanceof HTMLInputElement)) return;
      const deviceName = String(toggle.getAttribute("data-visibility-device") || "").trim();
      const targetName = String(toggle.getAttribute("data-visibility-target") || "").trim();
      if (!deviceName || !targetName) return;
      const clickMeta = consumeVisibilityCheckboxClickMeta(deviceName, targetName);
      const checked = Boolean(toggle.checked);
      const affectedNames = clickMeta.altKey
        ? visibilityDeviceNamesInOrder()
        : (selectedVisibilityDevices.has(deviceName) && selectedVisibilityDevices.size > 1
          ? Array.from(selectedVisibilityDevices)
          : [deviceName]);
      let changed = 0;
      for (const name of affectedNames) {
        const device = getDeviceByName(name);
        if (!device) continue;
        if (setDeviceVisibilityForTarget(device, targetName, checked)) changed += 1;
      }
      if (changed <= 0) {
        setStatus(`No visibility change for ${deviceName}`);
        renderVisibilityPanel();
        return;
      }
      const targetLabel = DEVICE_VISIBILITY_TARGETS.find((entry) => entry.key === targetName)?.label || targetName;
      const label = checked ? "Shown" : "Hidden";
      const rangeSuffix = affectedNames.length > 1 ? ` (${affectedNames.length} devices)` : "";
      refreshFromModelEdit(`${label} in ${targetLabel}: ${deviceName}${rangeSuffix}`);
    };

    if (visibilityListPanel) visibilityListPanel.onclick = (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const editBtn = target.closest("[data-visibility-select]");
      if (editBtn) {
        const deviceName = String(editBtn.getAttribute("data-visibility-select") || "").trim();
        if (!deviceName) return;
        selectedVisibilityDevices.clear();
        lastVisibilitySelectionAnchor = "";
        selectedDeviceName = deviceName;
        showMainTab("devices");
        return;
      }
      if (target.closest('input[type="checkbox"], .visibility-drag-grip')) return;
      const row = target.closest("[data-visibility-row]");
      if (!(row instanceof HTMLElement)) return;
      const deviceName = String(row.getAttribute("data-visibility-row") || "").trim();
      updateVisibilitySelection(deviceName, {
        shiftKey: Boolean(event.shiftKey),
        metaKey: Boolean(event.metaKey || event.ctrlKey),
        altKey: Boolean(event.altKey),
      });
    };

    if (visibilityListPanel) visibilityListPanel.ondragstart = (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const grip = target.closest(".visibility-drag-grip");
      if (!grip) {
        event.preventDefault();
        return;
      }
      const row = grip.closest("[data-visibility-row]");
      if (!(row instanceof HTMLElement)) return;
      const deviceName = String(row.getAttribute("data-visibility-row") || "").trim();
      if (!deviceName) return;
      visibilityDragDeviceName = deviceName;
      clearVisibilityDragIndicators();
      row.classList.add("dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", deviceName);
      }
    };

    if (visibilityListPanel) visibilityListPanel.ondragover = (event) => {
      const row = event.target instanceof HTMLElement
        ? event.target.closest("[data-visibility-row]")
        : null;
      if (!(row instanceof HTMLElement)) return;
      const targetName = String(row.getAttribute("data-visibility-row") || "").trim();
      if (!targetName || targetName === visibilityDragDeviceName) return;
      event.preventDefault();
      const bounds = row.getBoundingClientRect();
      const placeAfter = (event.clientY - bounds.top) >= (bounds.height / 2.0);
      clearVisibilityDragIndicators();
      row.classList.add(placeAfter ? "drag-over-after" : "drag-over-before");
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    };

    if (visibilityListPanel) visibilityListPanel.ondrop = (event) => {
      event.preventDefault();
      const row = event.target instanceof HTMLElement
        ? event.target.closest("[data-visibility-row]")
        : null;
      if (!(row instanceof HTMLElement)) {
        clearVisibilityDragIndicators();
        visibilityDragDeviceName = "";
        return;
      }
      const targetName = String(row.getAttribute("data-visibility-row") || "").trim();
      const sourceName = visibilityDragDeviceName
        || String(event.dataTransfer?.getData("text/plain") || "").trim();
      if (!sourceName || !targetName || sourceName === targetName) {
        clearVisibilityDragIndicators();
        visibilityDragDeviceName = "";
        return;
      }
      const placeAfter = row.classList.contains("drag-over-after");
      const moved = moveDeviceInModelOrder(sourceName, targetName, placeAfter);
      clearVisibilityDragIndicators();
      visibilityDragDeviceName = "";
      if (!moved) return;
      const relation = placeAfter ? "after" : "before";
      refreshFromModelEdit(`Moved ${sourceName} ${relation} ${targetName}`);
      renderVisibilityPanel();
    };

    if (visibilityListPanel) visibilityListPanel.ondragend = () => {
      clearVisibilityDragIndicators();
      visibilityDragDeviceName = "";
    };

    if (showAllDevicesBtn) showAllDevicesBtn.onclick = () => {
      let changed = false;
      for (const device of ensureModelDeviceArray()) {
        for (const target of DEVICE_VISIBILITY_TARGETS) {
          if (setDeviceVisibilityForTarget(device, target.key, true)) changed = true;
        }
        if (device.visible !== true || device.hidden === true) changed = true;
        device.visible = true;
        device.hidden = false;
      }
      if (!changed) {
        setStatus("All devices are already shown everywhere.");
        return;
      }
      refreshFromModelEdit("Showed all devices everywhere");
      renderVisibilityPanel();
    };

    if (hideAllDevicesBtn) hideAllDevicesBtn.onclick = () => {
      let changed = false;
      for (const device of ensureModelDeviceArray()) {
        const name = String(device?.name || "").trim();
        if (!name) continue;
        for (const target of DEVICE_VISIBILITY_TARGETS) {
          if (setDeviceVisibilityForTarget(device, target.key, false)) changed = true;
        }
        if (device.visible !== false || device.hidden !== true) changed = true;
        device.visible = false;
        device.hidden = true;
      }
      if (!changed) {
        setStatus("All devices are already hidden everywhere.");
        return;
      }
      refreshFromModelEdit("Hid all devices everywhere");
      renderVisibilityPanel();
    };

    if (invertVisibleDevicesBtn) invertVisibleDevicesBtn.onclick = () => {
      const devices = ensureModelDeviceArray();
      if (!devices.length) {
        setStatus("No devices available.", true);
        return;
      }
      for (const device of devices) {
        for (const target of DEVICE_VISIBILITY_TARGETS) {
          setDeviceVisibilityForTarget(device, target.key, !deviceVisibleForTarget(device, target.key));
        }
      }
      refreshFromModelEdit("Inverted all device visibility targets");
      renderVisibilityPanel();
    };
    if (prefersDarkMedia && typeof prefersDarkMedia.addEventListener === "function") {
      prefersDarkMedia.addEventListener("change", (event) => {
        if (hasExplicitThemePreference) return;
        applyThemeMode(event.matches ? "dark" : "light", false);
      });
    }

    if (addDeviceBtn) addDeviceBtn.onclick = () => {
      const rawName = String(newDeviceName?.value || "").trim();
      if (!rawName) {
        setStatus("Device name is required.", true);
        return;
      }
      if (getDeviceByName(rawName)) {
        setStatus(`Device already exists: ${rawName}`, true);
        return;
      }
      const rawType = String(newDeviceType?.value || "").trim() || "Other";
      const location = normalizeDeviceLocation(newDeviceLocation?.value);
      const rackUnits = Number(newDeviceRackUnits?.value);
      const rackMountable = Boolean(newDeviceRackMountable?.checked);
      if (location === "Rack" && !rackMountable) {
        setStatus("Mark the device as Rack mountable before assigning it to Rack.", true);
        return;
      }
      if (!Number.isInteger(rackUnits) || rackUnits < 1 || rackUnits > 16) {
        setStatus("Device height must be a whole number from 1 to 16 U/HE.", true);
        return;
      }
      const devices = ensureModelDeviceArray();
      devices.push({
        name: rawName,
        device_type: rawType,
        layout_group: rawType,
        location,
        rack_mountable: rackMountable,
        rack_units: rackUnits,
        ports: [],
      });
      sortDevicesInModel();
      selectedDeviceName = rawName;
      if (newDeviceName) newDeviceName.value = "";
      if (newDeviceLocation) newDeviceLocation.value = "Desk";
      if (newDeviceRackUnits) newDeviceRackUnits.value = "1";
      if (newDeviceRackMountable) newDeviceRackMountable.checked = false;
      refreshFromModelEdit(`Added device: ${rawName}`);
      showMainTab("devices");
    };

    if (saveDeviceMetaBtn) saveDeviceMetaBtn.onclick = () => {
      if (commitPendingDeviceEditorEdits("Saved device")) {
        flushPendingDeviceEditorAutoSave("save-device-button");
      }
    };

    if (rackEditorDeviceSelect) rackEditorDeviceSelect.onchange = () => {
      const name = String(rackEditorDeviceSelect.value || "").trim();
      const device = getDeviceByName(name);
      if (!device) return;
      selectedDeviceName = name;
      syncRackEditorControlsFromDevice(device);
      renderRackEditor({ deviceName: name });
      setRackEditorStatus(`Selected ${name}.`);
    };

    if (rackEditorLocationSelect) rackEditorLocationSelect.onchange = () => {
      const rackFieldsDisabled = normalizeDeviceLocation(rackEditorLocationSelect.value) !== "Rack";
      if (rackEditorRackSelect) rackEditorRackSelect.disabled = rackFieldsDisabled;
      if (rackEditorStartUSelect) rackEditorStartUSelect.disabled = rackFieldsDisabled;
    };

    if (newDeviceRackMountable) newDeviceRackMountable.onchange = () => {
      if (!newDeviceRackMountable.checked && newDeviceLocation) newDeviceLocation.value = "Desk";
    };

    if (deviceRackMountableInput) deviceRackMountableInput.onchange = () => {
      if (!deviceRackMountableInput.checked && deviceLocationInput) deviceLocationInput.value = "Desk";
    };

    if (applyRackPlacementBtn) applyRackPlacementBtn.onclick = () => {
      applyRackEditorPlacement();
    };

    if (removeRackPlacementBtn) removeRackPlacementBtn.onclick = () => {
      const name = String(rackEditorDeviceSelect?.value || "").trim();
      const device = getDeviceByName(name);
      if (!isRackMountableDevice(device)) {
        setRackEditorStatus("Choose gear marked as Rack mountable.", true);
        return;
      }
      device.location = "Rack";
      device.rack_units = normalizeRackUnits(rackEditorUnitsInput?.value || device.rack_units);
      delete device.rack_position;
      refreshFromModelEdit(`Removed ${name} from rack`);
      renderRackEditor({ deviceName: name });
      setRackEditorStatus(`${name} remains a Rack device and is now unplaced.`);
    };

    if (panelRack) panelRack.onclick = (event) => {
      const selectButton = event.target instanceof HTMLElement
        ? event.target.closest("[data-rack-select-device]")
        : null;
      if (!(selectButton instanceof HTMLElement)) return;
      const name = String(selectButton.getAttribute("data-rack-select-device") || "").trim();
      const device = getDeviceByName(name);
      if (!device) return;
      selectedDeviceName = name;
      renderRackEditor({ deviceName: name });
      setRackEditorStatus(`Selected ${name}.`);
    };

    if (panelRack) panelRack.ondragstart = (event) => {
      const source = event.target instanceof HTMLElement
        ? event.target.closest("[data-rack-drag-device]")
        : null;
      if (!(source instanceof HTMLElement) || !event.dataTransfer) return;
      const name = String(source.getAttribute("data-rack-drag-device") || "").trim();
      const device = getDeviceByName(name);
      if (!isRackMountableDevice(device)) {
        event.preventDefault();
        setRackEditorStatus("Only rack-mountable devices can be dragged into racks.", true);
        return;
      }
      const units = normalizeRackUnits(device.rack_units);
      let grabOffsetU = 0;
      if (source.classList.contains("rack-device-block")) {
        const rect = source.getBoundingClientRect();
        if (rect.height > 0) {
          const relativeY = Math.max(0, Math.min(rect.height - 0.001, event.clientY - rect.top));
          const offsetFromTop = Math.max(0, Math.min(units - 1, Math.floor((relativeY / rect.height) * units)));
          grabOffsetU = units - 1 - offsetFromTop;
        }
      }
      rackDragState = { deviceName: name, units, grabOffsetU };
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", name);
      try {
        event.dataTransfer.setData("application/x-studio-rack-device", name);
      } catch (_error) {
        // Some Safari versions reject custom drag MIME types; text/plain remains portable.
      }
      source.classList.add("rack-dragging");
      for (const grid of Array.from(panelRack.querySelectorAll("[data-rack-drop-rack]"))) {
        grid.classList.add("rack-drop-target");
      }
      selectedDeviceName = name;
      setRackEditorStatus(`Dragging ${name}. Drop it on a rack unit.`);
    };

    if (panelRack) panelRack.ondragover = (event) => {
      const grid = event.target instanceof HTMLElement
        ? event.target.closest("[data-rack-drop-rack]")
        : null;
      if (!(grid instanceof HTMLElement) || !rackDragState) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      for (const candidate of Array.from(panelRack.querySelectorAll("[data-rack-drop-rack]"))) {
        candidate.classList.remove("rack-drop-valid", "rack-drop-invalid");
      }
      const rack = Number(grid.getAttribute("data-rack-drop-rack"));
      const placement = rackDropPlacementFromPointer(
        grid,
        event.clientY,
        rackDragState.units,
        rackDragState.grabOffsetU,
      );
      const check = placement
        ? canPlaceRackDevice(rackDragState.deviceName, rack, placement.start_u, rackDragState.units)
        : { ok: false };
      grid.classList.add(check.ok ? "rack-drop-valid" : "rack-drop-invalid");
    };

    if (panelRack) panelRack.ondragleave = (event) => {
      const grid = event.target instanceof HTMLElement
        ? event.target.closest("[data-rack-drop-rack]")
        : null;
      if (!(grid instanceof HTMLElement)) return;
      const related = event.relatedTarget;
      if (related instanceof Node && grid.contains(related)) return;
      grid.classList.remove("rack-drop-valid", "rack-drop-invalid");
    };

    if (panelRack) panelRack.ondrop = (event) => {
      const grid = event.target instanceof HTMLElement
        ? event.target.closest("[data-rack-drop-rack]")
        : null;
      if (!(grid instanceof HTMLElement) || !rackDragState) return;
      event.preventDefault();
      const { deviceName, units, grabOffsetU } = rackDragState;
      const rack = Number(grid.getAttribute("data-rack-drop-rack"));
      const placement = rackDropPlacementFromPointer(grid, event.clientY, units, grabOffsetU);
      if (!placement) {
        setRackEditorStatus(`Could not determine a rack unit for ${deviceName}. Placement unchanged.`, true);
        finishRackDrag();
        return;
      }
      moveRackDeviceToPosition(deviceName, rack, placement.start_u);
      finishRackDrag();
    };

    if (panelRack) panelRack.ondragend = () => {
      if (rackDragState) finishRackDrag(true);
    };

    if (deviceListPanel) deviceListPanel.onclick = (event) => {
      const selectBtn = event.target.closest("[data-select-device]");
      if (selectBtn) {
        const nextDeviceName = String(selectBtn.getAttribute("data-select-device") || "");
        if (
          nextDeviceName !== selectedDeviceName
          && !commitPendingDeviceEditorEdits("Saved before selecting another device")
        ) return;
        selectedDeviceName = nextDeviceName;
        renderDeviceList();
        renderDeviceEditor();
        return;
      }
      const removeBtn = event.target.closest("[data-remove-device]");
      if (!removeBtn) return;
      const targetName = String(removeBtn.getAttribute("data-remove-device") || "");
      if (!targetName) return;
      const device = getDeviceByName(targetName);
      if (!device) return;
      const devices = ensureModelDeviceArray();
      const idx = devices.findIndex((row) => String(row?.name || "") === targetName);
      if (idx < 0) return;
      devices.splice(idx, 1);
      const before = connections.length;
      connections = connections.filter((row) => row.source_device !== targetName && row.dest_device !== targetName);
      const removed = before - connections.length;
      if (selectedDeviceName === targetName) selectedDeviceName = "";
      refreshFromModelEdit(`Removed device: ${targetName}${removed > 0 ? ` (${removed} connection(s) removed)` : ""}`);
    };

    if (deviceEditorPanel) {
      deviceEditorPanel.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (target.type !== "checkbox") return;
        const visibleIndexAttr = target.getAttribute("data-port-visible");
        if (visibleIndexAttr != null) {
          lastPortCheckboxClickMeta = {
            attr: "data-port-visible",
            index: Number(visibleIndexAttr),
            shiftKey: Boolean(event.shiftKey),
            ts: Date.now(),
          };
          return;
        }
        const enabledIndexAttr = target.getAttribute("data-port-enabled");
        if (enabledIndexAttr != null) {
          lastPortCheckboxClickMeta = {
            attr: "data-port-enabled",
            index: Number(enabledIndexAttr),
            shiftKey: Boolean(event.shiftKey),
            ts: Date.now(),
          };
        }
      }, true);
    }

    if (deviceEditorPanel) deviceEditorPanel.onchange = (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;

      if (target.id === "addPortTypeSelect") {
        applyPresetToAddForm();
        return;
      }

      const device = ensureSelectedDevice();
      if (!device) return;
      const ports = ensureDevicePortsArray(device);

      const renameIndexAttr = target.getAttribute("data-port-name");
      if (renameIndexAttr != null) {
        const idx = Number(renameIndexAttr);
        if (!Number.isInteger(idx) || !ports[idx]) return;
        const nextName = String(target.value || "").trim();
        if (!nextName) {
          setStatus("Port name cannot be empty.", true);
          target.value = String(ports[idx]?.name || "");
          return;
        }
        ports[idx].name = nextName;
        refreshFromModelEdit(`Updated port name on ${String(device.name || "")}`);
        return;
      }

      const familyIndexAttr = target.getAttribute("data-port-family");
      if (familyIndexAttr != null) {
        const idx = Number(familyIndexAttr);
        if (!Number.isInteger(idx) || !ports[idx]) return;
        ports[idx].families = [normalizeFamily(target.value || "AUDIO")];
        refreshFromModelEdit(`Updated port family on ${String(device.name || "")}`);
        return;
      }

      const visibleIndexAttr = target.getAttribute("data-port-visible");
      if (visibleIndexAttr != null) {
        const idx = Number(visibleIndexAttr);
        if (!Number.isInteger(idx) || !ports[idx] || !(target instanceof HTMLInputElement)) return;
        const checked = Boolean(target.checked);
        const shiftKey = consumePortCheckboxShift("data-port-visible", idx);
        const changed = applyPortCheckboxRange(device, ports, "data-port-visible", target, checked, shiftKey);
        if (changed <= 0) {
          setStatus(`No port visibility change on ${String(device.name || "")}`);
          return;
        }
        const suffix = changed > 1 ? ` (${changed} ports)` : "";
        refreshFromModelEdit(`Updated port visibility on ${String(device.name || "")}${suffix}`);
        return;
      }

      const enabledIndexAttr = target.getAttribute("data-port-enabled");
      if (enabledIndexAttr != null) {
        const idx = Number(enabledIndexAttr);
        if (!Number.isInteger(idx) || !ports[idx] || !(target instanceof HTMLInputElement)) return;
        const checked = Boolean(target.checked);
        const shiftKey = consumePortCheckboxShift("data-port-enabled", idx);
        const changed = applyPortCheckboxRange(device, ports, "data-port-enabled", target, checked, shiftKey);
        if (changed <= 0) {
          setStatus(`No port enable change on ${String(device.name || "")}`);
          return;
        }
        const suffix = changed > 1 ? ` (${changed} ports)` : "";
        refreshFromModelEdit(`Updated port enable state on ${String(device.name || "")}${suffix}`);
        return;
      }

      const transportIndexAttr = target.getAttribute("data-port-transport");
      if (transportIndexAttr != null) {
        const idx = Number(transportIndexAttr);
        if (!Number.isInteger(idx) || !ports[idx]) return;
        ports[idx].transport = String(target.value || "").trim();
        refreshFromModelEdit(`Updated port transport on ${String(device.name || "")}`);
      }
    };

    if (deviceEditorPanel) deviceEditorPanel.onclick = (event) => {
      const addBtn = event.target.closest("#addPortsBtn");
      if (addBtn) {
        addPortsFromEditor();
        return;
      }
      const removeBtn = event.target.closest("[data-port-remove]");
      if (!removeBtn) return;
      const idx = Number(removeBtn.getAttribute("data-port-remove"));
      const device = ensureSelectedDevice();
      if (!device || !Number.isInteger(idx)) return;
      const ports = ensureDevicePortsArray(device);
      if (!ports[idx]) return;
      const portName = String(ports[idx]?.name || "");
      ports.splice(idx, 1);
      refreshFromModelEdit(`Removed port ${portName} on ${String(device.name || "")}`);
    };

    window.addEventListener("message", (event) => {
      const data = event?.data;
      if (!data || typeof data !== "object") return;
      if (data.type === "studio-shell-main-tab-set") {
        applyExternalMainTab(data.tab, data.matrix_subtab);
        return;
      }
      if (data.type === "studio-theme-set") {
        applyThemeMode(String(data.mode || ""), false);
        return;
      }
      if (data.type === "studio-shell-autosave-set") {
        setAutoSaveEnabled(Boolean(data.enabled), false);
        try {
          window.parent.postMessage({ type: "studio-shell-autosave-changed", enabled: autoSaveEnabled }, "*");
        } catch (_error) {
          // Ignore parent messaging failures.
        }
        return;
      }
      if (data.type === "studio-shell-autosave-request") {
        try {
          window.parent.postMessage({ type: "studio-shell-autosave-state", enabled: autoSaveEnabled }, "*");
        } catch (_error) {
          // Ignore parent messaging failures.
        }
        return;
      }
      if (data.type === "studio-shell-autosave-flush") {
        const requestId = String(data.request_id || "");
        void flushAutoSaveForShell(String(data.reason || "shell-navigation")).then((ok) => {
          try {
            window.parent.postMessage({
              type: "studio-shell-autosave-flushed",
              request_id: requestId,
              ok: Boolean(ok),
            }, "*");
          } catch (_error) {
            // Ignore parent messaging failures.
          }
        });
      }
    });

    window.addEventListener("error", (event) => {
      const message = String(event?.message || event || "Unknown runtime error");
      captureDebugIssue("error", message, {
        file: String(event?.filename || ""),
        line: Number(event?.lineno || 0),
        column: Number(event?.colno || 0),
      });
      setStatus(`Runtime error: ${message}`, true);
    });
    window.addEventListener("unhandledrejection", (event) => {
      const reasonText = String(event?.reason || "Unknown rejection");
      captureDebugIssue("unhandledrejection", reasonText);
      setStatus(`Runtime promise error: ${reasonText}`, true);
    });
    window.addEventListener("resize", () => {
      scheduleMatrixViewportHeightUpdate();
      syncMatrixHorizontalScroller();
    });
    window.addEventListener("blur", () => {
      void flushAutoSaveForShell("application-window-blur");
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "hidden") return;
      void flushAutoSaveForShell("application-hidden");
    });

    bindDebugTools();
    refreshDebugReportPanel();
    bindMatrixHorizontalScrollerSync();
    bindMatrixHorizontalScroll();
    const storedTheme = loadThemePreference();
    if (storedTheme) {
      hasExplicitThemePreference = true;
      selectedThemeMode = storedTheme;
    } else {
      hasExplicitThemePreference = false;
      selectedThemeMode = resolveThemeMode("");
    }
    writeScaleControls(loadPersistedScale());
    setPatchMode(DEFAULT_PATCH_MODE);
    setPairCount(DEFAULT_PAIR_COUNT);
    applyUiConfigFromModel();
    initFamilySelect(preferredFamilyFromConfig);
    applyDestOrientation();
    applyThemeMode(selectedThemeMode, false);
    applyMatrixScale(false);
    autoSaveEnabled = loadAutoSavePreference();
    applySaveControlsState();
    renderDeviceList();
    renderDeviceEditor();
    renderRackEditor();
    renderVisibilityPanel();
    const initialUrlParameters = new URLSearchParams(window.location.search || "");
    document.documentElement.classList.toggle("embedded-mode", initialUrlParameters.get("embedded") === "1");
    showMatrixSubTab(selectedMatrixSubTab);
    applyExternalMainTab(initialUrlParameters.get("tab") || "matrix", initialUrlParameters.get("matrix_subtab") || "");
    refreshVisualPreviews("init");
    setStatus("Connecting to save API...");
    detectSaveApi();
    renderMatrix();
  </script>
</body>
</html>
"""
    return (
        template
        .replace("__TITLE__", safe_title)
        .replace("__DATE__", generated_iso)
        .replace("__MODEL_JSON__", model_json)
        .replace("__EMPTY_MODEL_TEMPLATE_JSON__", empty_model_template_json)
        .replace("__MATRIX_JSON__", matrix_json)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CAT-style point-to-point diagrams as standalone HTML/SVG (no Graphviz)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html"),
        help="Output HTML path.",
    )
    parser.add_argument(
        "--title",
        default="Studio Sidecar",
        help="Diagram title.",
    )
    parser.add_argument(
        "--layer",
        action="append",
        dest="layers",
        default=[],
        help="Filter to one or more Layer values (repeatable).",
    )
    parser.add_argument(
        "--status",
        action="append",
        dest="statuses",
        default=[],
        help="Filter to one or more Status values (repeatable, case-insensitive).",
    )
    parser.add_argument(
        "--show-patchbays",
        action="store_true",
        help="Include patchbay devices and patchbay-related cables in the diagram.",
    )
    parser.add_argument(
        "--show-power",
        action="store_true",
        help="Include power connections and power ports (hidden by default).",
    )
    parser.add_argument(
        "--svg-dir",
        type=Path,
        default=None,
        help="Optional folder to also export one standalone SVG file per layer.",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("projects/studio-sidecar/device-configurations/basis.json"),
        help="Device/port model JSON (used by default when present).",
    )
    parser.add_argument(
        "--connections-json",
        type=Path,
        default=Path("projects/studio-sidecar/patch-configurations/basis/patch-default.json"),
        help="Connection matrix JSON containing source/destination port links.",
    )
    parser.add_argument(
        "--routing-rules",
        type=Path,
        default=Path("json/routing_rules.json"),
        help="Routing/label rules JSON file.",
    )
    parser.add_argument(
        "--debug-routes-json",
        type=Path,
        default=Path("projects/studio-sidecar/outputs/debug/route-debug.json"),
        help="Optional JSON output with resolved route geometry/score per cable.",
    )
    parser.add_argument(
        "--matrix-output",
        type=Path,
        default=Path("routing_matrix.html"),
        help="Path to write the interactive routing matrix HTML.",
    )
    parser.add_argument(
        "--force-matrix-overwrite",
        action="store_true",
        help=(
            # Safety switch: prevent accidental clobbering of hand-tuned root UI unless explicitly requested.
            "Allow overwriting an existing root routing_matrix.html. "
            "By default, existing root routing_matrix.html is preserved."
        ),
    )
    parser.add_argument(
        "--matrix-model-url",
        default=None,
        help="Optional URL/path that routing_matrix.html should load for live model JSON.",
    )
    parser.add_argument(
        "--matrix-connections-url",
        default=None,
        help="Optional URL/path that routing_matrix.html should load for live connection JSON.",
    )
    parser.add_argument(
        "--no-matrix-live-link",
        action="store_true",
        help="Disable live JSON loading in routing_matrix.html (embedded snapshot only).",
    )
    return parser.parse_args()


def filter_patchbays_from_matrix_inputs(
    model_data: dict[str, object],
    matrix_payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    filtered_model = dict(model_data)
    model_devices = model_data.get("devices", [])
    if isinstance(model_devices, list):
        # Keep patchbay inventory available to the device and rack editors. The
        # matrix payload below remains free of patchbay routing rows by default.
        filtered_model["devices"] = [device for device in model_devices if isinstance(device, dict)]

    filtered_matrix = dict(matrix_payload)
    rows = matrix_payload.get("connections", [])
    if isinstance(rows, list):
        filtered_matrix["connections"] = [
            row
            for row in rows
            if isinstance(row, dict)
            and not is_patchbay_device(str(row.get("source_device", "")))
            and not is_patchbay_device(str(row.get("dest_device", "")))
        ]
    return filtered_model, filtered_matrix


def visible_device_names_from_model(model_data: dict[str, object]) -> set[str]:
    raw_devices = model_data.get("devices", [])
    if not isinstance(raw_devices, list):
        return set()
    visible: set[str] = set()
    for device in raw_devices:
        if not isinstance(device, dict):
            continue
        if not is_model_device_visible(device):
            continue
        name = str(device.get("name") or "").strip()
        if name:
            visible.add(name)
    return visible


def visible_port_endpoints_from_model(model_data: dict[str, object]) -> set[tuple[str, str]]:
    raw_devices = model_data.get("devices", [])
    if not isinstance(raw_devices, list):
        return set()
    endpoints: set[tuple[str, str]] = set()
    for device in raw_devices:
        if not isinstance(device, dict):
            continue
        if not is_model_device_visible(device):
            continue
        device_name = str(device.get("name") or "").strip()
        if not device_name:
            continue
        raw_ports = device.get("ports")
        if not isinstance(raw_ports, list):
            continue
        for port in raw_ports:
            if not isinstance(port, dict):
                continue
            port_name = str(port.get("name") or "").strip()
            if not port_name:
                continue
            port_hidden = parse_bool(port.get("hidden"), default=False)
            port_visible = parse_bool(port.get("visible"), default=True)
            if port_hidden or not port_visible:
                continue
            endpoint_device, endpoint_port = maybe_merge_monitor_pair(device_name, port_name)
            endpoints.add((endpoint_device, endpoint_port))
    return endpoints


def main() -> int:
    args = parse_args()
    generated_on = dt.date.today()
    selected_layers = set(args.layers) or None
    status_filter = {status.lower() for status in args.statuses} or None
    drawing_rules = load_routing_rules(args.routing_rules)
    route_debug_payload: dict[str, object] = {
        "generated_on": generated_on.isoformat(),
        "title": "",
        "rules_file": str(args.routing_rules),
        "rules": drawing_rules,
        "layers": {},
    }

    try:
        model_data = load_json_dict(args.model)
        matrix_payload = load_json_dict(args.connections_json)
    except Exception as exc:
        print(f"Error loading model/matrix JSON: {exc}", file=sys.stderr)
        return 1

    render_title = str(model_data.get("title") or args.title).strip() or args.title
    route_debug_payload["title"] = render_title
    device_type_overrides: dict[str, str] = {}
    visible_devices = visible_device_names_from_model(model_data)
    visible_ports = visible_port_endpoints_from_model(model_data)
    try:
        port_inventory, device_type_overrides, port_meta = build_port_inventory_from_model(
            model_data,
            include_power_ports=args.show_power,
        )
        connections = read_connections_from_model_matrix(
            model_data=model_data,
            matrix_path=args.connections_json,
            selected_layers=selected_layers,
            status_filter=status_filter,
            port_meta=port_meta,
        )
    except Exception as exc:
        print(f"Error building routing data: {exc}", file=sys.stderr)
        return 1

    connections = [
        connection
        for connection in connections
        if connection.source_device in visible_devices and connection.dest_device in visible_devices
    ]
    connections = [
        connection
        for connection in connections
        if (connection.source_device, connection.source_jack) in visible_ports
        and (connection.dest_device, connection.dest_jack) in visible_ports
    ]

    if not args.show_power:
        connections = [
            connection
            for connection in connections
            if resolve_connection_family_and_color(connection)[0] != "Power"
        ]

    layer_extra_devices: dict[str, set[str]] = defaultdict(set)
    layer_hidden_patch_connections: dict[str, list[Connection]] = defaultdict(list)
    if not args.show_patchbays:
        filtered: list[Connection] = []
        for connection in connections:
            src_is_patch = is_patchbay_device(connection.source_device)
            dst_is_patch = is_patchbay_device(connection.dest_device)
            if not src_is_patch and not dst_is_patch:
                filtered.append(connection)
                continue
            layer_hidden_patch_connections[connection.layer].append(connection)
            if src_is_patch and not dst_is_patch:
                layer_extra_devices[connection.layer].add(connection.dest_device)
            elif dst_is_patch and not src_is_patch:
                layer_extra_devices[connection.layer].add(connection.source_device)
        connections = filtered

    if not connections and not layer_extra_devices:
        print("Error: No connections or visible devices matched the selected filters.", file=sys.stderr)
        return 1

    grouped_connections: dict[str, list[Connection]] = defaultdict(list)
    for connection in connections:
        grouped_connections[connection.layer].append(connection)
    for layer, devices in layer_extra_devices.items():
        if devices and layer not in grouped_connections:
            grouped_connections[layer] = []

    # A hidden device can remove the last visible route from a layer. Still
    # rewrite that layer's SVG so the preview/open/download controls never
    # serve an older diagram left behind by a previous visibility state.
    if selected_layers is None and args.svg_dir:
        standard_layers = {
            str(config.get("layer") or "").strip()
            for config in DEFAULT_MATRIX_FAMILY_DEFINITIONS.values()
            if isinstance(config, dict)
        }
        if not args.show_power:
            standard_layers.discard("Power")
        for layer in standard_layers:
            if layer:
                grouped_connections.setdefault(layer, [])

    svgs: dict[str, str] = {}
    for layer in sorted(grouped_connections, key=natural_key):
        layer_route_debug: list[dict[str, object]] = []
        svgs[layer] = render_svg(
            layer=layer,
            connections=grouped_connections[layer],
            title=render_title,
            port_inventory=port_inventory,
            generated_on=generated_on,
            extra_devices=layer_extra_devices.get(layer),
            hidden_patch_connections=layer_hidden_patch_connections.get(layer),
            device_type_overrides=device_type_overrides,
            drawing_rules=drawing_rules,
            route_debug_records=layer_route_debug,
        )
        route_debug_payload["layers"][layer] = layer_route_debug

    html_text = build_html(
        render_title,
        grouped_connections,
        svgs,
        generated_on=generated_on,
        layer_extra_devices=layer_extra_devices,
    )
    args.output.write_text(html_text, encoding="utf-8")
    print(f"Wrote HTML: {args.output}")

    if args.svg_dir:
        args.svg_dir.mkdir(parents=True, exist_ok=True)
        for layer in sorted(svgs, key=natural_key):
            layer_file = args.svg_dir / f"{slugify(layer)}.svg"
            layer_file.write_text(svgs[layer], encoding="utf-8")
            print(f"Wrote SVG: {layer_file}")

        # Combine analog and digital signal paths in one audio-only overview.
        # The underlying layer names stay intact so each family keeps its own
        # wire colour and legend entry.
        current_family_definitions = matrix_family_definitions(model_data)
        audio_layers = {
            str(current_family_definitions.get(family, {}).get("layer") or "").strip()
            for family in ("AUDIO", "DIGI")
        }
        audio_layers.discard("")
        all_audio_connections = [
            connection
            for connection in connections
            if connection.layer in audio_layers
        ]
        all_audio_extra_devices: set[str] = set()
        for layer, devices in layer_extra_devices.items():
            if layer in audio_layers:
                all_audio_extra_devices |= set(devices)

        all_audio_hidden_patch_connections: list[Connection] = []
        if not args.show_patchbays:
            for hidden in layer_hidden_patch_connections.values():
                all_audio_hidden_patch_connections.extend(
                    connection
                    for connection in hidden
                    if connection.layer in audio_layers
                )

        all_audio_route_debug: list[dict[str, object]] = []
        all_audio_svg = render_svg(
            layer="All Audio",
            connections=all_audio_connections,
            title=render_title,
            port_inventory=port_inventory,
            generated_on=generated_on,
            extra_devices=all_audio_extra_devices or None,
            hidden_patch_connections=all_audio_hidden_patch_connections or None,
            overview_mode=True,
            device_type_overrides=device_type_overrides,
            drawing_rules=drawing_rules,
            route_debug_records=all_audio_route_debug,
        )
        route_debug_payload["layers"]["All Audio"] = all_audio_route_debug
        all_audio_file = args.svg_dir / "all-audio.svg"
        all_audio_file.write_text(all_audio_svg, encoding="utf-8")
        print(f"Wrote SVG: {all_audio_file}")

        # Also export one large signal-flow overview. Power is summarized by a
        # small input-side group badge on each powered device; dedicated power
        # routes and infrastructure remain in power.svg only.
        overview_connections = [
            connection
            for connection in connections
            if resolve_connection_family_and_color(connection)[0] != "Power"
        ]
        overview_power_groups = power_groups_by_device(connections)
        all_extra_devices: set[str] = set()
        for layer, devices in layer_extra_devices.items():
            if layer.strip().lower() != "power":
                all_extra_devices |= set(devices)

        all_hidden_patch_connections: list[Connection] = []
        if not args.show_patchbays:
            for hidden in layer_hidden_patch_connections.values():
                all_hidden_patch_connections.extend(
                    connection
                    for connection in hidden
                    if resolve_connection_family_and_color(connection)[0] != "Power"
                )

        all_route_debug: list[dict[str, object]] = []
        all_svg = render_svg(
            layer="All Connections",
            connections=overview_connections,
            title=render_title,
            port_inventory=port_inventory,
            generated_on=generated_on,
            extra_devices=all_extra_devices or None,
            hidden_patch_connections=all_hidden_patch_connections or None,
            overview_mode=True,
            device_type_overrides=device_type_overrides,
            drawing_rules=drawing_rules,
            route_debug_records=all_route_debug,
            overview_power_groups=overview_power_groups,
        )
        route_debug_payload["layers"]["All Connections"] = all_route_debug
        all_file = args.svg_dir / "all-connections.svg"
        all_file.write_text(all_svg, encoding="utf-8")
        print(f"Wrote SVG: {all_file}")

    if args.debug_routes_json:
        args.debug_routes_json.parent.mkdir(parents=True, exist_ok=True)
        args.debug_routes_json.write_text(
            json.dumps(route_debug_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote route debug JSON: {args.debug_routes_json}")

    if model_data is not None and matrix_payload is not None and args.matrix_output:
        matrix_model = model_data
        matrix_data = matrix_payload
        if not args.show_patchbays:
            matrix_model, matrix_data = filter_patchbays_from_matrix_inputs(model_data, matrix_payload)

        enable_live_json = not args.no_matrix_live_link
        live_model_url: str | None = None
        live_connections_url: str | None = None
        if enable_live_json:
            live_model_url = (
                str(args.matrix_model_url).strip()
                if args.matrix_model_url
                else relative_url_for_html(args.model, args.matrix_output)
            )
            live_connections_url = (
                str(args.matrix_connections_url).strip()
                if args.matrix_connections_url
                else relative_url_for_html(args.connections_json, args.matrix_output)
            )

        matrix_html = build_routing_matrix_html(
            title=render_title,
            model_data=matrix_model,
            matrix_payload=matrix_data,
            generated_on=generated_on,
            live_model_url=live_model_url,
            live_matrix_url=live_connections_url,
            enable_live_json=enable_live_json,
            show_patchbays=args.show_patchbays,
        )
        # Keep the main root UI stable by default; developers can still generate to other targets
        # or explicitly opt in to replacing root routing_matrix.html via --force-matrix-overwrite.
        root_matrix_output = Path("routing_matrix.html").resolve()
        requested_matrix_output = args.matrix_output.resolve()
        should_preserve_existing_root_matrix = (
            requested_matrix_output == root_matrix_output
            and args.matrix_output.exists()
            and not args.force_matrix_overwrite
        )
        if should_preserve_existing_root_matrix:
            print(
                "Skipped matrix UI overwrite for existing root routing_matrix.html "
                f"({args.matrix_output}). Use --force-matrix-overwrite to replace it."
            )
        else:
            args.matrix_output.write_text(matrix_html, encoding="utf-8")
            print(f"Wrote matrix UI: {args.matrix_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
