from __future__ import annotations

import datetime as dt
import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from generate_point_to_point import (
    Connection,
    DeviceBox,
    GroupBlock,
    align_paired_rows,
    apply_layer_column_overrides,
    assign_bidirectional_port_sides,
    assign_columns,
    build_boxes,
    compute_route_top_base,
    containing_group_bottom,
    main,
    move_device_box_down,
    place_device_below_intervening_columns,
    render_svg,
    split_ports_for_device,
    stacked_top_route_lane,
)


ROOT = Path(__file__).resolve().parents[1]


def connection(
    cable_id: str,
    source: str,
    source_port: str,
    destination: str,
    destination_port: str,
    *,
    layer: str = "Power",
    connection_type: str = "Power",
    notes: str = "",
) -> Connection:
    return Connection(
        cable_id=cable_id,
        source_device=source,
        source_jack=source_port,
        dest_device=destination,
        dest_jack=destination_port,
        layer=layer,
        signal_type="Mains Power" if layer == "Power" else "Data",
        status="Connected",
        cable_type=connection_type,
        connection_type=connection_type,
        notes=notes,
    )


ROUTING_RULES = {
    "routing": {
        "fifo_forward_turns": True,
        "backward_out_to_in_wrap": "below",
        "video_early_turn": True,
        "video_vertical_rows_threshold": 6,
        "wire_clearance_px": 12,
        "power_wire_clearance_px": 18,
        "power_lane_spacing_px": 18,
        "power_column_gap_px": 420,
        "left_route_gutter_px": 108,
    }
}


class SvgRoutingTests(unittest.TestCase):
    def test_top_route_lane_clears_group_caption_and_border(self) -> None:
        box = DeviceBox(
            name="Dock",
            device_type="Computer / Control",
            x=100.0,
            y=128.0,
            width=280.0,
            height=100.0,
            in_ports=[],
            out_ports=[],
            in_port_y={},
            out_port_y={},
            port_roles={},
            port_connected={},
        )
        group = GroupBlock(
            device_type="Computer / Control",
            x=90.0,
            y=120.0,
            width=300.0,
            height=116.0,
        )

        route_y = compute_route_top_base({"Dock": box}, [group], margin_y=34.0)

        caption_top = group.y - 13.0
        self.assertLessEqual(route_y, caption_top - 8.0)
        self.assertEqual(stacked_top_route_lane(route_y, 0, 12.0, 34.0), route_y)
        self.assertLess(stacked_top_route_lane(route_y, 1, 12.0, 34.0), route_y)

    def test_bottom_route_lane_respects_containing_group(self) -> None:
        box = DeviceBox(
            name="Audient ASP 880",
            device_type="Preamps / Channel Strip",
            x=160.0,
            y=226.0,
            width=270.0,
            height=86.0,
            in_ports=[],
            out_ports=[],
            in_port_y={},
            out_port_y={},
            port_roles={},
            port_connected={},
        )
        group = GroupBlock(
            "Preamps / Channel Strip",
            150.0,
            218.0,
            290.0,
            240.0,
        )

        self.assertEqual(containing_group_bottom(box, [group]), 458.0)

    def test_ssl_line_inputs_and_outputs_share_numbered_rows(self) -> None:
        roles = {
            "MADI In Optical": "in",
            "MADI Out Optical": "out",
            **{f"Line In {channel}": "in" for channel in range(1, 25)},
            **{f"Line Out {channel}": "out" for channel in range(13, 25)},
        }

        inputs, outputs = split_ports_for_device("SSL AX MADI", roles)
        aligned_inputs, aligned_outputs = align_paired_rows(inputs, outputs)

        for channel in range(13, 25):
            self.assertEqual(
                aligned_inputs.index(f"Line In {channel}"),
                aligned_outputs.index(f"Line Out {channel}"),
            )
        self.assertEqual(
            aligned_inputs.index("MADI In Optical"),
            aligned_outputs.index("MADI Out Optical"),
        )

    def test_rme_digital_and_analog_counterparts_share_rows(self) -> None:
        roles = {
            "MADI In Optical": "in",
            "MADI Out Optical": "out",
            "ADAT 1 In (Optical)": "in",
            "ADAT 1 Out (Optical, S/PDIF)": "out",
            "USB Host (Computer)": "io",
            **{f"Line In {channel}": "in" for channel in range(1, 5)},
            "Line Out 1 (XLR)": "out",
            "Line Out 2 (XLR)": "out",
            "Line Out 3": "out",
            "Line Out 4": "out",
        }

        inputs, outputs = split_ports_for_device("RME UFX III", roles)
        aligned_inputs, aligned_outputs = align_paired_rows(inputs, outputs)

        expected_pairs = [
            ("Line In 1", "Line Out 1 (XLR)"),
            ("Line In 2", "Line Out 2 (XLR)"),
            ("Line In 3", "Line Out 3"),
            ("Line In 4", "Line Out 4"),
            ("ADAT 1 In (Optical)", "ADAT 1 Out (Optical, S/PDIF)"),
            ("MADI In Optical", "MADI Out Optical"),
            ("USB Host (Computer)", "USB Host (Computer)"),
        ]
        for input_port, output_port in expected_pairs:
            self.assertEqual(
                aligned_inputs.index(input_port),
                aligned_outputs.index(output_port),
            )

    def test_bidirectional_ports_render_once_on_the_side_facing_their_peer(self) -> None:
        rows = [
            connection(
                "NETWORK-001",
                "Netgear Unmanaged Switch",
                "P 1",
                "Avid S1 #1",
                "Ethernet",
                layer="Network",
                connection_type="CAT6",
            )
        ]
        roles = {
            "Avid S1 #1": {"Ethernet": "io"},
            "Netgear Unmanaged Switch": {"P 1": "io"},
        }

        bidirectional_ids = assign_bidirectional_port_sides(
            [["Avid S1 #1"], ["Netgear Unmanaged Switch"]],
            rows,
            roles,
        )

        self.assertEqual(bidirectional_ids, {"NETWORK-001"})
        self.assertEqual(
            split_ports_for_device("Avid S1 #1", roles["Avid S1 #1"]),
            ([], ["Ethernet"]),
        )
        self.assertEqual(
            split_ports_for_device(
                "Netgear Unmanaged Switch",
                roles["Netgear Unmanaged Switch"],
            ),
            (["P 1"], []),
        )

    def test_reversed_bidirectional_link_still_uses_short_forward_route(self) -> None:
        rows = [
            connection(
                "NETWORK-001",
                "Netgear Unmanaged Switch",
                "P 1",
                "Avid S1 #1",
                "Ethernet",
                layer="Network",
                connection_type="CAT6",
            )
        ]
        inventory = {
            "Avid S1 #1": {"Ethernet": "io"},
            "Netgear Unmanaged Switch": {"P 1": "io"},
        }
        debug: list[dict[str, object]] = []

        svg = render_svg(
            "Network",
            rows,
            "Facing IO fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        self.assertEqual(svg.count(">Ethernet</text>"), 1)
        self.assertEqual(svg.count(">P 1</text>"), 1)
        self.assertEqual(debug[0]["source"]["device"], "Avid S1 #1")
        self.assertEqual(debug[0]["destination"]["device"], "Netgear Unmanaged Switch")
        self.assertEqual(debug[0]["route_mode"], "forward")
        self.assertLess(debug[0]["source"]["x"], debug[0]["destination"]["x"])

    def test_reciprocal_madi_rows_collapse_to_one_short_visual_link(self) -> None:
        rows = [
            connection(
                "DIGI-001",
                "RME UFX III",
                "MADI Out Optical",
                "SSL AX MADI",
                "MADI In Optical",
                layer="Digital Audio",
                connection_type="MADI-OPT",
            ),
            connection(
                "DIGI-002",
                "SSL AX MADI",
                "MADI Out Optical",
                "RME UFX III",
                "MADI In Optical",
                layer="Digital Audio",
                connection_type="MADI-OPT",
            ),
        ]
        inventory = {
            "RME UFX III": {
                "MADI In Optical": "in",
                "MADI Out Optical": "out",
            },
            "SSL AX MADI": {
                "MADI In Optical": "in",
                "MADI Out Optical": "out",
            },
        }
        debug: list[dict[str, object]] = []

        svg = render_svg(
            "Digital Audio",
            rows,
            "MADI fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        self.assertEqual(len(debug), 1)
        self.assertEqual(debug[0]["cable_id"], "DIGI-001↔002")
        self.assertEqual(debug[0]["route_mode"], "forward")
        self.assertIn("Cables: 1", svg)
        self.assertIn('marker-start="url(#arrow)" marker-end="url(#arrow)"', svg)

    def test_black_lion_output_banks_stay_grouped_by_type(self) -> None:
        roles = {
            "AC In": "in",
            "Analog Outlet 1": "out",
            "Analog Outlet 2": "out",
            "Digital Outlet 1": "out",
            "Digital Outlet 2": "out",
            "High Current Outlet 1": "out",
            "High Current Outlet 2": "out",
            "Front Unswitched Outlet 1": "out",
            "Front Unswitched Outlet 2": "out",
        }

        _inputs, outputs = split_ports_for_device(
            "Black Lion Audio PG-1 Type F MKII",
            roles,
        )

        self.assertEqual(
            outputs,
            [
                "Analog Outlet 1",
                "Analog Outlet 2",
                "Digital Outlet 1",
                "Digital Outlet 2",
                "High Current Outlet 1",
                "High Current Outlet 2",
                "Front Unswitched Outlet 1",
                "Front Unswitched Outlet 2",
            ],
        )

    def test_all_audio_uses_the_analog_signal_flow_columns(self) -> None:
        initial_columns = [
            ["Monitor", "Console"],
            ["Converter", "Talkback Mic"],
            ["Preamp", "Amplifier"],
        ]
        device_types = {
            "Talkback Mic": "Microphone / DI",
            "Preamp": "Preamps / Channel Strip",
            "Console": "Console / Mixer",
            "Converter": "Interface / Converter",
            "Amplifier": "Amplifier / Monitor Control",
            "Monitor": "Speaker / Monitor",
        }

        analog_columns = apply_layer_column_overrides(
            "Audio Analog",
            initial_columns,
            device_types,
        )
        all_audio_columns = apply_layer_column_overrides(
            "All Audio",
            initial_columns,
            device_types,
        )

        self.assertEqual(all_audio_columns, analog_columns)
        self.assertEqual(
            all_audio_columns,
            [
                ["Talkback Mic"],
                ["Console", "Preamp"],
                ["Converter"],
                ["Amplifier"],
                ["Monitor"],
            ],
        )

    def test_digital_audio_uses_three_forward_signal_stages(self) -> None:
        columns = [
            ["Audient ASP 880", "RME UFX III"],
            ["SSL AX MADI", "TC Electronic Clarity M Stereo"],
        ]

        result = apply_layer_column_overrides("Digital Audio", columns)
        column_by_device = {
            device: column_index
            for column_index, column in enumerate(result)
            for device in column
        }

        self.assertEqual(column_by_device["Audient ASP 880"], 0)
        self.assertEqual(column_by_device["RME UFX III"], 1)
        self.assertEqual(column_by_device["SSL AX MADI"], 2)
        self.assertEqual(column_by_device["TC Electronic Clarity M Stereo"], 2)

    def test_all_audio_places_clarity_below_atc_in_monitoring(self) -> None:
        columns = [
            ["SSL AX MADI", "TC Electronic Clarity M Stereo"],
            ["ATC SCM 11", "Auratone 5C"],
        ]
        device_types = {
            "SSL AX MADI": "Interface / Converter",
            "ATC SCM 11": "Speaker / Monitor",
            "TC Electronic Clarity M Stereo": "Speaker / Monitor",
            "Auratone 5C": "Speaker / Monitor",
        }

        result = apply_layer_column_overrides("All Audio", columns, device_types)

        monitoring_column = next(
            column for column in result if "TC Electronic Clarity M Stereo" in column
        )
        self.assertEqual(
            monitoring_column,
            ["ATC SCM 11", "TC Electronic Clarity M Stereo", "Auratone 5C"],
        )

    def test_clarity_clears_the_intervening_monitor_stack(self) -> None:
        def box(name: str, device_type: str, x: float, y: float, height: float) -> DeviceBox:
            return DeviceBox(
                name=name,
                device_type=device_type,
                x=x,
                y=y,
                width=270.0,
                height=height,
                in_ports=["In"],
                out_ports=[],
                in_port_y={"In": y + 47.0},
                out_port_y={},
                port_roles={"In": "in"},
                port_connected={"In": True},
            )

        boxes = {
            "RME UFX III": box("RME UFX III", "Interface", 700.0, 226.0, 200.0),
            "IMG STAGELINE PPA-100/SW": box(
                "IMG STAGELINE PPA-100/SW",
                "Amplifier / Monitor Control",
                1300.0,
                458.0,
                174.0,
            ),
            "TC Electronic Clarity M Stereo": box(
                "TC Electronic Clarity M Stereo",
                "Speaker / Monitor",
                1900.0,
                458.0,
                64.0,
            ),
        }
        groups = [
            GroupBlock("Speaker / Monitor", 1890.0, 218.0, 290.0, 312.0),
        ]

        place_device_below_intervening_columns(
            boxes,
            groups,
            {
                "RME UFX III": 1,
                "IMG STAGELINE PPA-100/SW": 2,
                "TC Electronic Clarity M Stereo": 3,
            },
            "RME UFX III",
            "TC Electronic Clarity M Stereo",
        )

        clarity = boxes["TC Electronic Clarity M Stereo"]
        self.assertEqual(clarity.y, 662.0)
        self.assertEqual(clarity.in_port_y["In"], 709.0)
        self.assertEqual(groups[0].y + groups[0].height, 734.0)

    def test_preserved_flow_order_keeps_preamps_above_mixer(self) -> None:
        roles = {
            "Audient ASP 880": {"Line Out": "out"},
            "Allen & Heath GS3000": {"Tape Out": "out"},
        }
        connected = {
            device: {port: True for port in ports}
            for device, ports in roles.items()
        }

        boxes, _width, _groups = build_boxes(
            ["Audient ASP 880", "Allen & Heath GS3000"],
            roles,
            connected,
            {
                "Audient ASP 880": "Preamps / Channel Strip",
                "Allen & Heath GS3000": "Console / Mixer",
            },
            {"Audient ASP 880": 0, "Allen & Heath GS3000": 0},
            start_x=100.0,
            start_y=200.0,
            preserve_column_order=True,
        )

        self.assertLess(boxes["Audient ASP 880"].y, boxes["Allen & Heath GS3000"].y)

    def test_all_connections_places_power_amps_above_headphone_amp(self) -> None:
        columns = [
            [
                "IMG STAGELINE PPA-100/SW",
                "Behringer A800 #2",
                "Behringer A800 #1",
            ]
        ]

        result = apply_layer_column_overrides("All Connections", columns)

        self.assertEqual(
            result,
            [[
                "Behringer A800 #1",
                "Behringer A800 #2",
                "IMG STAGELINE PPA-100/SW",
            ]],
        )

    def test_all_connections_places_switch_below_peripherals(self) -> None:
        columns = [[
            "Netgear Unmanaged Switch",
            "Streamdeck #2",
            "TV Screen",
            "Streamdeck #1",
        ]]

        result = apply_layer_column_overrides("All Connections", columns)

        self.assertEqual(
            result,
            [[
                "TV Screen",
                "Streamdeck #1",
                "Streamdeck #2",
                "Netgear Unmanaged Switch",
            ]],
        )

    def test_computer_data_usb_destinations_follow_dock_port_order(self) -> None:
        columns = [
            ["Mac mini"],
            ["Thunderbolt Dock", "TV Screen"],
            ["Streamdeck #2", "RME UFX III", "Streamdeck #1"],
        ]

        result = apply_layer_column_overrides("Computer/Data", columns)

        self.assertEqual(
            result[2],
            ["RME UFX III", "Streamdeck #1", "Streamdeck #2"],
        )

    def test_all_connections_keeps_ssl_in_its_signal_stage(self) -> None:
        columns = [
            ["RME UFX III"],
            ["SSL AX MADI", "TC Electronic Clarity M Stereo"],
            ["Behringer A800 #1"],
        ]

        result = apply_layer_column_overrides("All Connections", columns)
        column_by_device = {
            device: column_index
            for column_index, column in enumerate(result)
            for device in column
        }

        self.assertLess(
            column_by_device["RME UFX III"],
            column_by_device["SSL AX MADI"],
        )
        self.assertEqual(
            column_by_device["SSL AX MADI"],
            column_by_device["TC Electronic Clarity M Stereo"],
        )
        self.assertLess(
            column_by_device["TC Electronic Clarity M Stereo"],
            column_by_device["Behringer A800 #1"],
        )

    def test_generated_all_connections_bundles_share_route_grammar(self) -> None:
        debug_path = ROOT / "projects" / "studio-sidecar" / "outputs" / "debug" / "route-debug.json"
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
        routes = payload["layers"]["All Connections"]
        bundles: dict[tuple[str, str], list[dict[str, object]]] = {}
        for route in routes:
            key = (route["source"]["device"], route["destination"]["device"])
            bundles.setdefault(key, []).append(route)

        def route_signature(route: dict[str, object]) -> tuple[str, ...]:
            points = route["points"]
            return tuple(
                "H" if points[index - 1]["y"] == points[index]["y"] else "V"
                for index in range(1, len(points))
            )

        inconsistent = {
            f"{source} -> {destination}": {
                route_signature(route) for route in bundle
            }
            for (source, destination), bundle in bundles.items()
            if len(bundle) > 1
            and len({route_signature(route) for route in bundle}) > 1
        }
        self.assertEqual(inconsistent, {})

    def test_generated_preamp_bundle_clears_computer_control_group(self) -> None:
        debug_path = ROOT / "projects" / "studio-sidecar" / "outputs" / "debug" / "route-debug.json"
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
        route = next(
            item
            for item in payload["layers"]["All Connections"]
            if item["cable_id"] == "AUDIO-029-032"
        )

        self.assertEqual(route["score"]["unrelated_group_crossings"], 0)
        self.assertLess(
            min(point["y"] for point in route["points"]),
            min(route["source"]["y"], route["destination"]["y"]),
        )

    def test_descending_stereo_pair_uses_crossing_free_turn_order(self) -> None:
        debug_path = ROOT / "projects" / "studio-sidecar" / "outputs" / "debug" / "route-debug.json"
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
        routes = {
            item["cable_id"]: item
            for item in payload["layers"]["All Connections"]
            if item["cable_id"] in {"AUDIO-035", "AUDIO-036"}
        }

        def first_vertical_x(route: dict[str, object]) -> float:
            points = route["points"]
            for index in range(1, len(points)):
                if points[index - 1]["x"] == points[index]["x"]:
                    return float(points[index]["x"])
            self.fail(f"No vertical turn in {route['cable_id']}")

        self.assertGreater(
            first_vertical_x(routes["AUDIO-035"]),
            first_vertical_x(routes["AUDIO-036"]),
        )

    def test_all_audio_collapses_sequential_runs_into_multichannel_links(self) -> None:
        rows = [
            connection(
                f"AUDIO-{channel:03d}",
                "Console",
                f"Line Out {channel}",
                "Converter",
                f"Line In {channel}",
                layer="Audio Analog",
                connection_type="Analog",
            )
            for channel in range(1, 5)
        ]
        inventory = {
            "Console": {f"Line Out {channel}": "out" for channel in range(1, 5)},
            "Converter": {f"Line In {channel}": "in" for channel in range(1, 5)},
        }

        svg = render_svg(
            "All Audio",
            rows,
            "Multichannel audio fixture",
            inventory,
            dt.date(2026, 8, 22),
            overview_mode=True,
            drawing_rules=ROUTING_RULES,
        )

        self.assertIn("Cables: 1", svg)
        self.assertIn("AUDIO-001-004", svg)
        self.assertIn("Line Out 1+Line Out 2+Line Out 3+Line Out 4", svg)
        self.assertIn('stroke-width="2.3"', svg)
        self.assertIn('markerUnits="userSpaceOnUse"', svg)
        destination_branches = [
            line
            for line in svg.splitlines()
            if 'class="multichannel-destination-branch"' in line
        ]
        self.assertEqual(len(destination_branches), 4)
        self.assertTrue(
            all('marker-end="url(#arrow)"' in line for line in destination_branches)
        )
        source_collector = re.search(
            r'class="multichannel-source-collector" x1="([\d.]+)"',
            svg,
        )
        destination_collector = re.search(
            r'class="multichannel-destination-collector" x1="([\d.]+)"',
            svg,
        )
        trunk = re.search(
            r'<path d="([^"]+)"[^>]*><title>AUDIO-001-004:',
            svg,
        )
        self.assertIsNotNone(source_collector)
        self.assertIsNotNone(destination_collector)
        self.assertIsNotNone(trunk)
        trunk_path = trunk.group(1)
        self.assertTrue(trunk_path.startswith(f"M {float(source_collector.group(1)):.1f} "))
        self.assertTrue(trunk_path.endswith(f" {float(destination_collector.group(1)):.1f}"))

    def test_headphone_stereo_bundle_shows_both_mono_source_branches(self) -> None:
        rows = [
            connection(
                "AUDIO-001",
                "SSL AX MADI",
                "Line Out 1",
                "IMG STAGELINE PPA-100/SW",
                "HA In 1",
                layer="Audio Analog",
                connection_type="MONO",
            ),
            connection(
                "AUDIO-002",
                "SSL AX MADI",
                "Line Out 2",
                "IMG STAGELINE PPA-100/SW",
                "HA In 1",
                layer="Audio Analog",
                connection_type="MONO",
            ),
        ]
        inventory = {
            "SSL AX MADI": {"Line Out 1": "out", "Line Out 2": "out"},
            "IMG STAGELINE PPA-100/SW": {"HA In 1": "in"},
        }

        svg = render_svg(
            "Audio Analog",
            rows,
            "Stereo headphone fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
        )

        self.assertIn("Line Out 1+Line Out 2", svg)
        self.assertEqual(svg.count('class="stereo-source-branch"'), 4)
        self.assertEqual(svg.count('class="stereo-source-collector"'), 2)
        self.assertEqual(svg.count('class="stereo-source-merge"'), 1)

    def test_power_visual_gives_each_circuit_a_distinct_propagated_colour(self) -> None:
        rows = [
            connection(
                "POWER-001",
                "Meterkast",
                "Groep 1",
                "Distributor",
                "AC In",
            ),
            connection(
                "POWER-002",
                "Distributor",
                "Outlet 1",
                "Studio Load",
                "AC In",
            ),
            connection(
                "POWER-003",
                "Meterkast",
                "Groep 2",
                "Lights",
                "AC In",
            ),
        ]
        inventory = {
            "Meterkast": {"Groep 1": "out", "Groep 2": "out"},
            "Distributor": {"AC In": "in", "Outlet 1": "out"},
            "Studio Load": {"AC In": "in"},
            "Lights": {"AC In": "in"},
        }

        power_svg = render_svg(
            "Power",
            rows,
            "Power groups fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
        )
        overview_svg = render_svg(
            "All Connections",
            rows,
            "Power groups fixture",
            inventory,
            dt.date(2026, 8, 22),
            overview_mode=True,
            drawing_rules=ROUTING_RULES,
        )

        self.assertIn("Power Groups", power_svg)
        self.assertIn("Group 1 (2)", power_svg)
        self.assertIn("Group 2 (1)", power_svg)
        self.assertIn('stroke="#1d4ed8"', power_svg)
        self.assertIn('stroke="#b45309"', power_svg)
        self.assertNotIn("Power Groups", overview_svg)
        self.assertNotIn('stroke="#1d4ed8"', overview_svg)

    def test_all_connections_uses_input_badge_instead_of_power_routes(self) -> None:
        power_rows = [
            connection(
                "POWER-001",
                "Meterkast",
                "Groep 1",
                "Mixer PSU",
                "AC In",
            ),
            connection(
                "POWER-002",
                "Mixer PSU",
                "DC Out",
                "Mixer",
                "DC In",
            ),
        ]
        signal_rows = [
            connection(
                "AUDIO-001",
                "Mixer",
                "Line Out",
                "Recorder",
                "Line In",
                layer="Audio Analog",
                connection_type="Analog",
            )
        ]
        inventory = {
            "Mixer": {"DC In": "in", "Line Out": "out"},
            "Recorder": {"Line In": "in"},
        }

        overview_svg = render_svg(
            "All Connections",
            signal_rows,
            "Power badge fixture",
            inventory,
            dt.date(2026, 8, 22),
            overview_mode=True,
            drawing_rules=ROUTING_RULES,
            overview_power_groups={"Mixer": "Group 1"},
        )

        self.assertIn('data-power-group="Group 1"', overview_svg)
        self.assertIn("Power circuit: Group 1", overview_svg)
        self.assertNotIn("POWER-001", overview_svg)
        self.assertNotIn("Mixer PSU", overview_svg)
        self.assertNotIn("DC In", overview_svg)

    def test_power_visual_colours_black_lion_banks_and_their_downstream_routes(self) -> None:
        rows = [
            connection("POWER-001", "Meterkast", "Groep 1", "Black Lion", "AC In"),
            connection("POWER-002", "Black Lion", "Analog Outlet 1", "Switcher", "AC In"),
            connection("POWER-003", "Switcher", "Outlet 1", "Low Load", "AC In"),
            connection("POWER-004", "Black Lion", "Digital Outlet 1", "Digital Load", "AC In"),
            connection("POWER-005", "Black Lion", "High Current Outlet 1", "Amplifier", "AC In"),
        ]
        inventory = {
            "Meterkast": {"Groep 1": "out"},
            "Black Lion": {
                "AC In": "in",
                "Analog Outlet 1": "out",
                "Digital Outlet 1": "out",
                "High Current Outlet 1": "out",
            },
            "Switcher": {"AC In": "in", "Outlet 1": "out"},
            "Low Load": {"AC In": "in"},
            "Digital Load": {"AC In": "in"},
            "Amplifier": {"AC In": "in"},
        }

        power_svg = render_svg(
            "Power",
            rows,
            "Power banks fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
        )

        self.assertIn("Group 1 · Digital (1)", power_svg)
        self.assertIn("Group 1 · Hi (1)", power_svg)
        self.assertIn("Group 1 · Lo (2)", power_svg)
        self.assertLess(power_svg.index("Group 1 · Digital"), power_svg.index("Group 1 · Hi"))
        self.assertLess(power_svg.index("Group 1 · Hi"), power_svg.index("Group 1 · Lo"))
        self.assertIn('stroke="#0e7490"', power_svg)
        self.assertIn('stroke="#7e22ce"', power_svg)
        self.assertIn('stroke="#be123c"', power_svg)

    def test_full_export_rewrites_layers_with_no_visible_routes(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "generator"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            svg_dir = temp / "svgs"
            power_svg = svg_dir / "power.svg"
            svg_dir.mkdir()
            power_svg.write_text("stale power drawing", encoding="utf-8")
            argv = [
                "generate_point_to_point.py",
                "--model",
                str(fixture / "model.json"),
                "--connections-json",
                str(fixture / "connections.json"),
                "--routing-rules",
                str(ROOT / "json" / "routing_rules.json"),
                "--output",
                str(temp / "preview.html"),
                "--svg-dir",
                str(svg_dir),
                "--debug-routes-json",
                str(temp / "routes.json"),
                "--matrix-output",
                str(temp / "matrix.html"),
                "--show-power",
            ]

            with patch("sys.argv", argv):
                self.assertEqual(main(), 0)

            expected_files = {
                "audio-analog.svg",
                "computer-data.svg",
                "digital-audio.svg",
                "all-audio.svg",
                "network.svg",
                "power.svg",
                "all-connections.svg",
            }
            self.assertEqual({path.name for path in svg_dir.glob("*.svg")}, expected_files)
            current_power = power_svg.read_text(encoding="utf-8")
            self.assertNotIn("stale power drawing", current_power)
            self.assertIn("Devices: 0 | Cables: 0", current_power)

            all_audio = (svg_dir / "all-audio.svg").read_text(encoding="utf-8")
            self.assertIn("All Audio", all_audio)
            self.assertIn("Cables: 1", all_audio)
            self.assertIn("Analog Audio", all_audio)
            self.assertIn("AUDIO-001", all_audio)
            self.assertNotIn("NETWORK-001", all_audio)

    def test_power_layout_follows_topology_instead_of_audio_device_type_hints(self) -> None:
        devices = ["Wall", "Distributor", "Preamp", "Monitor"]
        rows = [
            connection("POWER-001", "Wall", "Out", "Distributor", "AC In"),
            connection("POWER-002", "Distributor", "Out", "Preamp", "AC In"),
            connection("POWER-003", "Preamp", "DC Out", "Monitor", "DC In"),
        ]
        sources = Counter(row.source_device for row in rows)
        destinations = Counter(row.dest_device for row in rows)
        device_types = {
            "Wall": "Power Source",
            "Distributor": "Power Distribution",
            "Preamp": "Preamps / Channel Strip",
            "Monitor": "Powered Load",
        }

        columns, stages = assign_columns(
            devices,
            sources,
            destinations,
            rows,
            device_types,
            layer="Power",
        )

        column_by_device = {
            device: column_index
            for column_index, column in enumerate(columns)
            for device in column
        }
        self.assertEqual(stages, {"Wall": 0, "Distributor": 1, "Preamp": 2, "Monitor": 3})
        self.assertLess(column_by_device["Distributor"], column_by_device["Preamp"])
        self.assertLess(column_by_device["Preamp"], column_by_device["Monitor"])

    def test_power_fanout_uses_separate_ordered_turn_lanes(self) -> None:
        rows = [
            connection(
                f"POWER-{index:03}",
                "Distributor",
                "Outlet 1",
                f"Load {index}",
                "AC In",
            )
            for index in range(1, 6)
        ]
        inventory = {
            "Distributor": {"Outlet 1": "out"},
            **{f"Load {index}": {"AC In": "in"} for index in range(1, 6)},
        }
        debug: list[dict[str, object]] = []

        render_svg(
            "Power",
            rows,
            "Routing fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        # The first destination is level with the source. Every subsequent
        # route has a vertical turn at points[2], and those turns must fan out.
        turn_x = [float(route["points"][2]["x"]) for route in debug[1:]]
        self.assertEqual(turn_x, sorted(turn_x))
        self.assertEqual(len(turn_x), len(set(turn_x)))
        self.assertTrue(
            all(right - left >= 18 for left, right in zip(turn_x, turn_x[1:])),
            turn_x,
        )

    def test_type_hints_do_not_collapse_converter_and_amp_stages_into_bottom_wraps(self) -> None:
        rows = [
            connection(
                "AUDIO-001",
                "Console",
                "Line Out",
                "Converter",
                "Line In",
                layer="Audio Analog",
                connection_type="Analog",
            ),
            *[
                connection(
                    f"AUDIO-{index:03}",
                    "Converter",
                    f"Line Out {index}",
                    "IMG STAGELINE PPA-100/SW",
                    f"HA In {index}",
                    layer="Audio Analog",
                    connection_type="Analog",
                )
                for index in range(2, 8)
            ],
            connection(
                "AUDIO-008",
                "IMG STAGELINE PPA-100/SW",
                "Speaker Out",
                "Monitor",
                "Speaker In",
                layer="Audio Analog",
                connection_type="Analog",
            ),
        ]
        inventory = {
            "Console": {"Line Out": "out"},
            "Converter": {
                "Line In": "in",
                **{f"Line Out {index}": "out" for index in range(2, 8)},
            },
            "IMG STAGELINE PPA-100/SW": {
                **{f"HA In {index}": "in" for index in range(2, 8)},
                "Speaker Out": "out",
            },
            "Monitor": {"Speaker In": "in"},
        }
        debug: list[dict[str, object]] = []

        render_svg(
            "Audio Analog",
            rows,
            "Same-column regression fixture",
            inventory,
            dt.date(2026, 8, 22),
            device_type_overrides={
                "Console": "Console / Mixer",
                "Converter": "Interface / Converter",
                "IMG STAGELINE PPA-100/SW": "Amplifier / Monitor Control",
                "Monitor": "Speaker / Monitor",
            },
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        fanout = [route for route in debug if route["source"]["device"] == "Converter"]
        self.assertEqual(len(fanout), 6)
        self.assertTrue(all(route["route_mode"] == "forward" for route in fanout))
        self.assertTrue(
            all(
                route["destination"]["column"] == route["source"]["column"] + 1
                for route in fanout
            )
        )
        self.assertTrue(
            all(route["score"]["outside_band_distance"] == 0 for route in fanout)
        )

    def test_route_debug_keeps_computer_protocols_distinct(self) -> None:
        rows = [
            connection(
                "COMP-001",
                "Computer",
                "HDMI Out",
                "Display",
                "HDMI In",
                layer="Computer/Data",
                connection_type="HDMI",
            ),
            connection(
                "COMP-002",
                "Computer",
                "Thunderbolt Out",
                "Dock",
                "Thunderbolt In",
                layer="Computer/Data",
                connection_type="TB4",
            ),
        ]
        inventory = {
            "Computer": {"HDMI Out": "out", "Thunderbolt Out": "out"},
            "Display": {"HDMI In": "in"},
            "Dock": {"Thunderbolt In": "in"},
        }
        debug: list[dict[str, object]] = []

        render_svg(
            "Computer/Data",
            rows,
            "Protocol fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        self.assertEqual({route["protocol"] for route in debug}, {"HDMI", "TB4"})
        self.assertTrue(
            all(route["score"]["different_protocol_overlap_count"] == 0 for route in debug)
        )

    def test_long_hdmi_route_stays_compact_in_the_endpoint_band(self) -> None:
        rows = [
            connection(
                "COMP-001",
                "Mac mini",
                "HDMI Out",
                "TV Screen",
                "HDMI In",
                layer="Computer/Data",
                connection_type="HDMI",
            ),
            connection(
                "COMP-002",
                "Mac mini",
                "Thunderbolt Port",
                "Thunderbolt Dock",
                "Host/Upstream Port",
                layer="Computer/Data",
                connection_type="TB4",
            ),
            connection(
                "COMP-003",
                "Thunderbolt Dock",
                "USB Port 1",
                "Streamdeck #1",
                "USB-C",
                layer="Computer/Data",
                connection_type="USB",
            ),
        ]
        inventory = {
            "Mac mini": {"HDMI Out": "out", "Thunderbolt Port": "io"},
            "Thunderbolt Dock": {"Host/Upstream Port": "io", "USB Port 1": "io"},
            "TV Screen": {"HDMI In": "in"},
            "Streamdeck #1": {"USB-C": "io"},
        }
        debug: list[dict[str, object]] = []

        render_svg(
            "Computer/Data",
            rows,
            "Compact HDMI fixture",
            inventory,
            dt.date(2026, 8, 22),
            drawing_rules=ROUTING_RULES,
            route_debug_records=debug,
        )

        hdmi = next(route for route in debug if route["protocol"] == "HDMI")
        endpoint_ys = [hdmi["source"]["y"], hdmi["destination"]["y"]]
        route_ys = [point["y"] for point in hdmi["points"]]
        self.assertGreaterEqual(min(route_ys), min(endpoint_ys))
        self.assertLessEqual(max(route_ys), max(endpoint_ys))
        self.assertEqual(hdmi["score"]["outside_band_distance"], 0)
        self.assertEqual(hdmi["destination"]["column"], 1)
        self.assertEqual(hdmi["score"]["bend_count"], 0)


if __name__ == "__main__":
    unittest.main()
