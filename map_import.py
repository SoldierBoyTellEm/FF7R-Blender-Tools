import bpy
import json
import os
import math
import re
import importlib
from mathutils import Vector, Euler, Matrix

from . import asset_linking, lights


ASSET_LIBRARY_SELECTION = asset_linking.ASSET_LIBRARY_ALL
IMPORT_MASSIVE_ENVIRONMENT_UMAPS = True
VERBOSE_OVERRIDE_LOGGING = False

# Blender spot lights emit along local -Z, while UE components use +X forward.
# Apply this after the normal mesh-style UE->Blender axis conversion.
_LIGHT_FWD_FIX = (
    Matrix.Rotation(math.radians(90.0), 4, "X") @
    Matrix.Rotation(math.radians(-90.0), 4, "Y")
)

# ---------------------------------------------------------------------------
# Asset index compatibility helpers
# ---------------------------------------------------------------------------

_ASSET_INDEX_CACHE = None  # type: dict[str, list[str]] | None
_ASSET_INDEX_SELECTION = None  # type: str | None


def build_asset_index(selection: str | None = None) -> dict:
    """Build mapping: collection name -> .blend files for the selected prop library."""
    return asset_linking.build_collection_asset_index(selection or ASSET_LIBRARY_SELECTION)


def get_asset_index(selection: str | None = None) -> dict:
    """Get (and lazily build) a collection asset index for the selected prop library."""
    global _ASSET_INDEX_CACHE, _ASSET_INDEX_SELECTION

    resolved_selection = selection or ASSET_LIBRARY_SELECTION
    if _ASSET_INDEX_CACHE is None or _ASSET_INDEX_SELECTION != resolved_selection:
        print("[End JSON Import] Building asset index (this runs once per session)...")
        _ASSET_INDEX_CACHE = build_asset_index(resolved_selection)
        _ASSET_INDEX_SELECTION = resolved_selection
        print(f"[End JSON Import] Indexed {len(_ASSET_INDEX_CACHE)} collection names.")
    return _ASSET_INDEX_CACHE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_static_mesh_name(object_name_str: str) -> str | None:
    """
    From Unreal-like object name: "StaticMesh'Block_GSFlowerBed_04A'"
    -> "Block_GSFlowerBed_04A"
    """
    if not object_name_str:
        return None
    if "'" in object_name_str:
        parts = object_name_str.split("'")
        if len(parts) >= 2:
            return parts[1]
    return object_name_str


def ensure_parent_collection_for_file(filepath: str) -> bpy.types.Collection:
    """
    Create or get a collection named after the JSON file (without extension),
    and ensure it is linked to the current scene.
    """
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    col = bpy.data.collections.get(base_name)
    if col is None:
        col = bpy.data.collections.new(base_name)

    scene = bpy.context.scene
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)

    return col


def _collection_matches_asset_name(collection: bpy.types.Collection, asset_name: str) -> bool:
    """
    Return True when *collection* looks like a datablock for *asset_name*,
    allowing Blender numeric suffixes such as ".001".
    """
    if collection is None:
        return False
    return collection.name == asset_name or collection.name.startswith(f"{asset_name}.")


def find_loaded_linked_source_collection(asset_name: str) -> bpy.types.Collection | None:
    """
    Find an already-loaded linked source collection for *asset_name* while
    ignoring local override collections created for previous actor instances.
    """
    for col in bpy.data.collections:
        if not _collection_matches_asset_name(col, asset_name):
            continue
        if col.library is None:
            continue
        if getattr(col, "override_library", None) is not None:
            continue
        return col
    return None


def find_or_load_collection_cached(asset_name: str) -> bpy.types.Collection | None:
    """
    Find an existing linked source collection, or use the cached asset index
    to locate a .blend file that contains a collection with that name and link it.
    """
    return asset_linking.find_or_load_collection(asset_name, ASSET_LIBRARY_SELECTION)


def find_or_load_asset_cached(asset_name: str) -> asset_linking.LinkedAsset | None:
    """
    Find a linked collection asset, or fall back to a lone linked object asset.
    """
    return asset_linking.find_or_load_asset(asset_name, ASSET_LIBRARY_SELECTION)


def create_collection_instance(
    collection: bpy.types.Collection,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
):
    """Create a collection instance object with the given transform."""
    obj = bpy.data.objects.new(name, None)
    obj.instance_type = "COLLECTION"
    obj.instance_collection = collection

    obj.location = location
    obj.rotation_euler = rotation_euler

    parent_collection.objects.link(obj)
    return obj


def create_linked_object_instance(
    source_obj: bpy.types.Object,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
):
    """Create a directly-linked object instance copy with the given transform."""
    obj = source_obj.copy()
    obj.name = name
    obj.rotation_mode = "XYZ"
    obj.location = location
    obj.rotation_euler = rotation_euler
    parent_collection.objects.link(obj)
    return obj


def create_wrapped_linked_object_instance(
    source_obj: bpy.types.Object,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
):
    """
    Create a stable local wrapper empty and place a linked object copy under it
    at identity transform.
    """
    wrapper = create_mesh_empty(name, location, rotation_euler, parent_collection)

    instance_obj = source_obj.copy()
    instance_obj.name = f"{name}__INSTANCE"
    instance_obj.rotation_mode = "XYZ"
    instance_obj.location = Vector((0.0, 0.0, 0.0))
    instance_obj.rotation_euler = Euler((0.0, 0.0, 0.0), "XYZ")
    instance_obj.parent = wrapper

    parent_collection.objects.link(instance_obj)
    return wrapper, instance_obj


def create_skeletal_wrapper_instance(
    collection: bpy.types.Collection,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
):
    """
    Create a stable local empty for the actor and put the linked skeletal
    collection instance under it at identity transform.
    """
    return create_wrapped_collection_instance(
        collection=collection,
        name=name,
        location=location,
        rotation_euler=rotation_euler,
        parent_collection=parent_collection,
    )


def create_wrapped_collection_instance(
    collection: bpy.types.Collection,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
):
    """
    Create a stable local wrapper empty and place the linked collection
    instance under it at identity transform.
    """
    wrapper = create_mesh_empty(name, location, rotation_euler, parent_collection)

    instance_obj = bpy.data.objects.new(f"{name}__INSTANCE", None)
    instance_obj.instance_type = "COLLECTION"
    instance_obj.instance_collection = collection
    instance_obj.location = Vector((0.0, 0.0, 0.0))
    instance_obj.rotation_euler = Euler((0.0, 0.0, 0.0), "XYZ")
    instance_obj.parent = wrapper

    parent_collection.objects.link(instance_obj)
    return wrapper, instance_obj


def assign_skeletal_source_metadata(
    wrapper_obj: bpy.types.Object,
    instance_obj: bpy.types.Object,
    instance_name: str,
    template_object_path: str | None,
) -> None:
    """
    Store import provenance on the actor wrapper and its current linked helper.
    """
    wrapper_obj["source_name"] = instance_name
    if template_object_path:
        wrapper_obj["template_object_path"] = template_object_path
    instance_obj["source_name"] = instance_name
    if template_object_path:
        instance_obj["template_object_path"] = template_object_path


def rotation_from_relative(rot_dict: dict) -> Euler:
    """
    Convert the RelativeRotation dict to a Blender XYZ Euler.

    Requirements:
    - X and Y rotations appeared swapped -> use Roll on X, Pitch on Y.
    - Z rotation inverted -> negate Yaw.
    - Y rotation also inverted -> negate Pitch.
    - Yaw still corresponds to Blender's Z axis.

    Unreal:
      Pitch: rotate around Y
      Yaw  : rotate around Z
      Roll : rotate around X

    Mapping to Blender XYZ euler:
      X = Roll
      Y = -Pitch
      Z = -Yaw
    """
    pitch_deg = float(rot_dict.get("Pitch", 0.0))
    yaw_deg = float(rot_dict.get("Yaw", 0.0))
    roll_deg = float(rot_dict.get("Roll", 0.0))

    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)
    roll = math.radians(roll_deg)

    return Euler((roll, -pitch, -yaw), "XYZ")


def light_rotation_from_relative(rot_dict: dict) -> Euler:
    """
    Convert RelativeRotation with the same mesh axis mapping, then align the
    Blender spot-light emission axis to UE's component-forward axis.
    """
    mesh_rot = rotation_from_relative(rot_dict)
    return (mesh_rot.to_matrix().to_4x4() @ _LIGHT_FWD_FIX).to_euler("XYZ")


def location_from_relative(loc_dict: dict, scale_factor: float = 0.01) -> Vector:
    """
    Convert the RelativeLocation dict to a Blender Vector and apply scale factor.

    Requirement:
    - Y location should be inverted.
    """
    x = float(loc_dict.get("X", 0.0)) * scale_factor
    y = -float(loc_dict.get("Y", 0.0)) * scale_factor  # invert Y
    z = float(loc_dict.get("Z", 0.0)) * scale_factor
    return Vector((x, y, z))


def _float_or_default(value, default: float = 0.0) -> float:
    """Safely coerce *value* to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_or_default(value, default: bool = False) -> bool:
    """Safely coerce common JSON-ish truthy values to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _get_light_temperature(props: dict) -> float | None:
    """
    Return whichever temperature key the export uses.

    FModel UMAP exports have shown both `Temperature` and `ColorTemperature`.
    """
    if "Temperature" in props:
        return _float_or_default(props.get("Temperature"), 6500.0)
    if "ColorTemperature" in props:
        return _float_or_default(props.get("ColorTemperature"), 6500.0)
    return None


def _resolve_light_name(entry: dict, default_name: str) -> str:
    """Use the owning actor name for light objects when the JSON provides it."""
    outer_name = _resolve_outer_name(entry)
    entry_name = entry.get("Name", default_name)
    entry_name = entry_name if isinstance(entry_name, str) and entry_name else default_name
    if outer_name:
        return f"{outer_name}.{entry_name}"
    return entry_name


def create_point_light_from_entry(
    entry: dict,
    parent_collection: bpy.types.Collection,
    location_scale: float,
    exposure_mult: float,
    attenuation_radius_mult: float,
    attach_parent_obj: bpy.types.Object | None = None,
):
    """
    Create a Blender point light from a UE PointLightComponent JSON entry.
    """
    props = entry.get("Properties", {})

    # Intensity / energy
    if "Intensity" in props:
        raw_intensity = props.get("Intensity", 0.0)
    else:
        raw_intensity = entry.get("IntensityNits", 0.0)

    intensity = _float_or_default(raw_intensity)
    energy = intensity * exposure_mult

    light_name = _resolve_light_name(entry, "PointLight")

    light_data = lights.create_static_light_data(
        light_name,
        "POINT",
        {**entry, **props},
        location_scale=location_scale,
        exposure_mult=exposure_mult,
        attenuation_radius_mult=attenuation_radius_mult,
    )
    energy = light_data.energy

    # Color (R/G/B 0-255 -> 0-1)
    r, g, b = light_data.color

    # Location / rotation (Properties.* preferred, else top-level)
    loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
    rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))

    loc = location_from_relative(loc_dict, scale_factor=location_scale)
    rot = rotation_from_relative(rot_dict)

    light_obj = bpy.data.objects.new(light_name, light_data)
    if attach_parent_obj is not None:
        light_obj.parent = attach_parent_obj
    light_obj.location = loc
    light_obj.rotation_euler = rot
    lights.apply_ue_light_object_properties(light_obj)

    parent_collection.objects.link(light_obj)
    return light_obj


def create_spot_light_from_entry(
    entry: dict,
    parent_collection: bpy.types.Collection,
    location_scale: float,
    exposure_mult: float,
    attenuation_radius_mult: float,
    attach_parent_obj: bpy.types.Object | None = None,
):
    """
    Create a Blender spot light from a UE SpotLightComponent JSON entry.
    """
    props = entry.get("Properties", {})

    if "Intensity" in props:
        raw_intensity = props.get("Intensity", 0.0)
    else:
        raw_intensity = entry.get("IntensityNits", 0.0)

    intensity = _float_or_default(raw_intensity)
    energy = intensity * exposure_mult

    light_name = _resolve_light_name(entry, "SpotLight")
    light_data = lights.create_static_light_data(
        light_name,
        "SPOT",
        {**entry, **props},
        location_scale=location_scale,
        exposure_mult=exposure_mult,
        attenuation_radius_mult=attenuation_radius_mult,
    )
    energy = light_data.energy
    r, g, b = light_data.color

    loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
    rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))

    loc = location_from_relative(loc_dict, scale_factor=location_scale)
    rot = light_rotation_from_relative(rot_dict)

    light_obj = bpy.data.objects.new(light_name, light_data)
    if attach_parent_obj is not None:
        light_obj.parent = attach_parent_obj
    light_obj.location = loc
    light_obj.rotation_euler = rot
    lights.apply_ue_light_object_properties(light_obj)

    parent_collection.objects.link(light_obj)
    return light_obj


def extract_skeletal_mesh_name(entry: dict) -> tuple[str | None, str | None]:
    """
    Extract the asset-lookup name and the template object path from an
    EndSkeletalMeshComponent entry.

    Asset lookup is derived from Template.ObjectPath, e.g.:
      "/Game/BluePrint/Character/Environment/BG0215_00_Switch_Standard.2"
    We take the last path segment before the dot-index:
      "BG0215_00_Switch_Standard"
    That is passed to the asset library lookup.

    The raw Template.ObjectPath string is also returned so callers can store
    it as a custom property on the created object.

    Returns:
        (asset_name, template_object_path)
        Either or both may be None if the data is absent.
    """
    template = entry.get("Template", {})
    if not isinstance(template, dict):
        return None, None

    obj_path = template.get("ObjectPath", "")
    if not obj_path:
        return None, None

    # "/Game/.../BG0215_00_Switch_Standard.2"  ->  "BG0215_00_Switch_Standard"
    last_segment = obj_path.rstrip("/").rsplit("/", 1)[-1]  # "BG0215_00_Switch_Standard.2"
    asset_name = last_segment.split(".")[0] or None           # "BG0215_00_Switch_Standard"

    return asset_name or None, obj_path


def _resolve_outer_name(entry: dict) -> str:
    """
    Return a human-readable actor name from the "Outer" field, which may be
    a plain string or a dict depending on the source JSON.
    """
    outer_raw = entry.get("Outer", "")
    if isinstance(outer_raw, dict):
        obj_name = outer_raw.get("ObjectName", "")
        # "ClassName'Path:PersistentLevel.ActorName'" -> "ActorName"
        name = obj_name.rsplit(".", 1)[-1].rstrip("'").strip()
        return name
    return outer_raw.strip()


def _object_reference_keys(ref: dict) -> set[str]:
    """
    Return stable lookup keys for an Unreal object reference dict.

    FModel references often include both a full ObjectName and an ObjectPath;
    the ObjectName is the useful one for matching component attachments, but
    shorter PersistentLevel/actor.component suffixes make constructed keys
    resilient to class-name differences.
    """
    keys: set[str] = set()
    if not isinstance(ref, dict):
        return keys

    obj_name = ref.get("ObjectName", "")
    if isinstance(obj_name, str) and obj_name:
        cleaned = obj_name.strip()
        _add_reference_key_with_aliases(keys, cleaned)
        if "PersistentLevel." in cleaned:
            after = cleaned.split("PersistentLevel.", 1)[1].rstrip("'")
            _add_reference_key_with_aliases(keys, after)

    obj_path = ref.get("ObjectPath", "")
    if isinstance(obj_path, str) and obj_path:
        _add_reference_key_with_aliases(keys, obj_path.strip())

    return {key for key in keys if key}


def _object_reference_lookup_keys(ref: dict) -> list[str]:
    """
    Return AttachParent lookup keys in priority order.

    Exact Unreal references are tried first; XENGINE__ aliases are only
    fallback keys so they cannot shadow a more precise component match.
    """
    exact_keys: list[str] = []
    alias_keys: list[str] = []

    def _append_unique(target: list[str], key: str):
        if key and key not in exact_keys and key not in alias_keys:
            target.append(key)

    def _add_exact(key: str):
        if not key:
            return
        _append_unique(exact_keys, key)
        for alias in _xengine_reference_aliases(key):
            _append_unique(alias_keys, alias)

    if not isinstance(ref, dict):
        return []

    obj_name = ref.get("ObjectName", "")
    if isinstance(obj_name, str) and obj_name:
        cleaned = obj_name.strip()
        _add_exact(cleaned)
        if "PersistentLevel." in cleaned:
            _add_exact(cleaned.split("PersistentLevel.", 1)[1].rstrip("'"))

    obj_path = ref.get("ObjectPath", "")
    if isinstance(obj_path, str) and obj_path:
        _add_exact(obj_path.strip())

    return exact_keys + alias_keys


def _xengine_reference_aliases(key: str) -> set[str]:
    """
    Return lookup aliases with an XENGINE__ actor prefix stripped.

    Some map exports attach lights to paths like
    ``PersistentLevel.XENGINE__Light_Wall_06A139.StaticMeshComponent0`` even
    when the useful actor identity is the unprefixed ``Light_Wall_06A139``.
    """
    aliases: set[str] = set()
    if not isinstance(key, str) or "XENGINE__" not in key:
        return aliases

    def _strip_actor_prefix(path: str) -> str | None:
        actor, sep, rest = path.partition(".")
        if not actor.startswith("XENGINE__"):
            return None
        actor = actor[len("XENGINE__"):]
        return f"{actor}{sep}{rest}" if sep else actor

    if "PersistentLevel." in key:
        prefix, rest = key.split("PersistentLevel.", 1)
        normalized = _strip_actor_prefix(rest.rstrip("'"))
        if normalized:
            suffix = "'" if key.endswith("'") else ""
            aliases.add(f"{prefix}PersistentLevel.{normalized}{suffix}")
            aliases.add(normalized)
    else:
        normalized = _strip_actor_prefix(key.rstrip("'"))
        if normalized:
            suffix = "'" if key.endswith("'") else ""
            aliases.add(f"{normalized}{suffix}")

    return aliases


def _add_reference_key_with_aliases(keys: set[str], key: str):
    """Add a reference key plus normalized aliases used for loose matching."""
    if not key:
        return
    keys.add(key)
    keys.update(_xengine_reference_aliases(key))


def _entry_component_reference_keys(entry: dict) -> set[str]:
    """
    Build lookup keys for a component entry so AttachParent can find the
    Blender object created for that component later in the same JSON file.
    """
    keys: set[str] = set()
    component_name = entry.get("Name")
    outer = entry.get("Outer", {})

    if not isinstance(component_name, str) or not component_name:
        return keys

    outer_obj_name = outer.get("ObjectName", "") if isinstance(outer, dict) else ""
    if isinstance(outer_obj_name, str) and outer_obj_name:
        if "'" in outer_obj_name:
            outer_path = outer_obj_name.split("'", 1)[1].rstrip("'")
            full_component_path = f"{outer_path}.{component_name}"
            _add_reference_key_with_aliases(keys, f"{entry.get('Type')}'{full_component_path}'")
            if "PersistentLevel." in full_component_path:
                _add_reference_key_with_aliases(
                    keys,
                    full_component_path.split("PersistentLevel.", 1)[1],
                )

    if isinstance(outer, str) and outer:
        _add_reference_key_with_aliases(keys, f"{outer}.{component_name}")

    return keys


def register_component_object(
    component_objects: dict[str, bpy.types.Object],
    entry: dict,
    obj: bpy.types.Object,
):
    """Register *obj* under the Unreal component-reference keys for *entry*."""
    if obj is None:
        return
    for key in _entry_component_reference_keys(entry):
        component_objects[key] = obj


def _register_actor_object(
    actor_objects: dict[str, bpy.types.Object],
    actor_name: str,
    obj: bpy.types.Object,
):
    """Register an object by actor/Outer name plus XENGINE__ aliases."""
    if not actor_name or obj is None:
        return
    actor_objects[actor_name] = obj
    for alias in _xengine_reference_aliases(actor_name):
        actor_objects.setdefault(alias, obj)


def _reference_export_index(ref: dict) -> int | None:
    """Return the numeric export index at the end of an ObjectPath, if present."""
    if not isinstance(ref, dict):
        return None
    obj_path = ref.get("ObjectPath", "")
    if not isinstance(obj_path, str) or "." not in obj_path:
        return None
    suffix = obj_path.rsplit(".", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return None


def register_override_material_targets(
    material_targets: dict[int, bpy.types.Object],
    props: dict,
    obj: bpy.types.Object,
):
    """Map MaterialInstanceDynamic export indices from OverrideMaterials to *obj*."""
    for ref in props.get("OverrideMaterials", []) or []:
        export_index = _reference_export_index(ref)
        if export_index is not None:
            material_targets[export_index] = obj


def _color_prop_values(color: dict) -> tuple[list[float] | None, str | None]:
    if not isinstance(color, dict):
        return None, None
    channels = []
    for channel in ("R", "G", "B", "A"):
        if channel in color:
            channels.append(_float_or_default(color.get(channel)))
    hex_value = color.get("Hex")
    return (channels or None), hex_value if isinstance(hex_value, str) else None


def _apply_material_parameter_light_props(obj: bpy.types.Object, props: dict):
    mpl = props.get("MaterialParameterLight", {})
    if obj is None or not isinstance(mpl, dict):
        return
    if "Intensity" in mpl:
        obj["material_parameter_light_intensity"] = _float_or_default(mpl.get("Intensity"))
    if "ColorTemperature" in mpl:
        obj["material_parameter_light_color_temperature"] = _float_or_default(
            mpl.get("ColorTemperature")
        )
    color_value, hex_value = _color_prop_values(mpl.get("Color", {}))
    if color_value is not None:
        obj["material_parameter_light_color"] = color_value
    if hex_value is not None:
        obj["material_parameter_light_color_hex"] = hex_value


def _apply_vector_parameter_props(obj: bpy.types.Object, props: dict):
    if obj is None:
        return
    for param in props.get("VectorParameterValues", []) or []:
        if not isinstance(param, dict):
            continue
        info = param.get("ParameterInfo", {})
        name = info.get("Name", "VectorParameter")
        value = param.get("ParameterValue", {})
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        color_value, hex_value = _color_prop_values(value)
        prop_prefix = f"material_vector_{name}"
        if color_value is not None:
            obj[prop_prefix] = color_value
        if hex_value is not None:
            obj[f"{prop_prefix}_hex"] = hex_value


def resolve_attach_parent_object(
    props: dict,
    component_objects: dict[str, bpy.types.Object],
    child_name: str = "object",
    report_missing: bool = True,
) -> bpy.types.Object | None:
    """Resolve Properties.AttachParent to a previously imported Blender object."""
    attach_parent = props.get("AttachParent", {})
    if not attach_parent:
        return None

    lookup_keys = _object_reference_lookup_keys(attach_parent)
    for key in lookup_keys:
        obj = component_objects.get(key)
        if obj is not None:
            return obj

    actor_name = _resolve_attach_parent_name(props)
    if actor_name:
        obj = bpy.data.objects.get(actor_name)
        if obj is not None:
            return obj

    if report_missing:
        object_name = attach_parent.get("ObjectName", "<missing ObjectName>")
        print(
            f"[End JSON Import]   AttachParent for '{child_name}' could not be resolved: "
            f"{object_name!r} (tried {len(lookup_keys)} key(s))."
        )
    return None


def apply_deferred_light_parent(
    light_obj: bpy.types.Object,
    props: dict,
    parent_obj: bpy.types.Object,
    location_scale: float,
):
    """Parent a previously-created light while keeping JSON local transform semantics."""
    light_obj.parent = parent_obj
    loc_dict = props.get("RelativeLocation", {})
    rot_dict = props.get("RelativeRotation", {})
    light_obj.location = location_from_relative(loc_dict, scale_factor=location_scale)
    if getattr(light_obj.data, "type", None) == "SPOT":
        light_obj.rotation_euler = light_rotation_from_relative(rot_dict)
    else:
        light_obj.rotation_euler = rotation_from_relative(rot_dict)


def apply_deferred_object_parent(
    child_obj: bpy.types.Object,
    props: dict,
    parent_obj: bpy.types.Object,
    location_scale: float,
):
    """Parent a mesh/helper object while keeping JSON local transform semantics."""
    child_obj.parent = parent_obj
    loc_dict = props.get("RelativeLocation", {})
    rot_dict = props.get("RelativeRotation", {})
    child_obj.location = location_from_relative(loc_dict, scale_factor=location_scale)
    child_obj.rotation_euler = rotation_from_relative(rot_dict)


def outer_to_instance_name(outer_name: str, asset_name: str) -> str:
    """
    Convert a UE actor name into a Blender-style name that preserves the
    instance number as a dot-suffix.

    UE appends ``_N`` when placing duplicate actors, e.g.:
      outer_name = "BG0522_00_Monitor_Standard_2"
      asset_name = "BG0522_00_Monitor_Standard"
      -> "BG0522_00_Monitor_Standard.2"

    When the outer name starts with the asset name but the remainder is not
    a plain ``_N`` pattern (e.g. "Standard2_2" after partial stripping),
    the outer name is returned unchanged so that uniqueness is still kept.
    """
    if asset_name and outer_name.startswith(asset_name):
        remainder = outer_name[len(asset_name):]
        m = re.match(r"^_(\d+)$", remainder)
        if m:
            return f"{asset_name}.{m.group(1)}"
    return outer_name


def create_mesh_empty(
    name: str,
    loc: "Vector",
    rot: "Euler",
    parent_collection: "bpy.types.Collection",
):
    """Create and link a PLAIN_AXES empty with the given transform."""
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 1.0
    obj.location = loc
    obj.rotation_euler = rot
    parent_collection.objects.link(obj)
    return obj


def resolve_game_asset_file_path(asset_path_name: str, game_root: str, extension: str) -> str | None:
    """
    Convert an Unreal asset path like:
      "/Game/Level/Game/Field/.../Name.Name"
    into a local exported file path:
      "<game_root>/Level/Game/Field/.../Name.<extension>"
    """
    if not game_root or not asset_path_name:
        return None

    prefix = "/Game/"
    if asset_path_name.startswith(prefix):
        rel = asset_path_name[len(prefix):]
    else:
        rel = asset_path_name.lstrip("/")

    rel_no_suffix = rel.split(".", 1)[0]
    rel_path = rel_no_suffix + extension
    rel_path_parts = rel_path.split("/")
    return os.path.join(game_root, *rel_path_parts)


def resolve_streaming_level_json_path(asset_path_name: str, game_root: str) -> str | None:
    """
    Convert an Unreal asset path into the matching exported JSON path.
    """
    return resolve_game_asset_file_path(asset_path_name, game_root, ".json")


def resolve_massive_environment_umap_path(entry: dict, game_root: str) -> str | None:
    """
    Convert a MassiveEnvironmentComponent's StreamingProxy path into a .umap path.

    The owning MassiveEnvironmentActor is a higher-level wrapper. The
    StreamingProxy points at the actual level package, e.g.:
      /Game/Level/Game/Field/1110-KALMT/Layout/1110-KALMT_Terrain_Strip.135
    which resolves to:
      <game_root>/Level/Game/Field/1110-KALMT/Layout/1110-KALMT_Terrain_Strip.umap
    """
    props = entry.get("Properties", {})
    if not isinstance(props, dict):
        return None

    streaming_proxy = props.get("StreamingProxy")
    if not isinstance(streaming_proxy, dict):
        return None

    asset_path_name = streaming_proxy.get("ObjectPath")
    return resolve_game_asset_file_path(asset_path_name, game_root, ".umap")


def get_umap_import_function():
    """Return the bundled Massive Environment .umap import function."""
    try:
        importer_module = importlib.import_module(f"{__package__}.mec.importer")
    except Exception as exc:
        print(f"[End JSON Import]   Bundled FF7R .umap importer is not available: {exc}")
        return None
    return getattr(importer_module, "import_umap_paths", None)


def is_umap_addon_available() -> bool:
    """Return True when the bundled Massive Environment .umap importer can be called."""
    try:
        importer_module = importlib.import_module(f"{__package__}.mec.importer")
    except Exception:
        return False
    return callable(getattr(importer_module, "import_umap_paths", None))


def import_massive_environment_umap(
    entry: dict,
    game_root: str,
    imported_umap_paths: set[str],
) -> tuple[int, int, set[str]]:
    """
    Import the .umap referenced by a MassiveEnvironmentComponent, if possible.

    Returns:
        (processed, skipped, missing_assets)
    """
    if not game_root:
        print("[End JSON Import]   MassiveEnvironmentComponent found but Game Root is not set - skipping .umap import.")
        return 0, 1, set()

    umap_path = resolve_massive_environment_umap_path(entry, game_root)
    if not umap_path:
        print("[End JSON Import]   MassiveEnvironmentComponent path could not be resolved - skipping .umap import.")
        return 0, 1, set()

    umap_path = os.path.realpath(umap_path)
    if umap_path in imported_umap_paths:
        print(f"[End JSON Import]   MassiveEnvironment .umap already imported: {umap_path}")
        return 0, 0, set()

    if not os.path.exists(umap_path):
        print(f"[End JSON Import]   MassiveEnvironment .umap not found: {umap_path}")
        imported_umap_paths.add(umap_path)
        return 0, 1, set()

    import_umap_paths = get_umap_import_function()
    if import_umap_paths is None:
        imported_umap_paths.add(umap_path)
        return 0, 1, set()

    print(f"[End JSON Import]   Importing MassiveEnvironment .umap: {umap_path}")
    imported_umap_paths.add(umap_path)
    try:
        processed, skipped = import_umap_paths(
            bpy.context,
            [umap_path],
            lod_mode="LEVEL",
            lod_level=0,
            scale_factor=0.01,
        )
        return processed, skipped, set()
    except Exception as exc:
        print(f"[End JSON Import]   MassiveEnvironment .umap import failed for {umap_path}: {exc}")
        return 0, 1, set()


def find_bone_by_socket_name(armature_obj: bpy.types.Object, socket_name: str):
    """
    Search the pose bones of *armature_obj* for one whose custom property
    'SocketName' equals *socket_name*.  Returns the bone name string, or None.
    """
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return None
    for bone in armature_obj.pose.bones:
        if bone.get("SocketName") == socket_name:
            return bone.name
    return None


def make_library_override_for_instance(instance_obj: bpy.types.Object) -> bpy.types.Object | None:
    """
    If *instance_obj* is a linked collection instance whose collection still
    lives in a library, create a library override for the collection's *content*
    so that every object inside the hierarchy becomes an individually addressable
    local override object.

    This is the Python equivalent of:
        Library Override > Make > Content
    in the Blender UI (right-click on the instance in the Outliner).

    ``ID.override_hierarchy_create`` creates local override copies of the
    collection AND all objects within it, making them reachable via
    ``instance_obj.children_recursive``, selectable in the viewport, and
    usable for bone parenting - unlike ``bpy.ops.object.make_override_library``
    which only overrides the instance empty itself and leaves the interior
    objects as unaddressable linked data.

    Returns *instance_obj* (with its instance_collection updated to the new
    local override collection), or *instance_obj* unchanged if the override
    cannot be created.
    """
    col = instance_obj.instance_collection
    if col is None or col.library is None:
        # Already local or not a linked collection - nothing to do.
        return instance_obj

    try:
        scene = bpy.context.scene
        view_layer = bpy.context.view_layer

        # override_hierarchy_create is the direct Python equivalent of
        # "Library Override > Make > Content": it produces local override
        # copies of the collection and every object in its hierarchy.
        override_col = col.override_hierarchy_create(
            scene=scene,
            view_layer=view_layer,
        )

        if override_col is not None:
            # Redirect the instance empty to the new local collection so that
            # children_recursive is populated with the override objects.
            instance_obj.instance_collection = override_col
            # Flush the depsgraph so children_recursive reflects the new state.
            view_layer.update()
            print(
                f"[End JSON Import]   Library override (content) created for "
                f"'{instance_obj.name}' -> collection '{override_col.name}'."
            )
        else:
            print(
                f"[End JSON Import]   override_hierarchy_create returned None for "
                f"'{instance_obj.name}' - override may already exist or collection "
                f"cannot be overridden."
            )

        return instance_obj

    except Exception as exc:
        print(
            f"[End JSON Import]   Could not make library override for "
            f"'{instance_obj.name}': {exc}"
        )
        return instance_obj


def ensure_overrideable_collection_instance(
    instance_obj: bpy.types.Object,
) -> bpy.types.Object | None:
    """
    Resolve a skeletal actor wrapper to its collection-instance child and
    create a local content override when the linked collection still comes
    from a library.
    """
    target_instance = instance_obj
    if (
        target_instance is not None
        and target_instance.instance_type != "COLLECTION"
    ):
        for child in target_instance.children:
            if child.instance_type == "COLLECTION" and child.instance_collection is not None:
                target_instance = child
                break

    if target_instance is None or target_instance.instance_type != "COLLECTION":
        return instance_obj

    col = target_instance.instance_collection
    if col is None or col.library is None:
        return target_instance

    try:
        scene = bpy.context.scene
        view_layer = bpy.context.view_layer
        override_result = None

        try:
            override_result = target_instance.override_hierarchy_create(
                scene=scene,
                view_layer=view_layer,
            )
        except Exception:
            override_result = None

        if isinstance(override_result, bpy.types.Object):
            target_instance = override_result
        elif isinstance(override_result, bpy.types.Collection):
            target_instance.instance_collection = override_result
        elif target_instance.instance_collection is not None and target_instance.instance_collection.library is not None:
            override_col = col.override_hierarchy_create(
                scene=scene,
                view_layer=view_layer,
            )
            if isinstance(override_col, bpy.types.Collection):
                target_instance.instance_collection = override_col

        view_layer.update()

        active_collection = target_instance.instance_collection
        if active_collection is not None and active_collection.library is None:
            print(
                f"[End JSON Import]   Library override (content) created for "
                f"'{target_instance.name}' -> collection '{active_collection.name}'."
            )
        else:
            print(
                f"[End JSON Import]   Library override request for "
                f"'{target_instance.name}' did not produce a local collection."
            )

        return target_instance

    except Exception as exc:
        print(
            f"[End JSON Import]   Could not make library override for "
            f"'{target_instance.name}': {exc}"
        )
        return target_instance


def ensure_top_level_object_overrides(
    instance_obj: bpy.types.Object,
):
    """
    Force local overrides for top-level objects in an overridden collection.

    Blender 5 can leave the collection override visible while the root objects
    themselves are still linked, which blocks adding constraints directly to
    those objects.
    """
    if instance_obj is None:
        print("[End JSON Import]   Top-level override pass skipped: instance_obj is None.")
        return

    override_collection = instance_obj.instance_collection
    if override_collection is None or override_collection.library is not None:
        print(
            f"[End JSON Import]   Top-level override pass skipped for "
            f"'{getattr(instance_obj, 'name', '<unknown>')}': no local override collection."
        )
        return

    if VERBOSE_OVERRIDE_LOGGING:
        print(
            f"[End JSON Import]   Forcing top-level object overrides for collection "
            f"'{override_collection.name}' via instance '{instance_obj.name}'."
        )

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    overridden_count = 0
    skipped_parented = 0
    skipped_override = 0
    skipped_local = 0
    failed_names: list[str] = []

    for obj in list(override_collection.objects):
        if obj.parent is not None:
            skipped_parented += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Skipping top-level override for '{obj.name}': "
                    f"already parented to '{obj.parent.name}'."
                )
            continue
        if obj.override_library is not None:
            skipped_override += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Skipping top-level override for '{obj.name}': "
                    f"object is already an override."
                )
            continue
        if obj.library is None:
            skipped_local += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Skipping top-level override for '{obj.name}': "
                    f"object is already local."
                )
            continue

        override_obj = None
        try:
            override_obj = obj.override_create(remap_local_usages=True)
        except Exception:
            override_obj = None

        if override_obj is None:
            try:
                override_obj = obj.override_hierarchy_create(
                    scene=scene,
                    view_layer=view_layer,
                )
            except Exception:
                override_obj = None

        if isinstance(override_obj, bpy.types.Object):
            overridden_count += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Created object override for '{obj.name}' "
                    f"-> '{override_obj.name}'."
                )
        else:
            failed_names.append(obj.name)

    if overridden_count:
        view_layer.update()
        print(
            f"[End JSON Import]   Forced object overrides for {overridden_count} "
            f"top-level collection object(s) in '{override_collection.name}'."
        )
    elif VERBOSE_OVERRIDE_LOGGING:
        print(
            f"[End JSON Import]   No new top-level object overrides were created for "
            f"'{override_collection.name}'."
        )

    if failed_names:
        preview = ", ".join(failed_names[:5])
        suffix = "" if len(failed_names) <= 5 else f", +{len(failed_names) - 5} more"
        print(
            f"[End JSON Import]   Failed to create {len(failed_names)} top-level "
            f"object override(s) in '{override_collection.name}': {preview}{suffix}."
        )
    elif VERBOSE_OVERRIDE_LOGGING and (skipped_parented or skipped_override or skipped_local):
        print(
            f"[End JSON Import]   Top-level override skipped existing objects in "
            f"'{override_collection.name}' "
            f"(parented={skipped_parented}, overrides={skipped_override}, local={skipped_local})."
        )


def parent_override_roots_to_wrapper(
    wrapper_obj: bpy.types.Object,
    instance_obj: bpy.types.Object,
):
    """
    Parent top-level overridden collection objects to the wrapper empty and
    clear their local transforms so the wrapper carries actor placement.
    """
    if wrapper_obj is None or instance_obj is None:
        print("[End JSON Import]   Wrapper parenting skipped: wrapper or instance is None.")
        return

    override_collection = instance_obj.instance_collection
    if override_collection is None or override_collection.library is not None:
        print(
            f"[End JSON Import]   Wrapper parenting skipped for "
            f"'{getattr(wrapper_obj, 'name', '<unknown>')}': no local override collection."
        )
        return

    if VERBOSE_OVERRIDE_LOGGING:
        print(
            f"[End JSON Import]   Parenting override roots from collection "
            f"'{override_collection.name}' under wrapper '{wrapper_obj.name}'."
        )

    reparented_count = 0
    skipped_helpers = 0
    skipped_parented = 0
    for obj in override_collection.objects:
        if obj == wrapper_obj or obj == instance_obj:
            skipped_helpers += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Skipping '{obj.name}' during wrapper parenting: "
                    f"wrapper/instance helper object."
                )
            continue
        if obj.parent is not None:
            skipped_parented += 1
            if VERBOSE_OVERRIDE_LOGGING:
                print(
                    f"[End JSON Import]     Skipping '{obj.name}' during wrapper parenting: "
                    f"already parented to '{obj.parent.name}'."
                )
            continue

        obj.parent = wrapper_obj
        obj.location = Vector((0.0, 0.0, 0.0))
        obj.rotation_euler = Euler((0.0, 0.0, 0.0), "XYZ")
        obj.scale = Vector((1.0, 1.0, 1.0))
        reparented_count += 1
        if VERBOSE_OVERRIDE_LOGGING:
            print(
                f"[End JSON Import]     Parented '{obj.name}' to '{wrapper_obj.name}' "
                f"and cleared local transforms."
            )

    if reparented_count:
        print(
            f"[End JSON Import]   Parented {reparented_count} top-level override object(s) "
            f"under '{wrapper_obj.name}'."
        )
    elif VERBOSE_OVERRIDE_LOGGING:
        print(
            f"[End JSON Import]   No override roots needed parenting under "
            f"'{wrapper_obj.name}' (helper={skipped_helpers}, already_parented={skipped_parented})."
        )

    instance_name = instance_obj.name
    bpy.data.objects.remove(instance_obj, do_unlink=True)
    if VERBOSE_OVERRIDE_LOGGING:
        print(
            f"[End JSON Import]   Removed temporary collection instance "
            f"'{instance_name}' after override expansion."
        )


def _strip_blender_numeric_suffix(name: str) -> str:
    """Return *name* without Blender's duplicate suffix, e.g. '.001'."""
    if not isinstance(name, str):
        return ""
    return re.sub(r"\.\d{3}$", "", name)


def find_attach_socket_empty(
    actor_obj: bpy.types.Object,
    socket_name: str,
) -> bpy.types.Object | None:
    """
    Search the descendants of *actor_obj* for an empty whose name ends with
    *socket_name*.
    """
    if actor_obj is None or not socket_name:
        print("[End JSON Import]   Socket search skipped: actor_obj missing or socket_name empty.")
        return None

    print(
        f"[End JSON Import]   Searching descendants of '{actor_obj.name}' "
        f"for socket empty ending with '{socket_name}'."
    )
    for child in actor_obj.children_recursive:
        child_name = _strip_blender_numeric_suffix(child.name)
        if child.type == "EMPTY" and child_name.endswith(socket_name):
            print(
                f"[End JSON Import]   Found socket empty '{child.name}' "
                f"under '{actor_obj.name}'."
            )
            return child
    print(
        f"[End JSON Import]   No socket empty ending with '{socket_name}' found under "
        f"'{actor_obj.name}'."
    )
    return None


def add_child_of_constraint(
    child_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    socket_name: str,
):
    """
    Add or reuse a Child Of constraint targeting *target_obj* on *child_obj*.
    """
    if child_obj is None or target_obj is None:
        return

    constraint_name = f"AttachSocket_{socket_name}"
    constraint = child_obj.constraints.get(constraint_name)
    if constraint is None or constraint.type != "CHILD_OF":
        constraint = child_obj.constraints.new("CHILD_OF")
        constraint.name = constraint_name

    constraint.target = target_obj
    constraint.subtarget = ""
    constraint.inverse_matrix = Matrix.Identity(4)

    print(
        f"[End JSON Import]   Added Child Of constraint '{constraint.name}' "
        f"on '{child_obj.name}' targeting '{target_obj.name}'."
    )


def try_attach_to_socket_empty(
    child_obj: bpy.types.Object,
    props: dict,
    socket_name: str,
) -> bool:
    """
    Try to attach *child_obj* to a descendant empty of the target skeletal
    actor whose name ends with *socket_name*.
    """
    attach_actor_name = _resolve_attach_parent_name(props)
    if not attach_actor_name:
        print(
            f"[End JSON Import]   Socket attach skipped for '{child_obj.name}': "
            f"AttachParent could not be resolved."
        )
        return False

    target_actor = bpy.data.objects.get(attach_actor_name)
    if target_actor is None:
        print(
            f"[End JSON Import]   Socket attach skipped for '{child_obj.name}': "
            f"target actor '{attach_actor_name}' not found."
        )
        return False

    print(
        f"[End JSON Import]   Attempting socket-empty attach for '{child_obj.name}' "
        f"to actor '{target_actor.name}' socket '{socket_name}'."
    )
    ensure_overrideable_collection_instance(target_actor)
    socket_obj = find_attach_socket_empty(target_actor, socket_name)
    if socket_obj is None:
        print(
            f"[End JSON Import]   Socket-empty attach failed for '{child_obj.name}': "
            f"no matching socket empty found."
        )
        return False

    add_child_of_constraint(child_obj, socket_obj, socket_name)
    print(
        f"[End JSON Import]   Socket-empty attach succeeded for '{child_obj.name}' "
        f"using '{socket_obj.name}'."
    )
    return True


def _resolve_attach_parent_name(props: dict) -> str | None:
    """
    Pull the actor instance name out of Properties.AttachParent.ObjectName.

    Example ObjectName value:
      "EndSkeletalMeshComponent'2350-GOLDS_Gimmick:PersistentLevel.BG1017_00_FerrisWheel_Standard_4.SkeletalMeshComponent0'"
    We want:
      "BG1017_00_FerrisWheel_Standard_4"
    i.e. the part after "PersistentLevel." and before the next ".".
    """
    attach_parent = props.get("AttachParent", {})
    if not isinstance(attach_parent, dict):
        return None
    obj_name = attach_parent.get("ObjectName", "")
    if not obj_name:
        return None
    # "SomeClass'Level:PersistentLevel.ActorName.ComponentName'"
    if "PersistentLevel." in obj_name:
        after = obj_name.split("PersistentLevel.", 1)[1]
        actor_and_rest = after.split(".")[0]
        return actor_and_rest.rstrip("'") or None
    # Fallback: last dotted segment stripped of quotes
    return obj_name.rsplit(".", 1)[-1].strip("'") or None


def parent_to_bone(
    child_obj: bpy.types.Object,
    armature_obj: bpy.types.Object,
    bone_name: str,
):
    """
    Parent *child_obj* to *bone_name* on *armature_obj* using 'BONE' parenting
    without clearing the child's existing transform.
    """
    child_obj.parent = armature_obj
    child_obj.parent_type = "BONE"
    child_obj.parent_bone = bone_name
    print(
        f"[End JSON Import]   Parented '{child_obj.name}' to bone '{bone_name}' "
        f"on armature '{armature_obj.name}'."
    )


def process_deferred_attach(
    child_obj: bpy.types.Object,
    props: dict,
    attach_socket: str,
    location_scale: float,
):
    """
    Resolve AttachSocketName after all objects for the file have been created.
    """
    print(
        f"[End JSON Import]   Processing deferred attach for '{child_obj.name}' "
        f"socket '{attach_socket}'."
    )

    if try_attach_to_socket_empty(child_obj, props, attach_socket):
        return

    attach_actor_name = _resolve_attach_parent_name(props)
    if attach_actor_name:
        target_instance = bpy.data.objects.get(attach_actor_name)
        if target_instance is None:
            print(
                f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                f"parent actor '{attach_actor_name}' not found in scene - skipping bone parent."
            )
        else:
            # Ensure the target collection instance has a library
            # override so its armature is accessible.
            target_instance = ensure_overrideable_collection_instance(target_instance)

            # Find the armature object inside the override hierarchy.
            armature_obj = None
            # The override may itself be an armature, or contain one
            # as a child.
            if target_instance.type == "ARMATURE":
                armature_obj = target_instance
            else:
                for child in target_instance.children_recursive:
                    if child.type == "ARMATURE":
                        armature_obj = child
                        break

            if armature_obj is None:
                print(
                    f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                    f"no armature found under '{target_instance.name}' - skipping bone parent."
                )
                apply_deferred_object_parent(child_obj, props, target_instance, location_scale)
                print(
                    f"[End JSON Import]   Attached '{child_obj.name}' to "
                    f"'{target_instance.name}' as fallback parent."
                )
            else:
                bone_name = find_bone_by_socket_name(armature_obj, attach_socket)
                if bone_name:
                    parent_to_bone(child_obj, armature_obj, bone_name)
                else:
                    print(
                        f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                        f"no bone with SocketName='{attach_socket}' found on "
                        f"'{armature_obj.name}' - skipping bone parent."
                    )
                    apply_deferred_object_parent(child_obj, props, target_instance, location_scale)
                    print(
                        f"[End JSON Import]   Attached '{child_obj.name}' to "
                        f"'{target_instance.name}' as fallback parent."
                    )
    else:
        print(
            f"[End JSON Import]   AttachSocketName '{attach_socket}' present but "
            f"AttachParent could not be resolved - skipping bone parent."
        )


def collect_asset_names_for_import(data: list) -> tuple[set[str], set[str]]:
    """Return unique static-mesh and skeletal-mesh asset names referenced by a JSON export."""
    static_asset_names: set[str] = set()
    skeletal_asset_names: set[str] = set()

    for entry in data:
        if not isinstance(entry, dict):
            continue

        t = entry.get("Type")
        if t in {"EndEnvironmentStaticMeshComponent", "StaticMeshComponent"}:
            props = entry.get("Properties", {})
            static_mesh = props.get("StaticMesh", {})
            if isinstance(static_mesh, dict):
                asset_name = extract_static_mesh_name(static_mesh.get("ObjectName"))
                if asset_name:
                    static_asset_names.add(asset_name)
        elif t == "EndSkeletalMeshComponent":
            asset_name, _template_object_path = extract_skeletal_mesh_name(entry)
            if asset_name:
                skeletal_asset_names.add(asset_name)

    return static_asset_names, skeletal_asset_names


def import_json_file(
    filepath: str,
    exposure_mult: float,
    attenuation_radius_mult: float,
    game_root: str,
    visited_paths: set[str],
    recursive_import: bool = True,
    import_massive_environment_umaps: bool = True,
    imported_umap_paths: set[str] | None = None,
) -> tuple[int, set[str]]:
    """
    Import one JSON environment file (recursively, via streaming volumes).

    Returns:
        (created_count, missing_assets)
    """
    filepath = os.path.realpath(filepath)

    if filepath in visited_paths:
        print(f"[End JSON Import] Skipping already-visited file: {filepath}")
        return 0, set()
    visited_paths.add(filepath)
    if imported_umap_paths is None:
        imported_umap_paths = set()

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Invalid file path: {filepath}")

    print(f"[End JSON Import] Loading: {filepath}")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to read JSON: {e}") from e

    if not isinstance(data, list):
        raise ValueError("JSON root must be a list of objects")

    print(f"[End JSON Import] JSON parsed - {len(data)} top-level entries.")

    static_asset_names, skeletal_asset_names = collect_asset_names_for_import(data)
    asset_linking.preload_assets(
        static_asset_names | skeletal_asset_names,
        ASSET_LIBRARY_SELECTION,
        include_objects=True,
    )
    static_asset_cache = {
        asset_name: find_or_load_asset_cached(asset_name)
        for asset_name in static_asset_names
    }
    skeletal_asset_cache = {
        asset_name: find_or_load_asset_cached(asset_name)
        for asset_name in skeletal_asset_names
    }

    # Parent collection is created on demand, only if we actually add objects.
    parent_collection = None

    created_count = 0
    missing_assets: set[str] = set()
    pending_parent_attaches: list[tuple[bpy.types.Object, dict, str]] = []
    pending_attaches: list[tuple[bpy.types.Object, dict, str]] = []
    pending_light_attaches: list[tuple[bpy.types.Object, dict, str]] = []
    component_objects: dict[str, bpy.types.Object] = {}
    actor_objects: dict[str, bpy.types.Object] = {}
    light_objects_by_outer: dict[str, list[bpy.types.Object]] = {}
    material_targets: dict[int, bpy.types.Object] = {}
    pending_material_parameter_lights: list[dict] = []
    pending_vector_parameters: list[tuple[int, dict]] = []
    location_scale = 0.01  # UE units -> Blender (cm -> m)

    for entry_index, entry in enumerate(data):
        if not isinstance(entry, dict):
            continue

        t = entry.get("Type")

        # ------------------------------------------------------------------ #
        # Static mesh components -> collection instances (empty fallback)
        # ------------------------------------------------------------------ #
        if t in {"EndEnvironmentStaticMeshComponent", "StaticMeshComponent"}:
            props = entry.get("Properties", {})
            static_mesh = props.get("StaticMesh", {})
            object_name_str = static_mesh.get("ObjectName")

            asset_name = extract_static_mesh_name(object_name_str)
            if not asset_name:
                print(f"[End JSON Import]   StaticMesh entry has no parseable ObjectName - skipping. (raw: {object_name_str!r})")
                continue

            rel_loc = props.get("RelativeLocation", {})
            rel_rot = props.get("RelativeRotation", {})
            loc = location_from_relative(rel_loc, scale_factor=location_scale)
            rot = rotation_from_relative(rel_rot)

            if parent_collection is None:
                parent_collection = ensure_parent_collection_for_file(filepath)

            attach_socket = props.get("AttachSocketName")
            has_attach_socket = bool(attach_socket)
            attach_name = _resolve_outer_name(entry) or asset_name
            asset = static_asset_cache.get(asset_name)
            if asset is not None:
                if asset.kind == "COLLECTION":
                    collection = asset.datablock
                    if attach_socket:
                        new_obj, instance_obj = create_wrapped_collection_instance(
                            collection=collection,
                            name=attach_name,
                            location=loc,
                            rotation_euler=rot,
                            parent_collection=parent_collection,
                        )
                        instance_obj = ensure_overrideable_collection_instance(instance_obj)
                        ensure_top_level_object_overrides(instance_obj)
                        parent_override_roots_to_wrapper(new_obj, instance_obj)
                    else:
                        new_obj = create_collection_instance(
                            collection=collection,
                            name=attach_name,
                            location=loc,
                            rotation_euler=rot,
                            parent_collection=parent_collection,
                        )
                else:
                    new_obj = create_linked_object_instance(
                        source_obj=asset.datablock,
                        name=attach_name,
                        location=loc,
                        rotation_euler=rot,
                        parent_collection=parent_collection,
                    )
            else:
                missing_assets.add(asset_name)
                placeholder_name = attach_name
                new_obj = create_mesh_empty(placeholder_name, loc, rot, parent_collection)
                print(
                    f"[End JSON Import]   StaticMesh fallback empty '{placeholder_name}' | "
                    f"loc=({loc.x:.3f},{loc.y:.3f},{loc.z:.3f})"
                )

            if attach_socket:
                pending_attaches.append((new_obj, props, attach_socket))
                print(
                    f"[End JSON Import]   Deferred attach for '{new_obj.name}' "
                    f"socket '{attach_socket}' until all file objects are imported."
                )
                attach_socket = None

            register_component_object(component_objects, entry, new_obj)
            _register_actor_object(actor_objects, attach_name, new_obj)
            register_override_material_targets(material_targets, props, new_obj)

            if props.get("AttachParent") and not has_attach_socket:
                parent_obj = resolve_attach_parent_object(
                    props,
                    component_objects,
                    attach_name,
                    report_missing=False,
                )
                if parent_obj is not None:
                    apply_deferred_object_parent(
                        new_obj,
                        props,
                        parent_obj,
                        location_scale,
                    )
                else:
                    pending_parent_attaches.append((new_obj, props, attach_name))

            # -------------------------------------------------------------- #
            # AttachSocketName -> prefer a matching socket empty under the
            # target skeletal actor, with the old bone path as fallback.
            # -------------------------------------------------------------- #
            if attach_socket and not try_attach_to_socket_empty(new_obj, props, attach_socket):
                attach_actor_name = _resolve_attach_parent_name(props)
                if attach_actor_name:
                    target_instance = bpy.data.objects.get(attach_actor_name)
                    if target_instance is None:
                        print(
                            f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                            f"parent actor '{attach_actor_name}' not found in scene - skipping bone parent."
                        )
                    else:
                        # Ensure the target collection instance has a library
                        # override so its armature is accessible.
                        target_instance = ensure_overrideable_collection_instance(target_instance)

                        # Find the armature object inside the override hierarchy.
                        armature_obj = None
                        # The override may itself be an armature, or contain one
                        # as a child.
                        if target_instance.type == "ARMATURE":
                            armature_obj = target_instance
                        else:
                            for child in target_instance.children_recursive:
                                if child.type == "ARMATURE":
                                    armature_obj = child
                                    break

                        if armature_obj is None:
                            print(
                                f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                                f"no armature found under '{target_instance.name}' - skipping bone parent."
                            )
                        else:
                            bone_name = find_bone_by_socket_name(armature_obj, attach_socket)
                            if bone_name:
                                parent_to_bone(new_obj, armature_obj, bone_name)
                            else:
                                print(
                                    f"[End JSON Import]   AttachSocketName '{attach_socket}': "
                                    f"no bone with SocketName='{attach_socket}' found on "
                                    f"'{armature_obj.name}' - skipping bone parent."
                                )
                else:
                    print(
                        f"[End JSON Import]   AttachSocketName '{attach_socket}' present but "
                        f"AttachParent could not be resolved - skipping bone parent."
                    )

            created_count += 1

        # ------------------------------------------------------------------ #
        # Skeletal mesh components -> collection instance, empty fallback
        # ------------------------------------------------------------------ #
        elif t == "EndSkeletalMeshComponent":
            if parent_collection is None:
                parent_collection = ensure_parent_collection_for_file(filepath)

            props = entry.get("Properties", {})
            loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
            rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))
            loc = location_from_relative(loc_dict, scale_factor=location_scale)
            rot = rotation_from_relative(rot_dict)

            # asset_name: bare mesh name derived from Template.ObjectPath for
            # asset library lookup (e.g. "BG0215_00_Switch_Standard").
            # template_object_path: the raw path stored as a custom property.
            asset_name, template_object_path = extract_skeletal_mesh_name(entry)

            # instance_name: use the outer actor name exactly as-is so that
            # e.g. "BG0215_00_Switch_Standard2_5" is preserved without any
            # suffix stripping or dot-conversion.
            outer_name = _resolve_outer_name(entry)
            if not outer_name:
                comp_name = entry.get("Name", "SkeletalMeshComponent")
                outer_name = comp_name if isinstance(comp_name, str) else "SkeletalMeshComponent"
            instance_name = outer_name  # no suffix modifications for skeletal meshes

            # Try to find a linked asset by the bare class name. Collections
            # remain preferred, but armature/object assets are valid too.
            asset = skeletal_asset_cache.get(asset_name) if asset_name else None

            if asset is not None:
                if asset.kind == "COLLECTION":
                    obj, instance_obj = create_skeletal_wrapper_instance(
                        collection=asset.datablock,
                        name=instance_name,
                        location=loc,
                        rotation_euler=rot,
                        parent_collection=parent_collection,
                    )
                    assign_skeletal_source_metadata(
                        obj,
                        instance_obj,
                        instance_name,
                        template_object_path,
                    )
                    instance_obj = ensure_overrideable_collection_instance(instance_obj)
                    ensure_top_level_object_overrides(instance_obj)
                    parent_override_roots_to_wrapper(obj, instance_obj)
                else:
                    obj, instance_obj = create_wrapped_linked_object_instance(
                        source_obj=asset.datablock,
                        name=instance_name,
                        location=loc,
                        rotation_euler=rot,
                        parent_collection=parent_collection,
                    )
                    assign_skeletal_source_metadata(
                        obj,
                        instance_obj,
                        instance_name,
                        template_object_path,
                    )
                print(
                    f"[End JSON Import]   SkeletalMesh wrapper '{instance_name}' "
                    f"(asset lookup: '{asset_name}', kind: {asset.kind}, "
                    f"template: '{template_object_path}') | "
                    f"loc=({loc.x:.3f},{loc.y:.3f},{loc.z:.3f})"
                )
            else:
                if asset_name:
                    missing_assets.add(asset_name)
                obj = create_mesh_empty(instance_name, loc, rot, parent_collection)
                obj["source_name"] = instance_name
                if template_object_path:
                    obj["template_object_path"] = template_object_path
                print(
                    f"[End JSON Import]   SkeletalMesh empty '{instance_name}' | "
                    f"loc=({loc.x:.3f},{loc.y:.3f},{loc.z:.3f})  "
                    f"rot=({math.degrees(rot.x):.2f},{math.degrees(rot.y):.2f},{math.degrees(rot.z):.2f}) deg"
                )

            register_component_object(component_objects, entry, obj)
            _register_actor_object(actor_objects, instance_name, obj)

            attach_socket = props.get("AttachSocketName")
            if attach_socket:
                pending_attaches.append((obj, props, attach_socket))
                print(
                    f"[End JSON Import]   Deferred attach for '{obj.name}' "
                    f"socket '{attach_socket}' until all file objects are imported."
                )
            elif props.get("AttachParent"):
                parent_obj = resolve_attach_parent_object(
                    props,
                    component_objects,
                    instance_name,
                    report_missing=False,
                )
                if parent_obj is not None:
                    apply_deferred_object_parent(
                        obj,
                        props,
                        parent_obj,
                        location_scale,
                    )
                else:
                    pending_parent_attaches.append((obj, props, instance_name))

            created_count += 1

        # ------------------------------------------------------------------ #
        # Point lights
        # ------------------------------------------------------------------ #
        elif t == "PointLightComponent":
            if parent_collection is None:
                parent_collection = ensure_parent_collection_for_file(filepath)

            props = entry.get("Properties", {})
            light_name = _resolve_light_name(entry, "PointLight")
            attach_parent_obj = resolve_attach_parent_object(
                props,
                component_objects,
                light_name,
                report_missing=False,
            )
            light_obj = create_point_light_from_entry(
                entry=entry,
                parent_collection=parent_collection,
                location_scale=location_scale,
                exposure_mult=exposure_mult,
                attenuation_radius_mult=attenuation_radius_mult,
                attach_parent_obj=attach_parent_obj,
            )
            outer_name = _resolve_outer_name(entry)
            if outer_name:
                light_objects_by_outer.setdefault(outer_name, []).append(light_obj)
            if attach_parent_obj is None and props.get("AttachParent"):
                pending_light_attaches.append((light_obj, props, light_name))
            created_count += 1

        # ------------------------------------------------------------------ #
        # Spot lights
        # ------------------------------------------------------------------ #
        elif t == "SpotLightComponent":
            if parent_collection is None:
                parent_collection = ensure_parent_collection_for_file(filepath)

            props = entry.get("Properties", {})
            light_name = _resolve_light_name(entry, "SpotLight")
            attach_parent_obj = resolve_attach_parent_object(
                props,
                component_objects,
                light_name,
                report_missing=False,
            )
            light_obj = create_spot_light_from_entry(
                entry=entry,
                parent_collection=parent_collection,
                location_scale=location_scale,
                exposure_mult=exposure_mult,
                attenuation_radius_mult=attenuation_radius_mult,
                attach_parent_obj=attach_parent_obj,
            )
            outer_name = _resolve_outer_name(entry)
            if outer_name:
                light_objects_by_outer.setdefault(outer_name, []).append(light_obj)
            if attach_parent_obj is None and props.get("AttachParent"):
                pending_light_attaches.append((light_obj, props, light_name))
            created_count += 1

        # ------------------------------------------------------------------ #
        # Material/light metadata components -> custom properties
        # ------------------------------------------------------------------ #
        elif t == "MaterialParameterLightPlacedComponent":
            pending_material_parameter_lights.append(entry)

        elif t == "MaterialInstanceDynamic":
            pending_vector_parameters.append((entry_index, entry))

        # ------------------------------------------------------------------ #
        # Streaming volumes -> recursively import referenced JSONs
        # ------------------------------------------------------------------ #
        elif t == "EndStreamingVolume":
            if not recursive_import:
                continue
            if not game_root:
                print("[End JSON Import]   EndStreamingVolume found but Game Root is not set - skipping.")
                continue

            props = entry.get("Properties", {})
            levels = props.get("StreamingLevels", [])
            print(f"[End JSON Import]   EndStreamingVolume - {len(levels)} streaming level(s) referenced.")
            for lvl in levels:
                if not isinstance(lvl, dict):
                    continue
                asset_path_name = lvl.get("AssetPathName")
                json_path = resolve_streaming_level_json_path(asset_path_name, game_root)
                if not json_path:
                    print(f"[End JSON Import]     Could not resolve path for: {asset_path_name!r}")
                    continue
                if not os.path.exists(json_path):
                    print(f"[End JSON Import]     Streaming JSON not found: {json_path}")
                    continue

                print(f"[End JSON Import]     Recursing into streaming level: {json_path}")
                child_created, child_missing = import_json_file(
                    filepath=json_path,
                    exposure_mult=exposure_mult,
                    attenuation_radius_mult=attenuation_radius_mult,
                    game_root=game_root,
                    visited_paths=visited_paths,
                    recursive_import=recursive_import,
                    import_massive_environment_umaps=import_massive_environment_umaps,
                    imported_umap_paths=imported_umap_paths,
                )
                print(f"[End JSON Import]     Streaming level done - {child_created} item(s) created, {len(child_missing)} missing.")
                created_count += child_created
                missing_assets |= child_missing

        elif t == "MassiveEnvironmentComponent":
            if import_massive_environment_umaps:
                processed, skipped, child_missing = import_massive_environment_umap(
                    entry,
                    game_root,
                    imported_umap_paths,
                )
                created_count += processed
                missing_assets |= child_missing
                if processed:
                    print(f"[End JSON Import]   MassiveEnvironment reference import done - {processed} processed, {skipped} skipped.")

        # Other types ignored

    if pending_parent_attaches:
        for child_obj, props, child_name in pending_parent_attaches:
            parent_obj = resolve_attach_parent_object(
                props,
                component_objects,
                child_name,
                report_missing=True,
            )
            if parent_obj is not None:
                apply_deferred_object_parent(
                    child_obj,
                    props,
                    parent_obj,
                    location_scale,
                )

    if pending_attaches:
        print(
            f"[End JSON Import] Processing {len(pending_attaches)} deferred attach request(s) "
            f"for '{os.path.basename(filepath)}'."
        )
        for child_obj, props, attach_socket in pending_attaches:
            process_deferred_attach(child_obj, props, attach_socket, location_scale)

    if pending_light_attaches:
        for light_obj, props, light_name in pending_light_attaches:
            parent_obj = resolve_attach_parent_object(
                props,
                component_objects,
                light_name,
                report_missing=True,
            )
            if parent_obj is not None:
                apply_deferred_light_parent(
                    light_obj,
                    props,
                    parent_obj,
                    location_scale,
                )

    for entry in pending_material_parameter_lights:
        props = entry.get("Properties", {})
        outer_name = _resolve_outer_name(entry)
        targets: list[bpy.types.Object] = []
        actor_obj = actor_objects.get(outer_name)
        if actor_obj is not None:
            targets.append(actor_obj)
        targets.extend(light_objects_by_outer.get(outer_name, []))
        for target in targets:
            _apply_material_parameter_light_props(target, props)

    for export_index, entry in pending_vector_parameters:
        target = material_targets.get(export_index)
        if target is not None:
            _apply_vector_parameter_props(target, entry.get("Properties", {}))

    # If created_count == 0 we never created parent_collection, so no empty
    # collection is left behind.
    print(
        f"[End JSON Import] Finished '{os.path.basename(filepath)}' - "
        f"{created_count} item(s) created, {len(missing_assets)} missing asset(s)."
    )
    return created_count, missing_assets
