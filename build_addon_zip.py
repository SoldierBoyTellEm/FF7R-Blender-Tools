#!/usr/bin/env python3
"""Package addon/ into an installable Blender add-on zip under dist/.

Usage: python build_addon_zip.py

The zip contains a single top-level ``ff7r_rebirth_tools/`` directory, which is
what Blender's "Install from Disk" expects -- a zip with loose files at its root
would extract straight into scripts/addons/ instead of into its own folder.
"""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
SOURCE = REPO / "addon"
PACKAGE = "ff7r_rebirth_tools"
DIST = REPO / "dist"

# Build artifacts and editor cruft that must never reach an installed add-on.
EXCLUDED_DIRECTORIES = {"__pycache__", ".vs", ".vscode", ".git"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".swp", ".tmp"}
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db"}


def addon_version() -> str:
    """Read bl_info's version tuple without importing bpy."""
    tree = ast.parse((SOURCE / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == "bl_info" for t in node.targets):
            continue
        info = ast.literal_eval(node.value)
        return ".".join(str(part) for part in info.get("version", ()))
    raise SystemExit("Could not read bl_info['version'] from addon/__init__.py")


def included_files() -> list[Path]:
    files = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(SOURCE).parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name in EXCLUDED_NAMES:
            continue
        files.append(path)
    return files


def main() -> None:
    if not (SOURCE / "__init__.py").is_file():
        raise SystemExit(f"No add-on found at {SOURCE}")

    version = addon_version()
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{PACKAGE}-{version}.zip"

    files = included_files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, Path(PACKAGE) / path.relative_to(SOURCE))

    size_mb = target.stat().st_size / 1024 / 1024
    print(f"{target.relative_to(REPO)}  ({len(files)} files, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
