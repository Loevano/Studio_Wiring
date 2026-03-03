# Casual User Manual (Simple)

Last updated: 2026-03-01

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

1. In top bar, open `Project` and choose `Create New Project...`
2. In top bar, open `Device Config` and choose `Create New Device Config...`
3. In top bar, open `Patch Config` and choose `Create New Patch Config...`

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

Use `Save Device Config As` if you want a copy/version.

## 6. Build Your Patch Config

Go to `Routing Matrix` tab (Prototype Matrix):

- click a `+` cell to connect
- click again to disconnect
- use `Patch Mode` for Single / Stereo / Multi / Paint
- use `Allow Double Patching` only if needed

Save with:

- `Save Patch` (overwrite current patch config)
- `Save Patch As` (new patch config file)

## 7. Auto Save Configs

If `Auto Save Configs` is ON, edits are saved automatically.

- Device edits save to the selected device config
- Patch edits save to the selected patch config

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
