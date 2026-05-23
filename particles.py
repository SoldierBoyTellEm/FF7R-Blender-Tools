"""Particle placeholder helpers for FF7R effect JSON imports."""

from __future__ import annotations

import math
import os

import bpy
from mathutils import Euler, Vector


NIAGARA_COMPONENT_TYPE = "NiagaraComponent"
END_NIAGARA_ACTOR_TYPE = "EndNiagaraActor"


def _float_or_default(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_effect_json(filepath: str) -> bool:
    return os.path.basename(filepath).lower().endswith("_effect.json")


def _ensure_parent_collection_for_file(filepath: str) -> bpy.types.Collection:
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    col = bpy.data.collections.get(base_name)
    if col is None:
        col = bpy.data.collections.new(base_name)

    scene = bpy.context.scene
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)

    return col


def _ensure_child_collection(
    parent_collection: bpy.types.Collection,
    child_name: str,
) -> bpy.types.Collection:
    col = bpy.data.collections.get(child_name)
    if col is None:
        col = bpy.data.collections.new(child_name)

    if parent_collection.children.get(col.name) is None:
        parent_collection.children.link(col)

    return col


def _ensure_particles_collection(filepath: str) -> bpy.types.Collection:
    root = _ensure_parent_collection_for_file(filepath)
    return _ensure_child_collection(root, f"{root.name}_Particles")


def _location_from_relative(loc_dict: dict, scale_factor: float = 0.01) -> Vector:
    x = _float_or_default(loc_dict.get("X", 0.0)) * scale_factor
    y = -_float_or_default(loc_dict.get("Y", 0.0)) * scale_factor
    z = _float_or_default(loc_dict.get("Z", 0.0)) * scale_factor
    return Vector((x, y, z))


def _rotation_from_relative(rot_dict: dict) -> Euler:
    pitch = math.radians(_float_or_default(rot_dict.get("Pitch", 0.0)))
    yaw = math.radians(_float_or_default(rot_dict.get("Yaw", 0.0)))
    roll = math.radians(_float_or_default(rot_dict.get("Roll", 0.0)))
    return Euler((roll, -pitch, -yaw), "XYZ")


def _outer_name(entry: dict) -> str:
    outer = entry.get("Outer", "")
    if isinstance(outer, str):
        return outer
    if isinstance(outer, dict):
        obj_name = outer.get("ObjectName", "")
        if isinstance(obj_name, str) and obj_name:
            return obj_name.rsplit(".", 1)[-1].rstrip("'")
    return ""


def _object_ref_path(ref: dict) -> str:
    if not isinstance(ref, dict):
        return ""
    obj_name = ref.get("ObjectName", "")
    if isinstance(obj_name, str) and "'" in obj_name:
        return obj_name.split("'", 1)[1].rstrip("'")
    if isinstance(obj_name, str):
        return obj_name
    return ""


def _asset_name_from_ref(ref: dict) -> str:
    ref_path = _object_ref_path(ref)
    if not ref_path:
        return ""
    return ref_path.rsplit("/", 1)[-1].split(".", 1)[0].split(":", 1)[-1]


def _component_key(entry: dict) -> str:
    outer_name = _outer_name(entry)
    comp_name = entry.get("Name", "")
    if outer_name and comp_name:
        return f"{outer_name}.{comp_name}"
    return str(comp_name or outer_name)


def _actor_ref_component_key(actor_entry: dict) -> str:
    props = actor_entry.get("Properties", {})
    if not isinstance(props, dict):
        return ""
    component_ref = props.get("NiagaraComponent") or props.get("RootComponent")
    component_path = _object_ref_path(component_ref)
    if not component_path:
        return ""
    if "PersistentLevel." in component_path:
        component_path = component_path.split("PersistentLevel.", 1)[1]
    return component_path.split(":", 1)[-1]


def _create_particle_empty(
    name: str,
    entry: dict,
    actor_entry: dict | None,
    collection: bpy.types.Collection,
    location_scale: float,
    filepath: str,
) -> bpy.types.Object:
    props = entry.get("Properties", {})
    if not isinstance(props, dict):
        props = {}

    loc = _location_from_relative(props.get("RelativeLocation", {}), location_scale)
    rot = _rotation_from_relative(props.get("RelativeRotation", {}))

    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "SPHERE"
    obj.empty_display_size = 1.0
    obj.location = loc
    obj.rotation_euler = rot
    obj["ff7r_source_json"] = filepath
    obj["ff7r_particle_component_name"] = entry.get("Name", "")
    obj["ff7r_particle_component_outer"] = _outer_name(entry)
    obj["ff7r_particle_component_type"] = entry.get("Type", "")

    asset_ref = props.get("Asset", {})
    asset_name = _asset_name_from_ref(asset_ref)
    if asset_name:
        obj["ff7r_niagara_system"] = asset_name
    if isinstance(asset_ref, dict):
        if asset_ref.get("ObjectName"):
            obj["ff7r_niagara_system_object_name"] = asset_ref.get("ObjectName")
        if asset_ref.get("ObjectPath"):
            obj["ff7r_niagara_system_object_path"] = asset_ref.get("ObjectPath")
    if props.get("m_VfxCategory"):
        obj["ff7r_vfx_category"] = props.get("m_VfxCategory")
    if props.get("bAbsoluteLocation") is not None:
        obj["ff7r_b_absolute_location"] = bool(props.get("bAbsoluteLocation"))
    if props.get("bAbsoluteRotation") is not None:
        obj["ff7r_b_absolute_rotation"] = bool(props.get("bAbsoluteRotation"))

    if actor_entry is not None:
        obj["ff7r_particle_actor_type"] = actor_entry.get("Type", "")
        obj["ff7r_particle_actor_name"] = actor_entry.get("Name", "")
        actor_props = actor_entry.get("Properties", {})
        if isinstance(actor_props, dict):
            tags = actor_props.get("Tags", [])
            if isinstance(tags, list):
                obj["ff7r_particle_actor_tags"] = ", ".join(str(tag) for tag in tags)

    collection.objects.link(obj)
    return obj


def create_niagara_empties_from_effect_json(
    data: list,
    filepath: str,
    location_scale: float = 0.01,
) -> int:
    """Create empties for EndNiagaraActor/NiagaraComponent entries in *_Effect.json files."""
    if not _is_effect_json(filepath):
        return 0

    actor_by_component_key: dict[str, dict] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("Type") != END_NIAGARA_ACTOR_TYPE:
            continue
        component_key = _actor_ref_component_key(entry)
        if component_key:
            actor_by_component_key[component_key] = entry

    component_entries = [
        entry for entry in data
        if isinstance(entry, dict) and entry.get("Type") == NIAGARA_COMPONENT_TYPE
    ]
    if not component_entries:
        return 0

    collection = _ensure_particles_collection(filepath)
    created_count = 0
    for entry in component_entries:
        component_key = _component_key(entry)
        actor_entry = actor_by_component_key.get(component_key)
        outer_name = _outer_name(entry)
        component_name = str(entry.get("Name", "NiagaraComponent"))

        if actor_entry is not None and actor_entry.get("Name"):
            empty_name = str(actor_entry.get("Name"))
        elif outer_name:
            empty_name = f"{outer_name}.{component_name}"
        else:
            empty_name = component_name

        _create_particle_empty(
            name=empty_name,
            entry=entry,
            actor_entry=actor_entry,
            collection=collection,
            location_scale=location_scale,
            filepath=filepath,
        )
        created_count += 1

    print(
        f"[End JSON Import]   Created {created_count} Niagara placeholder empty(s) "
        f"for '{os.path.basename(filepath)}'."
    )
    return created_count
