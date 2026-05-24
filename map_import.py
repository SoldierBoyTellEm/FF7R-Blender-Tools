import bpy
import json
import os
import math
import re
import importlib
from contextlib import contextmanager as _contextmanager
from mathutils import Vector, Euler, Matrix

from . import asset_linking, lights, particles, worlds


ASSET_LIBRARY_SELECTION = asset_linking.ASSET_LIBRARY_ALL
VERBOSE_OVERRIDE_LOGGING = False
TextureIndexCache = dict[tuple[str, str], dict[str, str]]

# Blender spot lights emit along local -Z, while UE components use +X forward.
_LIGHT_FWD_FIX = (
    Matrix.Rotation(math.radians(90.0), 4, "X") @
    Matrix.Rotation(math.radians(-90.0), 4, "Y")
)
_EXTERNAL_EMISSIVE_CONTEXT_PROP = "ExternalEmissiveContext"

def extract_static_mesh_name(object_name_str: str) -> str | None:
    if not object_name_str:
        return None
    if "'" in object_name_str:
        parts = object_name_str.split("'")
        if len(parts) >= 2:
            return parts[1]
    return object_name_str


def ensure_parent_collection_for_file(filepath: str) -> bpy.types.Collection:
    base_name = os.path.splitext(os.path.basename(filepath))[0]
    col = bpy.data.collections.get(base_name)
    if col is None:
        col = bpy.data.collections.new(base_name)

    scene = bpy.context.scene
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)

    return col


def ensure_child_collection(
    parent_collection: bpy.types.Collection,
    child_name: str,
) -> bpy.types.Collection:
    col = bpy.data.collections.get(child_name)
    if col is None:
        col = bpy.data.collections.new(child_name)

    if parent_collection.children.get(col.name) is None:
        parent_collection.children.link(col)

    return col


def ensure_typed_import_collection(
    root_collection: bpy.types.Collection,
    suffix: str,
) -> bpy.types.Collection:
    return ensure_child_collection(root_collection, f"{root_collection.name}{suffix}")


def collection_is_child(
    parent_collection: bpy.types.Collection,
    child_collection: bpy.types.Collection,
) -> bool:
    return any(child == child_collection for child in parent_collection.children)


def scene_collection_parents(collection: bpy.types.Collection) -> list[bpy.types.Collection]:
    if collection is None:
        return []

    scene_root = bpy.context.scene.collection
    parents: list[bpy.types.Collection] = []
    visited: set[int] = set()

    def visit(parent: bpy.types.Collection) -> None:
        pointer = parent.as_pointer()
        if pointer in visited:
            return
        visited.add(pointer)

        for child in list(parent.children):
            if child == collection:
                parents.append(parent)
            else:
                visit(child)

    visit(scene_root)
    return parents


def unlink_collection_from_scene(collection: bpy.types.Collection) -> int:
    parents = scene_collection_parents(collection)

    unlinked_count = 0
    for parent in parents:
        try:
            parent.children.unlink(collection)
            unlinked_count += 1
        except RuntimeError as exc:
            print(
                f"[End JSON Import]   Could not unlink collection "
                f"'{collection.name}' from '{parent.name}': {exc}"
            )

    return unlinked_count


def file_collection_under_import_type(
    collection: bpy.types.Collection,
    target_collection: bpy.types.Collection,
) -> None:
    """Keep local override collections filed, but leave linked source collections scene-unlinked."""
    if collection is None or target_collection is None:
        return

    scene_root = bpy.context.scene.collection
    if collection == scene_root or collection == target_collection:
        return

    if collection.library is not None:
        if unlink_collection_from_scene(collection):
            print(
                f"[End JSON Import]   Unlinked source collection "
                f"'{collection.name}' from the scene hierarchy."
        )
        return

    if not collection_is_child(target_collection, collection):
        try:
            target_collection.children.link(collection)
        except RuntimeError as exc:
            print(
                f"[End JSON Import]   Could not file collection "
                f"'{collection.name}' under '{target_collection.name}': {exc}"
            )
            return

    unlinked_count = 0
    for parent in scene_collection_parents(collection):
        if parent == target_collection:
            continue
        try:
            parent.children.unlink(collection)
            unlinked_count += 1
        except RuntimeError as exc:
            print(
                f"[End JSON Import]   Could not remove collection "
                f"'{collection.name}' from '{parent.name}': {exc}"
            )

    if unlinked_count:
        print(
            f"[End JSON Import]   Filed override collection '{collection.name}' "
            f"under '{target_collection.name}'."
        )


@_contextmanager
def _scene_link_collection(collection: bpy.types.Collection, parent: bpy.types.Collection):
    """Temporarily link a collection under parent so override_hierarchy_create can see it, then unlink the source."""
    already_linked = collection_is_child(parent, collection)
    if not already_linked:
        try:
            parent.children.link(collection)
        except RuntimeError:
            yield
            return
    try:
        yield
    finally:
        if not already_linked and collection_is_child(parent, collection):
            try:
                parent.children.unlink(collection)
            except RuntimeError:
                pass


def collapse_outliner_collections() -> None:
    screen = getattr(bpy.context, "screen", None)
    if screen is None:
        return

    for area in screen.areas:
        if area.type != "OUTLINER":
            continue

        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        space = next((s for s in area.spaces if s.type == "OUTLINER"), None)
        if region is None or space is None:
            continue

        try:
            with bpy.context.temp_override(area=area, region=region, space_data=space):
                for _ in range(8):
                    result = bpy.ops.outliner.show_one_level(open=False)
                    if result == {"CANCELLED"}:
                        break
        except Exception as exc:
            print(f"[End JSON Import]   Could not collapse Outliner collections: {exc}")


def find_or_load_collection_cached(asset_name: str) -> bpy.types.Collection | None:
    return asset_linking.find_or_load_collection(asset_name, ASSET_LIBRARY_SELECTION)


def find_or_load_asset_cached(asset_name: str) -> asset_linking.LinkedAsset | None:
    return asset_linking.find_or_load_asset(asset_name, ASSET_LIBRARY_SELECTION)


def create_collection_instance(
    collection: bpy.types.Collection,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
    scale: Vector | None = None,
):
    obj = bpy.data.objects.new(name, None)
    obj.instance_type = "COLLECTION"
    obj.instance_collection = collection

    obj.location = location
    obj.rotation_euler = rotation_euler
    if scale is not None:
        obj.scale = scale

    parent_collection.objects.link(obj)
    return obj


def create_linked_object_instance(
    source_obj: bpy.types.Object,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
    scale: Vector | None = None,
):
    obj = source_obj.copy()
    obj.name = name
    obj.rotation_mode = "XYZ"
    obj.location = location
    obj.rotation_euler = rotation_euler
    if scale is not None:
        obj.scale = _multiply_vectors(obj.scale, scale)
    parent_collection.objects.link(obj)
    return obj


def create_wrapped_linked_object_instance(
    source_obj: bpy.types.Object,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
    scale: Vector | None = None,
):
    wrapper = create_mesh_empty(name, location, rotation_euler, parent_collection, scale=scale)

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
    scale: Vector | None = None,
):
    return create_wrapped_collection_instance(
        collection=collection,
        name=name,
        location=location,
        rotation_euler=rotation_euler,
        parent_collection=parent_collection,
        scale=scale,
    )


def create_wrapped_collection_instance(
    collection: bpy.types.Collection,
    name: str,
    location: Vector,
    rotation_euler: Euler,
    parent_collection: bpy.types.Collection,
    scale: Vector | None = None,
):
    wrapper = create_mesh_empty(name, location, rotation_euler, parent_collection, scale=scale)

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
    wrapper_obj["source_name"] = instance_name
    if template_object_path:
        wrapper_obj["template_object_path"] = template_object_path
    instance_obj["source_name"] = instance_name
    if template_object_path:
        instance_obj["template_object_path"] = template_object_path


def rotation_from_relative(rot_dict: dict) -> Euler:
    """UE RelativeRotation (Pitch/Yaw/Roll degrees) -> Blender XYZ Euler. Mapping: X=Roll, Y=-Pitch, Z=-Yaw."""
    pitch_deg = float(rot_dict.get("Pitch", 0.0))
    yaw_deg = float(rot_dict.get("Yaw", 0.0))
    roll_deg = float(rot_dict.get("Roll", 0.0))

    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)
    roll = math.radians(roll_deg)

    return Euler((roll, -pitch, -yaw), "XYZ")


def light_rotation_from_relative(rot_dict: dict) -> Euler:
    """rotation_from_relative plus the Blender spot -Z -> UE +X forward correction."""
    if not _has_rotation_values(rot_dict):
        return Euler((0.0, 0.0, 0.0), "XYZ")
    mesh_rot = rotation_from_relative(rot_dict)
    return (mesh_rot.to_matrix().to_4x4() @ _LIGHT_FWD_FIX).to_euler("XYZ")


def _has_rotation_values(rot_dict: dict) -> bool:
    """Return True when the export supplied at least one UE rotation channel."""
    if not isinstance(rot_dict, dict):
        return False
    return any(axis in rot_dict for axis in ("Pitch", "Yaw", "Roll"))


def location_from_relative(loc_dict: dict, scale_factor: float = 0.01) -> Vector:
    if not isinstance(loc_dict, dict):
        loc_dict = {}
    x = _float_or_default(loc_dict.get("X", 0.0)) * scale_factor
    y = -_float_or_default(loc_dict.get("Y", 0.0)) * scale_factor
    z = _float_or_default(loc_dict.get("Z", 0.0)) * scale_factor
    return Vector((x, y, z))


def scale_from_relative(scale_dict: dict) -> Vector:
    if not isinstance(scale_dict, dict):
        scale_dict = {}
    return Vector((
        _float_or_default(scale_dict.get("X", 1.0), 1.0),
        _float_or_default(scale_dict.get("Y", 1.0), 1.0),
        _float_or_default(scale_dict.get("Z", 1.0), 1.0),
    ))


def scale_from_entry(props: dict, entry: dict | None = None) -> Vector:
    if not isinstance(props, dict):
        props = {}
    entry_scale = entry.get("RelativeScale3D", {}) if isinstance(entry, dict) else {}
    return scale_from_relative(props.get("RelativeScale3D", entry_scale))


def _multiply_vectors(lhs, rhs) -> Vector:
    return Vector((
        _float_or_default(lhs[0], 1.0) * _float_or_default(rhs[0], 1.0),
        _float_or_default(lhs[1], 1.0) * _float_or_default(rhs[1], 1.0),
        _float_or_default(lhs[2], 1.0) * _float_or_default(rhs[2], 1.0),
    ))


def _float_or_default(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _set_custom_property(
    obj: bpy.types.ID,
    name: str,
    value,
    *,
    subtype: str | None = None,
) -> None:
    obj[name] = value
    if subtype is None:
        return
    try:
        obj.id_properties_ui(name).update(subtype=subtype)
    except Exception:
        pass


def _bool_or_default(value, default: bool = False) -> bool:
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
    props = entry.get("Properties", {})
    light_name = _resolve_light_name(entry, "PointLight")
    light_data = lights.create_static_light_data(
        light_name, "POINT", {**entry, **props},
        location_scale=location_scale,
        exposure_mult=exposure_mult,
        attenuation_radius_mult=attenuation_radius_mult,
    )
    loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
    rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))
    loc = location_from_relative(loc_dict, scale_factor=location_scale)
    rot = rotation_from_relative(rot_dict)
    scale = scale_from_entry(props, entry)
    light_obj = bpy.data.objects.new(light_name, light_data)
    if attach_parent_obj is not None:
        light_obj.parent = attach_parent_obj
    light_obj.location = loc
    light_obj.rotation_euler = rot
    light_obj.scale = scale
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
    props = entry.get("Properties", {})
    light_name = _resolve_light_name(entry, "SpotLight")
    light_data = lights.create_static_light_data(
        light_name, "SPOT", {**entry, **props},
        location_scale=location_scale,
        exposure_mult=exposure_mult,
        attenuation_radius_mult=attenuation_radius_mult,
    )
    loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
    rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))
    loc = location_from_relative(loc_dict, scale_factor=location_scale)
    rot = light_rotation_from_relative(rot_dict)
    scale = scale_from_entry(props, entry)
    light_obj = bpy.data.objects.new(light_name, light_data)
    if attach_parent_obj is not None:
        light_obj.parent = attach_parent_obj
    light_obj.location = loc
    light_obj.rotation_euler = rot
    light_obj.scale = scale
    lights.apply_ue_light_object_properties(light_obj)
    parent_collection.objects.link(light_obj)
    return light_obj


def extract_skeletal_mesh_name(entry: dict) -> tuple[str | None, str | None]:
    """Return (asset_name, template_object_path) from an EndSkeletalMeshComponent entry."""
    template = entry.get("Template", {})
    if not isinstance(template, dict):
        return None, None

    obj_path = template.get("ObjectPath", "")
    if not obj_path:
        return None, None

    last_segment = obj_path.rstrip("/").rsplit("/", 1)[-1]
    asset_name = last_segment.split(".")[0] or None

    return asset_name or None, obj_path


def _resolve_outer_name(entry: dict) -> str:
    outer_raw = entry.get("Outer", "")
    if isinstance(outer_raw, dict):
        obj_name = outer_raw.get("ObjectName", "")
        name = obj_name.rsplit(".", 1)[-1].rstrip("'").strip()
        return name
    return outer_raw.strip()


def _object_reference_keys(ref: dict) -> set[str]:
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
    """Return AttachParent lookup keys with exact UE refs first, XENGINE__ aliases as fallback."""
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
    """Return lookup aliases with the XENGINE__ actor prefix stripped."""
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
    if not key:
        return
    keys.add(key)
    keys.update(_xengine_reference_aliases(key))


def _entry_component_reference_keys(entry: dict) -> set[str]:
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


def _apply_material_parameter_light_props(
    obj: bpy.types.Object,
    props: dict,
    exposure_mult: float = 1.0,
):
    mpl = props.get("MaterialParameterLight", {})
    if obj is None or not isinstance(mpl, dict):
        return
    if "Intensity" in mpl:
        intensity = _float_or_default(mpl.get("Intensity"))
        obj["material_parameter_light_intensity"] = intensity
        light_data = getattr(obj, "data", None)
        if light_data is not None and hasattr(light_data, "energy"):
            light_data.energy = intensity * exposure_mult
    if "ColorTemperature" in mpl:
        temperature = _float_or_default(mpl.get("ColorTemperature"))
        obj["material_parameter_light_color_temperature"] = temperature
        light_data = getattr(obj, "data", None)
        if light_data is not None:
            if hasattr(light_data, "use_temperature"):
                light_data.use_temperature = True
            if hasattr(light_data, "temperature"):
                light_data.temperature = temperature
    color_value, hex_value = _color_prop_values(mpl.get("Color", {}))
    if color_value is not None:
        obj["material_parameter_light_color"] = color_value
        light_data = getattr(obj, "data", None)
        if light_data is not None and hasattr(light_data, "color") and len(color_value) >= 3:
            light_data.color = (
                max(0.0, min(1.0, color_value[0] / 255.0)),
                max(0.0, min(1.0, color_value[1] / 255.0)),
                max(0.0, min(1.0, color_value[2] / 255.0)),
            )
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
        prop_prefix = name if name == _EXTERNAL_EMISSIVE_CONTEXT_PROP else f"material_vector_{name}"
        if color_value is not None:
            subtype = "COLOR" if name == _EXTERNAL_EMISSIVE_CONTEXT_PROP else None
            _set_custom_property(obj, prop_prefix, color_value, subtype=subtype)
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
    """Convert UE duplicate-suffix ``_N`` to Blender dot-suffix ``.N`` (e.g. ``Mesh_2`` -> ``Mesh.2``)."""
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
    scale: Vector | None = None,
):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 1.0
    obj.location = loc
    obj.rotation_euler = rot
    if scale is not None:
        obj.scale = scale
    parent_collection.objects.link(obj)
    return obj


def resolve_game_asset_file_path(asset_path_name: str, game_root: str, extension: str) -> str | None:
    """Map a /Game/... asset path to a local file path under game_root."""
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
    return resolve_game_asset_file_path(asset_path_name, game_root, ".json")


def is_path_in_or_beneath(path: str, root_dir: str) -> bool:
    """Return True when *path* is inside *root_dir* or is *root_dir* itself."""
    if not path or not root_dir:
        return True

    try:
        resolved_path = os.path.normcase(os.path.realpath(path))
        resolved_root = os.path.normcase(os.path.realpath(root_dir))
        return os.path.commonpath([resolved_path, resolved_root]) == resolved_root
    except (OSError, ValueError):
        return False


def collect_level_streaming_asset_paths(data: list) -> list[str]:
    """Return asset paths from LevelStreamingAlwaysLoaded/Dynamic entries (preferred over EndStreamingVolume)."""
    asset_paths: list[str] = []
    seen: set[str] = set()

    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("Type") not in {"LevelStreamingAlwaysLoaded", "LevelStreamingDynamic"}:
            continue

        props = entry.get("Properties", {})
        if not isinstance(props, dict):
            continue
        world_asset = props.get("WorldAsset")
        if not isinstance(world_asset, dict):
            continue

        asset_path_name = world_asset.get("AssetPathName")
        if not asset_path_name or asset_path_name in seen:
            continue
        seen.add(asset_path_name)
        asset_paths.append(asset_path_name)

    return asset_paths


def collect_volume_streaming_asset_paths(data: list) -> list[str]:
    asset_paths: list[str] = []
    seen: set[str] = set()

    for entry in data:
        if not isinstance(entry, dict) or entry.get("Type") != "EndStreamingVolume":
            continue

        props = entry.get("Properties", {})
        if not isinstance(props, dict):
            continue
        levels = props.get("StreamingLevels", [])
        if not isinstance(levels, list):
            continue

        for lvl in levels:
            if not isinstance(lvl, dict):
                continue
            asset_path_name = lvl.get("AssetPathName")
            if not asset_path_name or asset_path_name in seen:
                continue
            seen.add(asset_path_name)
            asset_paths.append(asset_path_name)

    return asset_paths


def import_streaming_asset_paths(
    asset_paths: list[str],
    source_label: str,
    game_root: str,
    exposure_mult: float,
    attenuation_radius_mult: float,
    location_scale: float,
    visited_paths: set[str],
    recursive_import: bool,
    allow_external_recursive_json: bool,
    recursive_root_dir: str,
    import_massive_environment_umaps: bool,
    imported_umap_paths: set[str],
    imported_world_sky_paths: set[str],
    texture_index_cache: TextureIndexCache | None = None,
    offset_mec_opposite_faces: bool = False,
) -> tuple[int, set[str]]:
    """Resolve streaming asset paths to JSON files and import them."""
    created_count = 0
    missing_assets: set[str] = set()

    print(f"[End JSON Import]   {source_label} - {len(asset_paths)} streaming level(s) referenced.")
    for asset_path_name in asset_paths:
        json_path = resolve_streaming_level_json_path(asset_path_name, game_root)
        if not json_path:
            print(f"[End JSON Import]     Could not resolve path for: {asset_path_name!r}")
            continue
        if (
            not allow_external_recursive_json
            and not is_path_in_or_beneath(json_path, recursive_root_dir)
        ):
            print(
                f"[End JSON Import]     Skipping external streaming JSON outside "
                f"'{recursive_root_dir}': {json_path}"
            )
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
            location_scale=location_scale,
            visited_paths=visited_paths,
            recursive_import=recursive_import,
            allow_external_recursive_json=allow_external_recursive_json,
            recursive_root_dir=recursive_root_dir,
            import_massive_environment_umaps=import_massive_environment_umaps,
            imported_umap_paths=imported_umap_paths,
            imported_world_sky_paths=imported_world_sky_paths,
            texture_index_cache=texture_index_cache,
            offset_mec_opposite_faces=offset_mec_opposite_faces,
        )
        print(f"[End JSON Import]     Streaming level done - {child_created} item(s) created, {len(child_missing)} missing.")
        created_count += child_created
        missing_assets |= child_missing

    return created_count, missing_assets


def resolve_massive_environment_umap_path(entry: dict, game_root: str) -> str | None:
    """Resolve a MassiveEnvironmentComponent StreamingProxy ObjectPath to a local .umap path."""
    props = entry.get("Properties", {})
    if not isinstance(props, dict):
        return None

    streaming_proxy = props.get("StreamingProxy")
    if not isinstance(streaming_proxy, dict):
        return None

    asset_path_name = streaming_proxy.get("ObjectPath")
    return resolve_game_asset_file_path(asset_path_name, game_root, ".umap")


def get_umap_import_function():
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
    texture_index_cache: TextureIndexCache | None = None,
    offset_opposite_faces: bool = False,
    scale_factor: float = 0.01,
) -> tuple[int, int, set[str]]:
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
            scale_factor=scale_factor,
            texture_index_cache=texture_index_cache,
            offset_opposite_faces=offset_opposite_faces,
        )
        return processed, skipped, set()
    except Exception as exc:
        print(f"[End JSON Import]   MassiveEnvironment .umap import failed for {umap_path}: {exc}")
        return 0, 1, set()


def find_bone_by_socket_name(armature_obj: bpy.types.Object, socket_name: str):
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return None
    for bone in armature_obj.pose.bones:
        if bone.get("SocketName") == socket_name:
            return bone.name
    return None


def make_library_override_for_instance(instance_obj: bpy.types.Object) -> bpy.types.Object | None:
    """Create a library content override for a linked collection instance (equivalent to Library Override > Make > Content)."""
    col = instance_obj.instance_collection
    if col is None or col.library is None:
        return instance_obj

    try:
        scene = bpy.context.scene
        view_layer = bpy.context.view_layer
        override_col = col.override_hierarchy_create(scene=scene, view_layer=view_layer)

        if override_col is not None:
            instance_obj.instance_collection = override_col
            view_layer.update()
            print(f"[End JSON Import]   Library override created for '{instance_obj.name}' -> '{override_col.name}'.")
        else:
            print(f"[End JSON Import]   override_hierarchy_create returned None for '{instance_obj.name}'.")

        return instance_obj

    except Exception as exc:
        print(f"[End JSON Import]   Could not make library override for '{instance_obj.name}': {exc}")
        return instance_obj


def ensure_overrideable_collection_instance(
    instance_obj: bpy.types.Object,
    parent_collection: bpy.types.Collection | None = None,
) -> bpy.types.Object | None:
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
        if active_collection is not None and parent_collection is not None:
            file_collection_under_import_type(active_collection, parent_collection)

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
    """Force local overrides for root objects in an overridden collection (needed for Blender 5 constraint targets)."""
    if instance_obj is None:
        return

    override_collection = instance_obj.instance_collection
    if override_collection is None or override_collection.library is not None:
        return

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    overridden_count = 0
    failed_names: list[str] = []

    for obj in list(override_collection.objects):
        if obj.parent is not None or obj.override_library is not None or obj.library is None:
            continue

        override_obj = None
        try:
            override_obj = obj.override_create(remap_local_usages=True)
        except Exception:
            pass

        if override_obj is None:
            try:
                override_obj = obj.override_hierarchy_create(scene=scene, view_layer=view_layer)
            except Exception:
                pass

        if isinstance(override_obj, bpy.types.Object):
            overridden_count += 1
        else:
            failed_names.append(obj.name)

    if overridden_count:
        view_layer.update()
        print(f"[End JSON Import]   Forced {overridden_count} top-level object override(s) in '{override_collection.name}'.")

    if failed_names:
        preview = ", ".join(failed_names[:5])
        suffix = "" if len(failed_names) <= 5 else f", +{len(failed_names) - 5} more"
        print(f"[End JSON Import]   Failed to override {len(failed_names)} object(s) in '{override_collection.name}': {preview}{suffix}.")


def parent_override_roots_to_wrapper(
    wrapper_obj: bpy.types.Object,
    instance_obj: bpy.types.Object,
):
    if wrapper_obj is None or instance_obj is None:
        return

    override_collection = instance_obj.instance_collection
    if override_collection is None or override_collection.library is not None:
        return

    reparented_count = 0
    for obj in override_collection.objects:
        if obj == wrapper_obj or obj == instance_obj or obj.parent is not None:
            continue
        obj.parent = wrapper_obj
        obj.location = Vector((0.0, 0.0, 0.0))
        obj.rotation_euler = Euler((0.0, 0.0, 0.0), "XYZ")
        obj.scale = Vector((1.0, 1.0, 1.0))
        reparented_count += 1

    if reparented_count:
        print(f"[End JSON Import]   Parented {reparented_count} override object(s) under '{wrapper_obj.name}'.")

    bpy.data.objects.remove(instance_obj, do_unlink=True)


def _strip_blender_numeric_suffix(name: str) -> str:
    """Return *name* without Blender's duplicate suffix, e.g. '.001'."""
    if not isinstance(name, str):
        return ""
    return re.sub(r"\.\d{3}$", "", name)


def find_attach_socket_empty(
    actor_obj: bpy.types.Object,
    socket_name: str,
) -> bpy.types.Object | None:
    if actor_obj is None or not socket_name:
        return None
    for child in actor_obj.children_recursive:
        child_name = _strip_blender_numeric_suffix(child.name)
        if child.type == "EMPTY" and child_name.endswith(socket_name):
            return child
    return None


def add_child_of_constraint(
    child_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    socket_name: str,
):
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


def try_attach_to_socket_empty(
    child_obj: bpy.types.Object,
    props: dict,
    socket_name: str,
) -> bool:
    attach_actor_name = _resolve_attach_parent_name(props)
    if not attach_actor_name:
        return False

    target_actor = bpy.data.objects.get(attach_actor_name)
    if target_actor is None:
        return False

    ensure_overrideable_collection_instance(target_actor)
    socket_obj = find_attach_socket_empty(target_actor, socket_name)
    if socket_obj is None:
        return False

    add_child_of_constraint(child_obj, socket_obj, socket_name)
    return True


def _resolve_attach_parent_name(props: dict) -> str | None:
    attach_parent = props.get("AttachParent", {})
    if not isinstance(attach_parent, dict):
        return None
    obj_name = attach_parent.get("ObjectName", "")
    if not obj_name:
        return None
    if "PersistentLevel." in obj_name:
        after = obj_name.split("PersistentLevel.", 1)[1]
        return after.split(".")[0].rstrip("'") or None
    return obj_name.rsplit(".", 1)[-1].strip("'") or None


def parent_to_bone(
    child_obj: bpy.types.Object,
    armature_obj: bpy.types.Object,
    bone_name: str,
):
    child_obj.parent = armature_obj
    child_obj.parent_type = "BONE"
    child_obj.parent_bone = bone_name


def process_deferred_attach(
    child_obj: bpy.types.Object,
    props: dict,
    attach_socket: str,
    location_scale: float,
):
    if try_attach_to_socket_empty(child_obj, props, attach_socket):
        return

    attach_actor_name = _resolve_attach_parent_name(props)
    if not attach_actor_name:
        return

    target_instance = bpy.data.objects.get(attach_actor_name)
    if target_instance is None:
        print(f"[End JSON Import]   AttachSocketName '{attach_socket}': parent '{attach_actor_name}' not found.")
        return

    target_instance = ensure_overrideable_collection_instance(target_instance)

    armature_obj = None
    if target_instance.type == "ARMATURE":
        armature_obj = target_instance
    else:
        for child in target_instance.children_recursive:
            if child.type == "ARMATURE":
                armature_obj = child
                break

    if armature_obj is None:
        apply_deferred_object_parent(child_obj, props, target_instance, location_scale)
        return

    bone_name = find_bone_by_socket_name(armature_obj, attach_socket)
    if bone_name:
        parent_to_bone(child_obj, armature_obj, bone_name)
    else:
        apply_deferred_object_parent(child_obj, props, target_instance, location_scale)


def collect_asset_names_for_import(data: list) -> tuple[set[str], set[str]]:
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
    allow_external_recursive_json: bool = False,
    recursive_root_dir: str | None = None,
    import_massive_environment_umaps: bool = True,
    imported_umap_paths: set[str] | None = None,
    imported_world_sky_paths: set[str] | None = None,
    texture_index_cache: TextureIndexCache | None = None,
    offset_mec_opposite_faces: bool = False,
    location_scale: float = 0.01,
) -> tuple[int, set[str]]:
    filepath = os.path.realpath(filepath)
    if recursive_root_dir is None:
        recursive_root_dir = os.path.dirname(filepath)
    else:
        recursive_root_dir = os.path.realpath(recursive_root_dir)

    if filepath in visited_paths:
        print(f"[End JSON Import] Skipping already-visited file: {filepath}")
        return 0, set()
    visited_paths.add(filepath)
    if imported_umap_paths is None:
        imported_umap_paths = set()
    if imported_world_sky_paths is None:
        imported_world_sky_paths = set()
    if texture_index_cache is None:
        texture_index_cache = {}
    location_scale = _float_or_default(location_scale, 0.01)

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

    parent_collection = None

    def ensure_root_collection() -> bpy.types.Collection:
        nonlocal parent_collection
        if parent_collection is None:
            parent_collection = ensure_parent_collection_for_file(filepath)
        return parent_collection

    def ensure_static_collection() -> bpy.types.Collection:
        return ensure_typed_import_collection(ensure_root_collection(), "_Static")

    def ensure_skeletal_collection() -> bpy.types.Collection:
        return ensure_typed_import_collection(ensure_root_collection(), "_Skeletal")

    def ensure_lights_collection() -> bpy.types.Collection:
        return ensure_typed_import_collection(ensure_root_collection(), "_Lights")

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
    created_count += particles.create_niagara_empties_from_effect_json(
        data,
        filepath,
        location_scale=location_scale,
    )
    if worlds.create_world_from_level_data(data, filepath):
        created_count += 1
    created_count += worlds.create_finite_fog_volume_from_level_data(
        data,
        filepath,
        location_scale=location_scale,
    )
    created_count += worlds.create_reflection_capture_probes(
        data,
        filepath,
        location_scale=location_scale,
    )

    level_streaming_asset_paths = collect_level_streaming_asset_paths(data)
    use_volume_streaming_fallback = not level_streaming_asset_paths
    if recursive_import:
        world_sky_result = worlds.import_world_sky_references(
            data=data,
            game_root=game_root,
            imported_world_sky_paths=imported_world_sky_paths,
            location_scale=location_scale,
        )
        created_count += (
            world_sky_result.worlds_created
            + world_sky_result.fog_volumes_created
            + world_sky_result.probes_created
        )
        if not game_root:
            if level_streaming_asset_paths:
                print("[End JSON Import]   LevelStreaming entries found but Game Root is not set - skipping.")
        elif level_streaming_asset_paths:
            child_created, child_missing = import_streaming_asset_paths(
                asset_paths=level_streaming_asset_paths,
                source_label="LevelStreaming",
                game_root=game_root,
                exposure_mult=exposure_mult,
                attenuation_radius_mult=attenuation_radius_mult,
                location_scale=location_scale,
                visited_paths=visited_paths,
                recursive_import=recursive_import,
                allow_external_recursive_json=allow_external_recursive_json,
                recursive_root_dir=recursive_root_dir,
                import_massive_environment_umaps=import_massive_environment_umaps,
                imported_umap_paths=imported_umap_paths,
                imported_world_sky_paths=imported_world_sky_paths,
                texture_index_cache=texture_index_cache,
                offset_mec_opposite_faces=offset_mec_opposite_faces,
            )
            created_count += child_created
            missing_assets |= child_missing

    for entry_index, entry in enumerate(data):
        if not isinstance(entry, dict):
            continue

        t = entry.get("Type")

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
            rel_scale = scale_from_entry(props, entry)
            loc = location_from_relative(rel_loc, scale_factor=location_scale)
            rot = rotation_from_relative(rel_rot)

            static_collection = ensure_static_collection()

            attach_socket = props.get("AttachSocketName")
            has_attach_socket = bool(attach_socket)
            attach_name = _resolve_outer_name(entry) or asset_name
            asset = static_asset_cache.get(asset_name)
            if asset is not None:
                if asset.kind == "COLLECTION":
                    collection = asset.datablock
                    file_collection_under_import_type(collection, static_collection)
                    if attach_socket:
                        with _scene_link_collection(collection, static_collection):
                            new_obj, instance_obj = create_wrapped_collection_instance(
                                collection=collection,
                                name=attach_name,
                                location=loc,
                                rotation_euler=rot,
                                parent_collection=static_collection,
                                scale=rel_scale,
                            )
                            instance_obj = ensure_overrideable_collection_instance(
                                instance_obj,
                                static_collection,
                            )
                        ensure_top_level_object_overrides(instance_obj)
                        parent_override_roots_to_wrapper(new_obj, instance_obj)
                    else:
                        new_obj = create_collection_instance(
                            collection=collection,
                            name=attach_name,
                            location=loc,
                            rotation_euler=rot,
                            parent_collection=static_collection,
                            scale=rel_scale,
                        )
                else:
                    new_obj = create_linked_object_instance(
                        source_obj=asset.datablock,
                        name=attach_name,
                        location=loc,
                        rotation_euler=rot,
                        parent_collection=static_collection,
                        scale=rel_scale,
                    )
            else:
                missing_assets.add(asset_name)
                placeholder_name = attach_name
                new_obj = create_mesh_empty(placeholder_name, loc, rot, static_collection, scale=rel_scale)
                print(
                    f"[End JSON Import]   StaticMesh fallback empty '{placeholder_name}' | "
                    f"loc=({loc.x:.3f},{loc.y:.3f},{loc.z:.3f})"
                )

            if attach_socket:
                pending_attaches.append((new_obj, props, attach_socket))
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
            created_count += 1

        elif t == "EndSkeletalMeshComponent":
            skeletal_collection = ensure_skeletal_collection()

            props = entry.get("Properties", {})
            loc_dict = props.get("RelativeLocation", entry.get("RelativeLocation", {}))
            rot_dict = props.get("RelativeRotation", entry.get("RelativeRotation", {}))
            rel_scale = scale_from_entry(props, entry)
            loc = location_from_relative(loc_dict, scale_factor=location_scale)
            rot = rotation_from_relative(rot_dict)

            asset_name, template_object_path = extract_skeletal_mesh_name(entry)

            outer_name = _resolve_outer_name(entry)
            if not outer_name:
                comp_name = entry.get("Name", "SkeletalMeshComponent")
                outer_name = comp_name if isinstance(comp_name, str) else "SkeletalMeshComponent"
            instance_name = outer_name

            asset = skeletal_asset_cache.get(asset_name) if asset_name else None

            if asset is not None:
                if asset.kind == "COLLECTION":
                    collection = asset.datablock
                    file_collection_under_import_type(collection, skeletal_collection)
                    with _scene_link_collection(collection, skeletal_collection):
                        obj, instance_obj = create_skeletal_wrapper_instance(
                            collection=collection,
                            name=instance_name,
                            location=loc,
                            rotation_euler=rot,
                            parent_collection=skeletal_collection,
                            scale=rel_scale,
                        )
                        assign_skeletal_source_metadata(
                            obj,
                            instance_obj,
                            instance_name,
                            template_object_path,
                        )
                        instance_obj = ensure_overrideable_collection_instance(
                            instance_obj,
                            skeletal_collection,
                        )
                    ensure_top_level_object_overrides(instance_obj)
                    parent_override_roots_to_wrapper(obj, instance_obj)
                else:
                    obj, instance_obj = create_wrapped_linked_object_instance(
                        source_obj=asset.datablock,
                        name=instance_name,
                        location=loc,
                        rotation_euler=rot,
                        parent_collection=skeletal_collection,
                        scale=rel_scale,
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
                obj = create_mesh_empty(instance_name, loc, rot, skeletal_collection, scale=rel_scale)
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

        elif t == "PointLightComponent":
            lights_collection = ensure_lights_collection()

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
                parent_collection=lights_collection,
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

        elif t == "SpotLightComponent":
            lights_collection = ensure_lights_collection()

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
                parent_collection=lights_collection,
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

        elif t == "MaterialParameterLightPlacedComponent":
            pending_material_parameter_lights.append(entry)

        elif t == "MaterialInstanceDynamic":
            pending_vector_parameters.append((entry_index, entry))

        elif t == "EndStreamingVolume":
            if not use_volume_streaming_fallback:
                continue
            if not recursive_import:
                continue
            if not game_root:
                print("[End JSON Import]   EndStreamingVolume found but Game Root is not set - skipping.")
                continue

            volume_asset_paths = collect_volume_streaming_asset_paths([entry])
            if not volume_asset_paths:
                continue
            child_created, child_missing = import_streaming_asset_paths(
                asset_paths=volume_asset_paths,
                source_label="EndStreamingVolume",
                game_root=game_root,
                exposure_mult=exposure_mult,
                attenuation_radius_mult=attenuation_radius_mult,
                location_scale=location_scale,
                visited_paths=visited_paths,
                recursive_import=recursive_import,
                allow_external_recursive_json=allow_external_recursive_json,
                recursive_root_dir=recursive_root_dir,
                import_massive_environment_umaps=import_massive_environment_umaps,
                imported_umap_paths=imported_umap_paths,
                imported_world_sky_paths=imported_world_sky_paths,
                texture_index_cache=texture_index_cache,
                offset_mec_opposite_faces=offset_mec_opposite_faces,
            )
            created_count += child_created
            missing_assets |= child_missing

        elif t == "MassiveEnvironmentComponent":
            if import_massive_environment_umaps:
                processed, skipped, child_missing = import_massive_environment_umap(
                    entry,
                    game_root,
                    imported_umap_paths,
                    texture_index_cache,
                    offset_opposite_faces=offset_mec_opposite_faces,
                    scale_factor=location_scale,
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
            _apply_material_parameter_light_props(target, props, exposure_mult=exposure_mult)

    for export_index, entry in pending_vector_parameters:
        target = material_targets.get(export_index)
        if target is not None:
            _apply_vector_parameter_props(target, entry.get("Properties", {}))

    print(
        f"[End JSON Import] Finished '{os.path.basename(filepath)}' - "
        f"{created_count} item(s) created, {len(missing_assets)} missing asset(s)."
    )
    return created_count, missing_assets
