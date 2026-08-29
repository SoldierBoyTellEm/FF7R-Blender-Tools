"""Shared timeline and action asset-library helpers for cutscene imports."""

from __future__ import annotations

from pathlib import Path

import bpy

from . import asset_linking


ARMATURE_ACTION_FPS: float = 30.0

_ASSET_ACTION_PATH_CACHE: dict[tuple[str, str], Path | None] = {}
_ASSET_ACTION_NAMES_CACHE: dict[Path, set[str]] = {}


def clear_action_caches() -> None:
    _ASSET_ACTION_PATH_CACHE.clear()
    _ASSET_ACTION_NAMES_CACHE.clear()


def scene_fps(scene) -> float:
    """Return the scene's effective frame rate, respecting fps_base."""
    fps = float(scene.render.fps)
    fps_base = float(scene.render.fps_base) if scene.render.fps_base else 1.0
    return fps / fps_base


def clear_scene_cameras(scene) -> int:
    """Remove all camera objects from *scene* and return the number removed."""
    cameras = [obj for obj in scene.objects if obj.type == "CAMERA"]
    for obj in cameras:
        bpy.data.objects.remove(obj, do_unlink=True)
    return len(cameras)


def ue_tick_to_frame(tick: int, tick_num: int, display_rate: int) -> float:
    """Convert a UE sequencer tick to a Blender frame number."""
    return tick * display_rate / tick_num


def source_to_scene_time_scale(display_rate: float | int, source_display_rate: float | int | None) -> float:
    """Scale authored source frames onto the Blender scene display rate."""
    if not source_display_rate:
        return 1.0
    return float(display_rate) / float(source_display_rate)


def find_loaded_action_by_name(action_name: str):
    """Return the loaded action, including Blender duplicate/truncation fallbacks."""
    action = bpy.data.actions.get(action_name)
    if action is not None:
        return action

    matches = sorted(
        (a for a in bpy.data.actions if asset_linking.id_name_matches(action_name, a.name)),
        key=lambda a: a.name,
    )
    return matches[0] if matches else None


def asset_action_names_in_blend(blend_path: Path) -> set[str]:
    """Return asset-marked action names exposed by one library .blend file."""
    cached = _ASSET_ACTION_NAMES_CACHE.get(blend_path)
    if cached is not None:
        return cached

    names: set[str] = set()
    try:
        with bpy.data.libraries.load(str(blend_path), assets_only=True) as (data_from, _data_to):
            names = set(data_from.actions)
    except TypeError:
        with bpy.data.libraries.load(str(blend_path)) as (data_from, _data_to):
            names = set(data_from.actions)
    except Exception as exc:
        print(f"[FF7R JSON Import]     WARNING asset action scan failed '{blend_path}': {exc}")

    _ASSET_ACTION_NAMES_CACHE[blend_path] = names
    return names


def append_action_from_asset_libraries(
    action_name: str,
    asset_library_selection: str = asset_linking.ASSET_LIBRARY_ALL,
):
    """Append a named action from the selected asset-library .blend files."""
    if asset_library_selection == asset_linking.ASSET_LIBRARY_NONE:
        return None

    cache_key = (asset_library_selection, action_name)
    candidate_paths: list[Path]
    if cache_key in _ASSET_ACTION_PATH_CACHE:
        cached_path = _ASSET_ACTION_PATH_CACHE[cache_key]
        if cached_path is None:
            return None
        candidate_paths = [cached_path]
    else:
        cached_path = None
        candidate_paths = asset_linking.iter_asset_library_blend_paths(asset_library_selection)

    found_path: Path | None = cached_path if cached_path else None
    if found_path is None:
        for blend_path in candidate_paths:
            if asset_linking.resolve_library_id_name(asset_action_names_in_blend(blend_path), action_name):
                found_path = blend_path
                break
        _ASSET_ACTION_PATH_CACHE[cache_key] = found_path

    if found_path is None:
        return None

    print(f"[FF7R JSON Import]     Appending action '{action_name}' from '{found_path.name}'")
    try:
        with bpy.data.libraries.load(str(found_path), link=False, assets_only=True) as (data_from, data_to):
            library_action_name = asset_linking.resolve_library_id_name(data_from.actions, action_name)
            if library_action_name is None:
                return None
            data_to.actions = [library_action_name]
    except TypeError:
        with bpy.data.libraries.load(str(found_path), link=False) as (data_from, data_to):
            library_action_name = asset_linking.resolve_library_id_name(data_from.actions, action_name)
            if library_action_name is None:
                return None
            data_to.actions = [library_action_name]
    except Exception as exc:
        print(f"[FF7R JSON Import]     WARNING action append failed '{action_name}': {exc}")
        return None

    return find_loaded_action_by_name(action_name)


def find_action_by_name(
    action_name: str,
    asset_library_selection: str = asset_linking.ASSET_LIBRARY_ALL,
):
    """Return a loaded action, or append it from the selected asset libraries."""
    action = find_loaded_action_by_name(action_name)
    if action is not None:
        return action
    return append_action_from_asset_libraries(action_name, asset_library_selection)
