#!/usr/bin/env python3
"""Migrate project patch configs to per-device subfolders.

Layout target:
  projects/<project>/patch-configurations/<device-config-stem>/<patch>.json

Also updates project.json:
  - paths.default_patch
  - device_patch_map
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def to_posix(path: Path) -> str:
    return str(path).replace(os.sep, "/")


def sanitize_segment(raw: str, fallback: str = "device-config") -> str:
    token = str(raw or "").strip()
    cleaned = []
    for ch in token:
        if ch.isalnum() or ch in "._-":
            cleaned.append(ch)
        else:
            cleaned.append("-")
    out = "".join(cleaned)
    while "--" in out:
        out = out.replace("--", "-")
    out = out.strip("-")
    if not out:
        out = fallback
    return out


def canonical_rel(value: str, project_dir: Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(project_dir.resolve())
        except Exception:
            return ""
    return to_posix(path)


def unique_target_rel(
    desired_rel: str,
    occupied: set[str],
) -> str:
    candidate = desired_rel
    if candidate not in occupied:
        occupied.add(candidate)
        return candidate
    p = Path(candidate)
    stem = p.stem
    suffix = p.suffix or ".json"
    parent = p.parent
    idx = 1
    while True:
        next_name = f"{stem}-{idx:03d}{suffix}"
        next_rel = to_posix(parent / next_name)
        if next_rel not in occupied:
            occupied.add(next_rel)
            return next_rel
        idx += 1


def migrate_project(project_dir: Path, apply_changes: bool) -> dict[str, object]:
    project_file = project_dir / "project.json"
    if not project_file.exists():
        return {"project": project_dir.name, "updated": False, "reason": "missing project.json"}

    payload = read_json(project_file)
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        paths = {}
        payload["paths"] = paths

    device_dir = project_dir / "device-configurations"
    patch_dir = project_dir / "patch-configurations"
    device_files = sorted([p for p in device_dir.glob("*.json") if p.is_file()], key=lambda p: p.name.lower())
    patch_files = sorted([p for p in patch_dir.rglob("*.json") if p.is_file()], key=lambda p: to_posix(p.relative_to(project_dir)).lower())
    if not device_files or not patch_files:
        return {"project": project_dir.name, "updated": False, "reason": "no device or patch files"}

    device_rels = [to_posix(p.relative_to(project_dir)) for p in device_files]
    device_rel_set = set(device_rels)
    first_device_rel = device_rels[0]
    configured_default_device = canonical_rel(str(paths.get("device_model") or ""), project_dir)
    default_device_rel = configured_default_device if configured_default_device in device_rel_set else first_device_rel

    existing_map_raw = payload.get("device_patch_map")
    existing_map_raw = existing_map_raw if isinstance(existing_map_raw, dict) else {}
    patch_rel_set = {to_posix(p.relative_to(project_dir)) for p in patch_files}

    assigned_device_by_patch: dict[str, str] = {}
    for raw_device, raw_patch_list in existing_map_raw.items():
        device_rel = canonical_rel(raw_device, project_dir)
        if device_rel not in device_rel_set:
            continue
        patch_list = raw_patch_list if isinstance(raw_patch_list, list) else []
        for raw_patch in patch_list:
            patch_rel = canonical_rel(raw_patch, project_dir)
            if patch_rel not in patch_rel_set:
                continue
            assigned_device_by_patch.setdefault(patch_rel, device_rel)

    default_patch_rel = canonical_rel(str(paths.get("default_patch") or ""), project_dir)
    occupied_rel_paths = set(patch_rel_set)
    moved_rel: dict[str, str] = {}
    new_device_patch_map: dict[str, list[str]] = {}
    moves: list[tuple[str, str]] = []

    for patch_path in patch_files:
        patch_rel = to_posix(patch_path.relative_to(project_dir))
        assigned_device = assigned_device_by_patch.get(patch_rel)
        if not assigned_device:
            if patch_rel == default_patch_rel and default_device_rel:
                assigned_device = default_device_rel
            else:
                assigned_device = default_device_rel or first_device_rel
        device_stem = Path(assigned_device).stem
        target_dir_rel = to_posix(Path("patch-configurations") / sanitize_segment(device_stem))
        desired_rel = to_posix(Path(target_dir_rel) / patch_path.name)
        # Allow no-op retention of an already-migrated path.
        occupied_rel_paths.discard(patch_rel)
        final_rel = unique_target_rel(desired_rel, occupied_rel_paths)
        moved_rel[patch_rel] = final_rel
        new_device_patch_map.setdefault(assigned_device, []).append(final_rel)
        if final_rel != patch_rel:
            moves.append((patch_rel, final_rel))

    # De-duplicate and sort map lists per device for stable metadata.
    normalized_map: dict[str, list[str]] = {}
    for device_rel, patch_list in new_device_patch_map.items():
        seen = set()
        ordered = []
        for rel in patch_list:
            if rel in seen:
                continue
            seen.add(rel)
            ordered.append(rel)
        normalized_map[device_rel] = sorted(ordered, key=lambda s: s.lower())

    if default_patch_rel in moved_rel:
        paths["default_patch"] = moved_rel[default_patch_rel]
    else:
        fallback_patches = normalized_map.get(default_device_rel, [])
        if fallback_patches:
            paths["default_patch"] = fallback_patches[0]
    if default_device_rel:
        paths["device_model"] = default_device_rel

    payload["device_patch_map"] = normalized_map

    if apply_changes:
        for from_rel, to_rel in moves:
            src = project_dir / from_rel
            dst = project_dir / to_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() == dst.resolve():
                continue
            src.rename(dst)
        write_json(project_file, payload)

    return {
        "project": project_dir.name,
        "updated": True,
        "moves": moves,
        "default_device": paths.get("device_model", ""),
        "default_patch": paths.get("default_patch", ""),
        "map_entries": len(normalized_map),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate patch configs to per-device subfolders.")
    parser.add_argument("--projects-root", default="projects", help="Path to projects root (default: projects)")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files")
    args = parser.parse_args()

    projects_root = Path(args.projects_root).resolve()
    if not projects_root.exists():
        raise SystemExit(f"Projects root not found: {projects_root}")

    projects = [
        p for p in sorted(projects_root.iterdir(), key=lambda q: q.name.lower())
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
    ]
    if not projects:
        print("No projects found.")
        return 0

    any_updates = False
    for project_dir in projects:
        result = migrate_project(project_dir, apply_changes=not args.dry_run)
        if not result.get("updated"):
            print(f"[skip] {result['project']}: {result.get('reason', 'no changes')}")
            continue
        any_updates = True
        moves = result.get("moves", [])
        mode = "plan" if args.dry_run else "done"
        print(f"[{mode}] {result['project']}: {len(moves)} move(s), map entries={result.get('map_entries')}")
        for src_rel, dst_rel in moves:
            print(f"  - {src_rel} -> {dst_rel}")
        print(f"  default_device={result.get('default_device')} | default_patch={result.get('default_patch')}")

    if not any_updates:
        print("No migratable projects found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
