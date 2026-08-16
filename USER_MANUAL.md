# Casual User Manual (Simple)

Last updated: 2026-08-12

This guide is for normal daily use. No coding knowledge needed.

## 1. What You Need

- This app runs with `Python 3`.
- Check if Python is installed:

python3 --version

If that shows a version (for example `Python 3.12.x`), you are good.

If not, install Python:

- macOS:
  - easiest: install from `https://www.python.org/downloads/`
  - or with Homebrew: `brew install python`
- Windows:
  - easiest: install from `https://www.python.org/downloads/`
  - or with winget: `winget install Python.Python.3`
- Linux (Ubuntu/Debian):
  - `sudo apt update && sudo apt install -y python3`

## 2. Start The App

Open Terminal and run:

cd ../Studio_Wiring
python3 routing_matrix_server.py --host 127.0.0.1 --port 8000

This sets up the connection on port 8000. If port already in use change it here and later in the browser.

Then open in browser:

http://127.0.0.1:8000/web/shell/index.html

## 3. First-Time Setup (Important)

You always work in this order:

1. `Project`
2. `Device Config`
3. `Patch Config`

### Step-by-step

1. In the top bar, choose `New Project` and confirm the displayed destination.
2. Choose `New Device Config` and enter a name.
3. Choose `New Patch Config` and enter a name.

That creates your working files.

## 4. How The 3 Levels Work

- `Project`: container/folder for your setup
- `Device Config`: devices + ports (what exists physically)
- `Patch Config`: connections between ports (how it is routed now)

Patch configs are tied to the selected project and selected device config.

## 5. Build Your Device Config

Go to `Devices & Ports` tab:

- add devices
- add/edit/remove ports
- set device type and port details
- click `Save Device Config` when ready

Pending fields for the selected device and its visible port list are applied automatically when you select another device, switch the Inputs/Outputs view, leave the Devices & Ports tab, or move to another browser window. Invalid values keep the editor open and show an error instead of being partially applied.

Use `Save Device Config As` if you want a copy/version.

## 6. Build Your Patch Config

Go to the `Wiring Matrix` tab:

- click a `+` cell to connect
- click again to disconnect
- use `Patch Mode` for Single / Stereo / Multi / Paint
- use `Allow Double Patching` only if needed

Save with:

- `Save Patch` (overwrite current patch config)
- `Save Patch As` (new patch config file)

The matrix shows `Saved`, `Saving`, or `Unsaved`. Use the Undo/Redo buttons or the usual Cmd/Ctrl-Z shortcuts. If another edit changed the same file on disk, the app reports a conflict instead of silently overwriting it.

## 6A. Arrange Rack Equipment

Open the `Rack Editor` tab to describe where equipment is physically installed.

- Existing devices are not rack mountable unless explicitly marked.
- In `Devices & Ports`, check `Rack mountable` for equipment that can physically be installed in a rack. Unmarked equipment such as speakers and S1 control surfaces is not shown in the Rack Editor.
- `Desk` and `Rack` describe the current location. A rack-mountable Desk device appears in the unplaced list and moves to `Rack` when dropped into a rack.
- Set its height from 1–16 U/HE.
- Drag the device from the unplaced list or its current rack position onto a rack unit. Green means valid; red means the move would overlap or exceed U16.
- For keyboard use, choose Rack 1–4 and the lowest occupied U, then apply the placement.
- Use `Remove from Rack` to leave a Rack device unplaced.

The editor shows U16 at the top and U1 at the bottom. It rejects placements that extend beyond U16 or overlap another device. Rack changes are part of the device configuration, so save the device config afterward. See [the Rack Editor reference](documentation/RACK_EDITOR.md) for the exact JSON fields.

## 6B. Route Audio Inside and Between Devices

The Wiring Matrix records physical cables and patch points. Use the separate `Routing Matrix` tab, immediately after Wiring Matrix, for logical audio paths inside interfaces, consoles, and converters. It only shows devices that expose routing endpoints.

Choose the source row and destination column, select a span (including 8 channels), then add, remove, or toggle the route. A source may feed more than one destination, but each destination can have only one active route.

For example, an eight-channel path can be expressed as:

1. `Audient ADAT 1-8 OUT` → `SSL ADAT 1-8 IN`
2. `SSL ADAT 1-8 IN` → `SSL MADI 25-32 OUT`
3. `SSL MADI 25-32 OUT` → `UFX MADI 25-32 IN`

All three logical signal links can be entered in Routing Matrix. If the studio also needs to document the physical cables for the first and third hop, record those separately in Wiring Matrix. Routing Matrix saves are stored separately from patch configs. They do not alter physical cables or regenerate wiring diagrams. Use Save or Auto Save as appropriate; a stale save reports a conflict rather than replacing a newer route configuration.

## 7. Auto Save Configs

If `Auto Save` is ON in the shell bar, edits are saved automatically. This is one global browser preference: it stays enabled when you switch between Wiring Matrix, Routing Matrix, Devices & Ports, Rack Editor, Visibility, and Visuals, and it is restored after a reload.

- Device edits save to the selected device config
- Patch edits save to the selected patch config
- Logical route edits save to the selected routing config
- Switching tabs or leaving the application window flushes pending edits immediately instead of waiting for the normal debounce.
- If that flush fails or times out, the tab switch is cancelled so unsaved work is not silently discarded.
- With Auto Save off, navigation within the same application preserves edits in the current browser session but does not write them to disk.
- Auto Save requires `routing_matrix_server.py`; the static HTML/file view cannot write project JSON to disk.

## 8. Visibility + Visuals

- `Visibility` tab: hide/show devices and reorder them
- `Visuals` tab: view generated SVG previews and open them

## 9. If Something Looks Wrong

1. Hard refresh the page (`Cmd+Shift+R` on Mac, `Ctrl+F5` on Windows)
2. Check that the top bar shows the correct `Project`, `Device Config`, and `Patch Config`
3. Make sure the server is still running in Terminal
4. If needed, click `Reload JSON`

## 10. Recommended Workflow

- Keep one main device config per studio layout
- Keep multiple patch configs for scenarios (tracking, mixing, live, etc.)
- Save often before switching configs
