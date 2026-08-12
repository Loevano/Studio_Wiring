# Studio Wiring

Studio Wiring is a local, JSON-first application for describing studio equipment,
patching ports, and generating point-to-point HTML and SVG wiring diagrams.

## Quick start

On macOS, double-click `Studio_Wiring.command` for the desktop server controller,
or `start_routing_shell.command` to start the server and open the browser directly.

From a terminal:

```bash
python3 routing_matrix_server.py --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000/web/shell/index.html>.

The normal workflow is:

1. Select or create a project.
2. Select or create a device configuration.
3. Select or create a patch configuration.
4. Add devices and ports; optionally mark equipment as Rack-mounted and drag it into place in the Rack Editor.
5. Create crosspoint connections.
6. Save the configuration and inspect the generated visuals.

## Data model

- `project.json` describes a project and binds device configurations to patches.
- `device-configurations/*.json` describes devices and their ports.
  Devices may optionally include `rack_mountable`, `location`, `rack_units`, and
  `rack_position` for physical rack eligibility and layout.
- `patch-configurations/<device-config-stem>/*.json` stores connections.
- `json/routing_rules.json` controls global routing and labeling behavior.
- `outputs/` contains generated HTML, SVG, and route-debug artifacts.

See [DATA_FLOW.md](DATA_FLOW.md) for the technical data-flow contract and
[USER_MANUAL.md](USER_MANUAL.md) for day-to-day usage.

## Development checks

Run the repository check script from the project root:

```bash
python3 tools/check_project.py
```

The check suite uses dedicated fixtures and must not rewrite files in `projects/`.

## Repository map

- `routing_matrix_server.py`: local static server and persistence API.
- `generate_point_to_point.py`: wiring-diagram and matrix generator.
- `routing_matrix.html`: generated editor and visual-preview application.
- `prototypes/routing_matrix_prototype_compact.html`: canonical routing-matrix application.
- `web/`: shell, tab manifest, and shared styles.
- `defaults/`: clean project and reusable device templates.
- `projects/`: user-owned project inputs and generated outputs.
- `documentation/`: architecture and extended reference material.
- `tools/`: repeatable checks, smoke tests, and migrations.

## Source and generated-file policy

Device and patch JSON under `projects/` is user-owned source data. Treat it as
irreplaceable and never use it as a test fixture. Files under a project's
`outputs/` are generated from that source data. `routing_matrix.html` is also
generated; application changes must be made in its generator source and then
regenerated. Runtime backup snapshots under `backups/` are local recovery data,
not application source.

## More documentation

- [Project structure](documentation/PROJECT_STRUCTURE.md)
- [Tab architecture](documentation/TAB_ARCHITECTURE.md)
- [Architecture consolidation boundary](documentation/ARCHITECTURE_CONSOLIDATION.md)
- [Rack Editor and rack-layout fields](documentation/RACK_EDITOR.md)
- [Visual routing rules](documentation/VISUAL_RULES.md)
- [Extended user manual](documentation/EXTENDED_USER_MANUAL.md)
- [Filing overview](documentation/FILING_OVERVIEW.md)

When save paths, APIs, or generator inputs/outputs change, update
[DATA_FLOW.md](DATA_FLOW.md) in the same change.
