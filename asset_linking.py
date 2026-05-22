"""Shared Blender asset-library lookup and collection-linking helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Iterable, Mapping

import bpy


ASSET_LIBRARY_NONE = "__NONE__"
ASSET_LIBRARY_ALL = "__ALL__"
ASSET_LIBRARY_MANUAL = "__MANUAL__"
ASSET_LIBRARY_MANUAL_PREFIX = "__PATH__:"

_ASSET_INDEX_CACHE: dict[str, dict[str, list[str]]] = {}
_OBJECT_ASSET_INDEX_CACHE: dict[str, dict[str, list[str]]] = {}
_COMBINED_INDEX_CACHE: set[str] = set()
_BLEND_PATHS_CACHE: dict[str, list[Path]] = {}
_BLENDER_4_ID_NAME_LIMIT = 63
_DUPLICATE_ID_SUFFIX_RE = re.compile(r"\.\d{3}$")


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


def uses_limited_id_names() -> bool:
    """Return True when Blender truncates ID names to the legacy 4.x limit."""
    return tuple(getattr(bpy.app, "version", (0, 0, 0))) < (5, 0, 0)


def limited_id_name(name: str) -> str:
    """Return Blender 4.x's visible ID name for *name*."""
    if not name or not uses_limited_id_names():
        return name

    encoded = name.encode("utf-8")
    if len(encoded) <= _BLENDER_4_ID_NAME_LIMIT:
        return name
    return encoded[:_BLENDER_4_ID_NAME_LIMIT].decode("utf-8", errors="ignore")


def id_name_candidates(name: str) -> tuple[str, ...]:
    """Return acceptable library/loaded ID names for the active Blender version."""
    if not name:
        return ()

    limited = limited_id_name(name)
    if limited != name:
        return name, limited
    return (name,)


def id_name_matches(requested_name: str, actual_name: str) -> bool:
    """
    Return True when *actual_name* can represent *requested_name*.

    Blender 5.x keeps full ID names. Blender 4.x truncates long names when it
    reads newer .blend files, so matching must also accept the truncated form.
    """
    if not requested_name or not actual_name:
        return False

    for candidate in id_name_candidates(requested_name):
        if actual_name == candidate or actual_name.startswith(f"{candidate}."):
            return True

    if uses_limited_id_names():
        suffix_match = _DUPLICATE_ID_SUFFIX_RE.search(actual_name)
        if suffix_match is not None and len(actual_name.encode("utf-8")) >= _BLENDER_4_ID_NAME_LIMIT:
            base_name = actual_name[:suffix_match.start()]
            return bool(base_name) and any(
                candidate.startswith(base_name) for candidate in id_name_candidates(requested_name)
            )

    return False


def resolve_library_id_name(available_names: Iterable[str], requested_name: str) -> str | None:
    """Resolve *requested_name* to the actual name exposed by a library load."""
    names = list(available_names)
    if not requested_name:
        return None

    name_set = set(names)
    for candidate in id_name_candidates(requested_name):
        if candidate in name_set:
            return candidate

    if uses_limited_id_names():
        matches = sorted(name for name in names if id_name_matches(requested_name, name))
        if matches:
            return matches[0]

    return None


def _resolve_library_id_names(available_names: Iterable[str], requested_names: Iterable[str]) -> list[str]:
    names = list(available_names)
    resolved_names: list[str] = []
    seen: set[str] = set()
    for requested_name in requested_names:
        resolved_name = resolve_library_id_name(names, requested_name)
        if resolved_name is None or resolved_name in seen:
            continue
        seen.add(resolved_name)
        resolved_names.append(resolved_name)
    return resolved_names


def index_paths_for_id_name(index: Mapping[str, list[str]], requested_name: str) -> list[str]:
    """Return indexed blend paths for *requested_name*, including 4.x truncation aliases."""
    paths: list[str] = []
    seen: set[str] = set()

    def add_paths(indexed_paths: Iterable[str] | None) -> None:
        if not indexed_paths:
            return
        for path in indexed_paths:
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)

    for candidate in id_name_candidates(requested_name):
        add_paths(index.get(candidate))

    if not paths and uses_limited_id_names():
        for indexed_name, indexed_paths in index.items():
            if id_name_matches(requested_name, indexed_name):
                add_paths(indexed_paths)

    return paths


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
    return id_name_matches(asset_name, collection.name)


def _object_matches_asset_name(obj: bpy.types.Object, asset_name: str) -> bool:
    if obj is None:
        return False
    return id_name_matches(asset_name, obj.name)


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

    blend_paths = index_paths_for_id_name(get_object_asset_index(selection), asset_name)
    if not blend_paths:
        print(f"[FF7R JSON Import]   Object asset not found in selected asset libraries: {asset_name!r}")
        return None

    for blend_path in blend_paths:
        if not os.path.exists(blend_path):
            continue
        try:
            with bpy.data.libraries.load(blend_path, link=True, assets_only=True) as (data_from, data_to):
                object_name = resolve_library_id_name(data_from.objects, asset_name)
                if object_name is None:
                    continue
                data_to.objects = [object_name]
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                object_name = resolve_library_id_name(data_from.objects, asset_name)
                if object_name is None:
                    continue
                data_to.objects = [object_name]
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

        collection_paths = index_paths_for_id_name(collection_index, asset_name)
        if collection_paths:
            collections_by_path.setdefault(collection_paths[0], []).append(asset_name)
            continue

        if not include_objects:
            continue

        if find_loaded_linked_source_object(asset_name) is not None:
            continue

        object_paths = index_paths_for_id_name(object_index, asset_name)
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
                data_to.collections = _resolve_library_id_names(data_from.collections, collection_names)
                data_to.objects = _resolve_library_id_names(data_from.objects, object_names)
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                data_to.collections = _resolve_library_id_names(data_from.collections, collection_names)
                data_to.objects = _resolve_library_id_names(data_from.objects, object_names)
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

    blend_paths = index_paths_for_id_name(get_collection_asset_index(selection), asset_name)
    if not blend_paths:
        if report_missing:
            print(f"[FF7R JSON Import]   Collection not found in selected asset libraries: {asset_name!r}")
        return None

    for blend_path in blend_paths:
        if not os.path.exists(blend_path):
            continue
        try:
            with bpy.data.libraries.load(blend_path, link=True, assets_only=True) as (data_from, data_to):
                collection_name = resolve_library_id_name(data_from.collections, asset_name)
                if collection_name is None:
                    continue
                data_to.collections = [collection_name]
        except TypeError:
            with bpy.data.libraries.load(blend_path, link=True) as (data_from, data_to):
                collection_name = resolve_library_id_name(data_from.collections, asset_name)
                if collection_name is None:
                    continue
                data_to.collections = [collection_name]
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
