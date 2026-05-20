"""Shared Blender asset-library lookup and collection-linking helpers."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

import bpy


ASSET_LIBRARY_NONE = "__NONE__"
ASSET_LIBRARY_ALL = "__ALL__"
ASSET_LIBRARY_MANUAL = "__MANUAL__"
ASSET_LIBRARY_MANUAL_PREFIX = "__PATH__:"

_ASSET_INDEX_CACHE: dict[str, dict[str, list[str]]] = {}
_OBJECT_ASSET_INDEX_CACHE: dict[str, dict[str, list[str]]] = {}
_COMBINED_INDEX_CACHE: set[str] = set()
_BLEND_PATHS_CACHE: dict[str, list[Path]] = {}


@dataclass(frozen=True)
class LinkedAsset:
    """Resolved linked asset datablock."""
    kind: str
    datablock: bpy.types.Collection | bpy.types.Object


def asset_library_items(_self=None, _context=None):
    """EnumProperty callback listing prop-library source options."""
    items = [
        (ASSET_LIBRARY_ALL, "All Libraries", "Search every configured Blender asset library"),
        (
            ASSET_LIBRARY_MANUAL,
            "Custom Folder",
            "Search a folder you choose without adding it as a Blender asset library",
        ),
        (ASSET_LIBRARY_NONE, "None", "Do not search for linked prop assets"),
    ]
    prefs = getattr(bpy.context, "preferences", None)
    filepaths = getattr(prefs, "filepaths", None)
    libraries = getattr(filepaths, "asset_libraries", None)
    if libraries is None:
        return items

    for lib in libraries:
        name = getattr(lib, "name", "") or getattr(lib, "path", "")
        path = getattr(lib, "path", "")
        if not name:
            continue
        items.append((name, name, bpy.path.abspath(path) if path else ""))
    return items


def manual_asset_library_selection(path: str) -> str:
    """Encode a manual asset root as an asset-library selection string."""
    if not path:
        return ASSET_LIBRARY_MANUAL
    return f"{ASSET_LIBRARY_MANUAL_PREFIX}{bpy.path.abspath(path)}"


def _manual_asset_library_root(selection: str) -> Path | None:
    """Return the manual asset root encoded in *selection*, if any."""
    if not isinstance(selection, str):
        return None
    if selection.startswith(ASSET_LIBRARY_MANUAL_PREFIX):
        path = selection[len(ASSET_LIBRARY_MANUAL_PREFIX):]
        return Path(bpy.path.abspath(path)) if path else None
    return None


def selected_asset_libraries(selection: str):
    """Return asset-library preference entries matching *selection*."""
    if selection in {ASSET_LIBRARY_NONE, ASSET_LIBRARY_MANUAL}:
        return []
    if _manual_asset_library_root(selection) is not None:
        return []

    prefs = getattr(bpy.context, "preferences", None)
    filepaths = getattr(prefs, "filepaths", None)
    libraries = list(getattr(filepaths, "asset_libraries", []) or [])
    if selection in ("", ASSET_LIBRARY_ALL):
        return libraries

    return [
        lib for lib in libraries
        if (getattr(lib, "name", "") or getattr(lib, "path", "")) == selection
    ]


def _cache_key(selection: str) -> str:
    return selection or ASSET_LIBRARY_ALL


def clear_asset_caches() -> None:
    """Clear shared asset lookup caches."""
    _ASSET_INDEX_CACHE.clear()
    _OBJECT_ASSET_INDEX_CACHE.clear()
    _COMBINED_INDEX_CACHE.clear()
    _BLEND_PATHS_CACHE.clear()


def iter_asset_library_blend_paths(selection: str = ASSET_LIBRARY_ALL) -> list[Path]:
    """Collect .blend files from the selected prop-library roots."""
    key = _cache_key(selection)
    cached = _BLEND_PATHS_CACHE.get(key)
    if cached is not None:
        return cached

    blend_paths: list[Path] = []
    seen: set[Path] = set()

    manual_root = _manual_asset_library_root(selection)
    roots: list[Path] = []
    if manual_root is not None:
        roots.append(manual_root)
    else:
        for lib in selected_asset_libraries(selection):
            lib_path = getattr(lib, "path", "")
            if not lib_path:
                continue
            roots.append(Path(bpy.path.abspath(lib_path)))

    for root in roots:
        if not root.is_dir():
            print(f"[FF7R JSON Import]   Skipping missing prop library folder: {root}")
            continue
        for blend_path in root.rglob("*.blend"):
            if blend_path not in seen:
                seen.add(blend_path)
                blend_paths.append(blend_path)

    _BLEND_PATHS_CACHE[key] = sorted(blend_paths)
    return _BLEND_PATHS_CACHE[key]


def build_asset_indexes(selection: str = ASSET_LIBRARY_ALL) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build collection and object indexes while opening each .blend file once."""
    collection_index: dict[str, list[str]] = {}
    object_index: dict[str, list[str]] = {}
    for blend_path in iter_asset_library_blend_paths(selection):
        try:
            with bpy.data.libraries.load(str(blend_path), link=True, assets_only=True) as (data_from, data_to):
                data_to.collections = []
                data_to.objects = []
                for col_name in data_from.collections:
                    collection_index.setdefault(col_name, []).append(str(blend_path))
                for obj_name in data_from.objects:
                    object_index.setdefault(obj_name, []).append(str(blend_path))
        except TypeError:
            with bpy.data.libraries.load(str(blend_path), link=True) as (data_from, data_to):
                data_to.collections = []
                data_to.objects = []
                for col_name in data_from.collections:
                    collection_index.setdefault(col_name, []).append(str(blend_path))
                for obj_name in data_from.objects:
                    object_index.setdefault(obj_name, []).append(str(blend_path))
        except Exception as exc:
            print(f"[FF7R JSON Import]   Failed to index {blend_path}: {exc}")
    return collection_index, object_index


def ensure_asset_indexes(selection: str = ASSET_LIBRARY_ALL) -> None:
    """Populate both collection and object indexes for *selection* once."""
    key = _cache_key(selection)
    if key in _COMBINED_INDEX_CACHE:
        return

    print(f"[FF7R JSON Import] Building asset indexes for asset library selection: {key}")
    collection_index, object_index = build_asset_indexes(selection)
    _ASSET_INDEX_CACHE[key] = collection_index
    _OBJECT_ASSET_INDEX_CACHE[key] = object_index
    _COMBINED_INDEX_CACHE.add(key)
    print(
        f"[FF7R JSON Import] Indexed {len(collection_index)} collection names "
        f"and {len(object_index)} object names."
    )


def build_collection_asset_index(selection: str = ASSET_LIBRARY_ALL) -> dict[str, list[str]]:
    """Build mapping: collection name -> .blend files where it exists."""
    collection_index, _object_index = build_asset_indexes(selection)
    return collection_index


def get_collection_asset_index(selection: str = ASSET_LIBRARY_ALL) -> dict[str, list[str]]:
    """Get or lazily build the collection asset index for a selection."""
    key = _cache_key(selection)
    if key not in _ASSET_INDEX_CACHE:
        ensure_asset_indexes(selection)
    return _ASSET_INDEX_CACHE[key]


def build_object_asset_index(selection: str = ASSET_LIBRARY_ALL) -> dict[str, list[str]]:
    """Build mapping: object name -> .blend files where it exists."""
    _collection_index, object_index = build_asset_indexes(selection)
    return object_index


def get_object_asset_index(selection: str = ASSET_LIBRARY_ALL) -> dict[str, list[str]]:
    """Get or lazily build the object asset index for a selection."""
    key = _cache_key(selection)
    if key not in _OBJECT_ASSET_INDEX_CACHE:
        ensure_asset_indexes(selection)
    return _OBJECT_ASSET_INDEX_CACHE[key]


def _collection_matches_asset_name(collection: bpy.types.Collection, asset_name: str) -> bool:
    if collection is None:
        return False
    return collection.name == asset_name or collection.name.startswith(f"{asset_name}.")


def _object_matches_asset_name(obj: bpy.types.Object, asset_name: str) -> bool:
    if obj is None:
        return False
    return obj.name == asset_name or obj.name.startswith(f"{asset_name}.")


def find_loaded_linked_source_collection(asset_name: str) -> bpy.types.Collection | None:
    """Find an already-loaded linked source collection for *asset_name*."""
    for col in bpy.data.collections:
        if not _collection_matches_asset_name(col, asset_name):
            continue
        if col.library is None:
            continue
        if getattr(col, "override_library", None) is not None:
            continue
        return col
    return None


def find_loaded_linked_source_object(asset_name: str) -> bpy.types.Object | None:
    """Find an already-loaded linked source object for *asset_name*."""
    for obj in bpy.data.objects:
        if not _object_matches_asset_name(obj, asset_name):
            continue
        if obj.library is None:
            continue
        if getattr(obj, "override_library", None) is not None:
            continue
        return obj
    return None


def find_or_load_object(
    asset_name: str,
    selection: str = ASSET_LIBRARY_ALL,
) -> bpy.types.Object | None:
    """Find or link an object asset from selected asset libraries."""
    if not asset_name:
        return None

    obj = find_loaded_linked_source_object(asset_name)
    if obj is not None:
        return obj

    if selection == ASSET_LIBRARY_NONE:
        return None

    blend_paths = get_object_asset_index(selection).get(asset_name)
    if not blend_paths:
        print(f"[FF7R JSON Import]   Object asset not found in selected asset libraries: {asset_name!r}")
        return None

    for blend_path in blend_paths:
        if not os.path.exists(blend_path):
            continue
        try:
            with bpy.data.libraries.load(blend_path, link=True, assets_only=True) as (data_from, data_to):
                if asset_name not in data_from.objects:
                    continue
                data_to.objects = [asset_name]
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                if asset_name not in data_from.objects:
                    continue
                data_to.objects = [asset_name]
        except Exception as exc:
            print(f"[FF7R JSON Import]   Failed to link object {asset_name!r} from {blend_path}: {exc}")
            continue

        obj = find_loaded_linked_source_object(asset_name)
        if obj is not None:
            print(f"[FF7R JSON Import]   Linked object asset fallback for {asset_name!r}.")
            return obj

    return None


def preload_assets(
    asset_names: set[str],
    selection: str = ASSET_LIBRARY_ALL,
    include_objects: bool = True,
) -> None:
    """
    Best-effort batch link for asset names from selected asset libraries.

    Collections are preferred, matching find_or_load_asset(). Object assets are
    only considered for names that do not have a collection index entry.
    Individual find_or_load_* calls should still be used afterward as a fallback
    for missing names or failed library loads.
    """
    if selection == ASSET_LIBRARY_NONE:
        return

    names = {name for name in asset_names if name}
    if not names:
        return

    collection_index = get_collection_asset_index(selection)
    object_index = get_object_asset_index(selection) if include_objects else {}

    collections_by_path: dict[str, list[str]] = {}
    objects_by_path: dict[str, list[str]] = {}

    for asset_name in sorted(names):
        if find_loaded_linked_source_collection(asset_name) is not None:
            continue

        collection_paths = collection_index.get(asset_name)
        if collection_paths:
            collections_by_path.setdefault(collection_paths[0], []).append(asset_name)
            continue

        if not include_objects:
            continue

        if find_loaded_linked_source_object(asset_name) is not None:
            continue

        object_paths = object_index.get(asset_name)
        if object_paths:
            objects_by_path.setdefault(object_paths[0], []).append(asset_name)

    blend_paths = sorted(set(collections_by_path) | set(objects_by_path))
    for blend_path in blend_paths:
        if not os.path.exists(blend_path):
            continue

        collection_names = collections_by_path.get(blend_path, [])
        object_names = objects_by_path.get(blend_path, [])
        try:
            with bpy.data.libraries.load(blend_path, link=True, assets_only=True) as (data_from, data_to):
                data_to.collections = [name for name in collection_names if name in data_from.collections]
                data_to.objects = [name for name in object_names if name in data_from.objects]
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                data_to.collections = [name for name in collection_names if name in data_from.collections]
                data_to.objects = [name for name in object_names if name in data_from.objects]
        except Exception as exc:
            print(f"[FF7R JSON Import]   Failed to batch link assets from {blend_path}: {exc}")


def find_or_load_collection(
    asset_name: str,
    selection: str = ASSET_LIBRARY_ALL,
    report_missing: bool = True,
) -> bpy.types.Collection | None:
    """Find an existing linked source collection or link it from selected asset libraries."""
    if not asset_name:
        return None

    col = find_loaded_linked_source_collection(asset_name)
    if col is not None:
        return col

    if selection == ASSET_LIBRARY_NONE:
        return None

    blend_paths = get_collection_asset_index(selection).get(asset_name)
    if not blend_paths:
        if report_missing:
            print(f"[FF7R JSON Import]   Collection not found in selected asset libraries: {asset_name!r}")
        return None

    for blend_path in blend_paths:
        if not os.path.exists(blend_path):
            continue
        try:
            with bpy.data.libraries.load(blend_path, link=True, assets_only=True) as (data_from, data_to):
                if asset_name not in data_from.collections:
                    continue
                data_to.collections = [asset_name]
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                if asset_name not in data_from.collections:
                    continue
                data_to.collections = [asset_name]
        except Exception as exc:
            print(f"[FF7R JSON Import]   Failed to link {asset_name!r} from {blend_path}: {exc}")
            continue

        col = find_loaded_linked_source_collection(asset_name)
        if col is not None:
            return col

    return None


def find_or_load_asset(asset_name: str, selection: str = ASSET_LIBRARY_ALL) -> LinkedAsset | None:
    """Resolve an asset name as a collection first, then a lone linked object."""
    col = find_or_load_collection(asset_name, selection, report_missing=False)
    if col is not None:
        return LinkedAsset(kind="COLLECTION", datablock=col)

    obj = find_or_load_object(asset_name, selection)
    if obj is not None:
        return LinkedAsset(kind="OBJECT", datablock=obj)

    return None
