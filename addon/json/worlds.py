"""World and reflection-probe helpers for FF7R JSON imports."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re

import bpy
from mathutils import Vector


SKY_CUBE_FACE_COUNT = 6
SKY_CUBE_FACE_NAMES = (
    "PositiveX",
    "NegativeX",
    "NegativeY",
    "PositiveY",
    "PositiveZ",
    "NegativeZ",
)
SKY_CUBE_FALLBACK_EXTENSIONS = ("hdr", "exr", "dds", "png", "tga", "jpg", "jpeg")
DEFAULT_LOCATION_SCALE = 0.01
MIN_FINITE_FOG_EXTENT = 500.0
MAX_FINITE_FOG_EXTENT = 50000.0
DEFAULT_FINITE_FOG_EXTENT = 10000.0
DEFAULT_FOG_ANISOTROPY = -0.5
FINITE_FOG_CUBE_VERSION = 2


@dataclass
class WorldSkyAssetReference:
    asset_path_name: str
    switch_labels: list[str] = field(default_factory=list)
    volume_names: list[str] = field(default_factory=list)
    is_default: bool = False


@dataclass
class WorldSkyImportResult:
    worlds_created: int = 0
    fog_volumes_created: int = 0
    probes_created: int = 0
    missing_json_paths: set[str] = field(default_factory=set)


def _addon_package_name() -> str:
    return (__package__ or "").split(".", 1)[0]


def _get_addon_preferences():
    addon_name = _addon_package_name()
    addon = bpy.context.preferences.addons.get(addon_name) if addon_name else None
    return addon.preferences if addon else None


def _get_texture_settings() -> tuple[str, str]:
    prefs = _get_addon_preferences()
    if prefs is None:
        return "", "dds"
    return (
        bpy.path.abspath(prefs.game_texture_root) if prefs.game_texture_root else "",
        (prefs.texture_extension or "dds").lstrip("."),
    )


def _float_or_default(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strip_ue_suffix(game_path: str) -> str:
    if not isinstance(game_path, str):
        return ""
    return game_path.split(".", 1)[0]


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
    return os.path.join(game_root, *rel_path.split("/"))


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


def location_from_relative(loc_dict: dict, scale_factor: float = 0.01) -> Vector:
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


def _srgb_channel_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _hex_color_to_blender_rgba(hex_value: str, default=None) -> tuple[float, float, float, float] | None:
    if not isinstance(hex_value, str):
        return default

    cleaned = hex_value.strip().lstrip("#")
    if len(cleaned) not in {6, 8} or not re.fullmatch(r"[0-9a-fA-F]+", cleaned):
        return default

    r = _srgb_channel_to_linear(int(cleaned[0:2], 16) / 255.0)
    g = _srgb_channel_to_linear(int(cleaned[2:4], 16) / 255.0)
    b = _srgb_channel_to_linear(int(cleaned[4:6], 16) / 255.0)
    a = int(cleaned[6:8], 16) / 255.0 if len(cleaned) == 8 else 1.0
    return (r, g, b, a)


def color_from_ue_linear_or_srgb(color: dict, default=(1.0, 1.0, 1.0, 1.0)) -> tuple[float, float, float, float]:
    if not isinstance(color, dict):
        return default

    hex_color = _hex_color_to_blender_rgba(color.get("Hex", ""), default=None)
    if hex_color is not None:
        return hex_color

    return (
        _float_or_default(color.get("R", default[0] * 255.0), default[0] * 255.0) / 255.0,
        _float_or_default(color.get("G", default[1] * 255.0), default[1] * 255.0) / 255.0,
        _float_or_default(color.get("B", default[2] * 255.0), default[2] * 255.0) / 255.0,
        _float_or_default(color.get("A", default[3] * 255.0), default[3] * 255.0) / 255.0,
    )


def _fog_anisotropy(_fog_props: dict) -> float:
    return DEFAULT_FOG_ANISOTROPY


def collect_world_sky_asset_references(data: list) -> list[WorldSkyAssetReference]:
    """Collect unique streaming assets from EndStreamingSwitchVolume entries labeled WorldSky."""
    refs_by_asset: dict[str, WorldSkyAssetReference] = {}

    def add_ref(
        asset_path_name: str,
        switch_label: str,
        volume_name: str,
        is_default: bool = False,
    ) -> None:
        if not asset_path_name:
            return
        ref = refs_by_asset.get(asset_path_name)
        if ref is None:
            ref = WorldSkyAssetReference(asset_path_name=asset_path_name)
            refs_by_asset[asset_path_name] = ref
        if switch_label and switch_label not in ref.switch_labels:
            ref.switch_labels.append(switch_label)
        if volume_name and volume_name not in ref.volume_names:
            ref.volume_names.append(volume_name)
        ref.is_default = ref.is_default or is_default

    for entry in data:
        if not isinstance(entry, dict) or entry.get("Type") != "EndStreamingSwitchVolume":
            continue
        props = entry.get("Properties", {})
        if not isinstance(props, dict) or props.get("AreaSwitchLabel") != "WorldSky":
            continue

        volume_name = entry.get("Name", "EndStreamingSwitchVolume")
        default_label = props.get("NoneSwitchLabel") or props.get("VolumeLabel") or "WorldSky_Default"
        for lvl in props.get("StreamingLevels", []) or []:
            if isinstance(lvl, dict):
                add_ref(
                    lvl.get("AssetPathName", ""),
                    str(default_label),
                    str(volume_name),
                    is_default=True,
                )

        for switch_set in props.get("SwitchLevelSets", []) or []:
            if not isinstance(switch_set, dict):
                continue
            switch_label = switch_set.get("SwitchLabel") or "WorldSky"
            for lvl in switch_set.get("StreamingLevels", []) or []:
                if isinstance(lvl, dict):
                    add_ref(
                        lvl.get("AssetPathName", ""),
                        str(switch_label),
                        str(volume_name),
                        is_default=False,
                    )

    return list(refs_by_asset.values())


def _load_json_list(filepath: str) -> list | None:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[End JSON Import]   Could not read WorldSky JSON '{filepath}': {exc}")
        return None

    if not isinstance(data, list):
        print(f"[End JSON Import]   WorldSky JSON root is not a list: {filepath}")
        return None
    return data


def _find_height_fog_with_distant_view(data: list) -> dict | None:
    fallback = None
    for entry in data:
        if not isinstance(entry, dict) or entry.get("Type") != "ExponentialHeightFogComponent":
            continue
        props = entry.get("Properties", {})
        if not isinstance(props, dict):
            continue
        if fallback is None:
            fallback = entry
        distant_view = props.get("DistantViewEnvironment", {})
        texture = distant_view.get("Texture", {}) if isinstance(distant_view, dict) else {}
        if isinstance(texture, dict) and texture.get("ObjectName"):
            return entry
    return fallback


def _distant_view_folder(distant_view: dict, texture_root: str) -> str | None:
    if not isinstance(distant_view, dict) or not texture_root:
        return None

    texture = distant_view.get("Texture", {})
    if not isinstance(texture, dict):
        return None

    object_path = texture.get("ObjectPath", "")
    object_path = _strip_ue_suffix(object_path)
    if not object_path:
        return None
    if object_path.lower().startswith("/game/"):
        object_path = object_path[6:]
    built_data_dir = os.path.join(texture_root, *object_path.split("/"))

    distant_view_dir = ""
    object_name = texture.get("ObjectName", "")
    match = re.search(r"'([^']+)'", object_name) if isinstance(object_name, str) else None
    if match:
        inner_name = match.group(1)
        if ":" in inner_name:
            distant_view_dir = inner_name.split(":", 1)[1]

    if not distant_view_dir:
        build_data_id = distant_view.get("BuildDataId", "")
        if isinstance(build_data_id, str) and build_data_id:
            distant_view_dir = f"DistantView_{build_data_id.replace('-', '')}"

    if not distant_view_dir:
        return None
    return os.path.join(built_data_dir, distant_view_dir)


def _unique_extensions(preferred_extension: str) -> list[str]:
    extensions: list[str] = []
    for ext in (preferred_extension, *SKY_CUBE_FALLBACK_EXTENSIONS):
        ext = (ext or "").lstrip(".").lower()
        if ext and ext not in extensions:
            extensions.append(ext)
    return extensions


def _find_cube_side_paths(face_dir: str, preferred_extension: str) -> dict[int, str]:
    side_paths: dict[int, str] = {}
    if not face_dir or not os.path.isdir(face_dir):
        return side_paths

    files_by_lower = {name.lower(): name for name in os.listdir(face_dir)}
    extensions = _unique_extensions(preferred_extension)
    for side_index in range(SKY_CUBE_FACE_COUNT):
        stem = f"side_{side_index}"
        for ext in extensions:
            filename = f"{stem}.{ext}"
            actual_name = files_by_lower.get(filename)
            if actual_name:
                side_paths[side_index] = os.path.join(face_dir, actual_name)
                break
    return side_paths


def _load_image(filepath: str, datablock_prefix: str) -> bpy.types.Image | None:
    try:
        try:
            image = bpy.data.images.load(filepath, check_existing=True)
        except TypeError:
            image = bpy.data.images.load(filepath)
    except Exception as exc:
        print(f"[End JSON Import]   Could not load skybox texture '{filepath}': {exc}")
        return None

    if image is not None and os.path.basename(filepath) == image.name:
        try:
            image.name = f"{datablock_prefix}_{os.path.basename(filepath)}"
        except Exception:
            pass
    return image


def _set_node_input(node, input_names: tuple[str, ...], value) -> None:
    for input_name in input_names:
        socket = node.inputs.get(input_name)
        if socket is not None:
            socket.default_value = value
            return


def _node_input(node, *input_names: str):
    for input_name in input_names:
        socket = node.inputs.get(input_name)
        if socket is not None:
            return socket
    return None


def _node_output(node, *output_names: str):
    for output_name in output_names:
        socket = node.outputs.get(output_name)
        if socket is not None:
            return socket
    return None


def _new_math(nodes, operation: str, label: str, location: tuple[float, float]):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.label = label
    node.location = location
    return node


def _component_socket(separate_node, axis: str):
    socket = separate_node.outputs.get(axis)
    if socket is not None:
        return socket
    return separate_node.outputs[{"X": 0, "Y": 1, "Z": 2}[axis]]


def _negate_socket(nodes, links, value_socket, location: tuple[float, float], label: str):
    multiply = _new_math(nodes, "MULTIPLY", label, location)
    multiply.inputs[1].default_value = -1.0
    links.new(value_socket, multiply.inputs[0])
    return multiply.outputs[0]


def _normalize_to_uv(nodes, links, numerator_socket, denominator_socket, location: tuple[float, float], label: str):
    divide = _new_math(nodes, "DIVIDE", f"{label} divide", location)
    links.new(numerator_socket, divide.inputs[0])
    links.new(denominator_socket, divide.inputs[1])

    add = _new_math(nodes, "ADD", f"{label} + 1", (location[0] + 180.0, location[1]))
    add.inputs[1].default_value = 1.0
    links.new(divide.outputs[0], add.inputs[0])

    scale = _new_math(nodes, "MULTIPLY", f"{label} * 0.5", (location[0] + 360.0, location[1]))
    scale.inputs[1].default_value = 0.5
    links.new(add.outputs[0], scale.inputs[0])
    return scale.outputs[0]


def _face_axis_data(face_index: int) -> tuple[str, str, tuple[str, float], tuple[str, float]]:
    """Return major axis, sign op, and (axis, sign) pairs for U/V numerator sockets."""
    return (
        ("X", "GREATER_THAN", ("Z", -1.0), ("Y", -1.0)),
        ("X", "LESS_THAN", ("Z", 1.0), ("Y", -1.0)),
        ("Y", "LESS_THAN", ("X", 1.0), ("Z", 1.0)),
        ("Y", "GREATER_THAN", ("X", 1.0), ("Z", -1.0)),
        ("Z", "GREATER_THAN", ("X", 1.0), ("Y", -1.0)),
        ("Z", "LESS_THAN", ("X", -1.0), ("Y", -1.0)),
    )[face_index]


def _make_color_mix(nodes, links, current_color, next_color, factor_socket, location: tuple[float, float]):
    try:
        mix = nodes.new("ShaderNodeMix")
        mix.location = location
        mix.data_type = "RGBA"
        if hasattr(mix, "factor_mode"):
            mix.factor_mode = "UNIFORM"
        factor_input = mix.inputs.get("Factor") or mix.inputs[0]
        a_input = mix.inputs.get("A") or mix.inputs[6]
        b_input = mix.inputs.get("B") or mix.inputs[7]
        result_output = mix.outputs.get("Result") or mix.outputs[2]
    except Exception:
        mix = nodes.new("ShaderNodeMixRGB")
        mix.location = location
        factor_input = mix.inputs.get("Fac") or mix.inputs[0]
        a_input = mix.inputs.get("Color1") or mix.inputs[1]
        b_input = mix.inputs.get("Color2") or mix.inputs[2]
        result_output = mix.outputs.get("Color") or mix.outputs[0]

    links.new(factor_socket, factor_input)
    links.new(current_color, a_input)
    links.new(next_color, b_input)
    return result_output


def _build_cube_skybox_nodes(
    tree: bpy.types.NodeTree,
    face_images: dict[int, bpy.types.Image],
) -> object | None:
    if len(face_images) != SKY_CUBE_FACE_COUNT:
        return None

    nodes = tree.nodes
    links = tree.links

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-1800.0, 120.0)
    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (-1580.0, 120.0)
    vector_socket = (
        texcoord.outputs.get("Reflection")
        or texcoord.outputs.get("Generated")
        or texcoord.outputs.get("Window")
    )
    if vector_socket is None:
        return None
    links.new(vector_socket, separate.inputs.get("Vector") or separate.inputs[0])

    axis_sockets = {
        axis: _component_socket(separate, axis)
        for axis in ("X", "Y", "Z")
    }
    # Imported level positions use X, -Y, Z. Apply that same handedness flip
    # before choosing Unreal cubemap faces.
    axis_sockets["Y"] = _negate_socket(
        nodes,
        links,
        axis_sockets["Y"],
        (-1360.0, -160.0),
        "Blender direction Y -> UE direction Y",
    )
    abs_sockets = {}
    for offset, axis in enumerate(("X", "Y", "Z")):
        abs_node = _new_math(
            nodes,
            "ABSOLUTE",
            f"abs {axis}",
            (-1360.0, 300.0 - offset * 140.0),
        )
        links.new(axis_sockets[axis], abs_node.inputs[0])
        abs_sockets[axis] = abs_node.outputs[0]

    max_xy = _new_math(nodes, "MAXIMUM", "max abs XY", (-1140.0, 190.0))
    links.new(abs_sockets["X"], max_xy.inputs[0])
    links.new(abs_sockets["Y"], max_xy.inputs[1])
    max_axis = _new_math(nodes, "MAXIMUM", "max abs XYZ", (-920.0, 140.0))
    links.new(max_xy.outputs[0], max_axis.inputs[0])
    links.new(abs_sockets["Z"], max_axis.inputs[1])

    axis_max_masks = {}
    for offset, axis in enumerate(("X", "Y", "Z")):
        compare = _new_math(
            nodes,
            "COMPARE",
            f"{axis} is major axis",
            (-700.0, 300.0 - offset * 140.0),
        )
        if len(compare.inputs) > 2:
            compare.inputs[2].default_value = 0.00001
        links.new(abs_sockets[axis], compare.inputs[0])
        links.new(max_axis.outputs[0], compare.inputs[1])
        axis_max_masks[axis] = compare.outputs[0]

    current_color = None
    for face_index in range(SKY_CUBE_FACE_COUNT):
        major_axis, sign_operation, u_data, v_data = _face_axis_data(face_index)
        base_x = -440.0
        base_y = 660.0 - face_index * 320.0

        sign_mask = _new_math(
            nodes,
            sign_operation,
            f"{SKY_CUBE_FACE_NAMES[face_index]} sign",
            (base_x, base_y),
        )
        sign_mask.inputs[1].default_value = 0.0
        links.new(axis_sockets[major_axis], sign_mask.inputs[0])

        face_mask = _new_math(
            nodes,
            "MULTIPLY",
            f"{SKY_CUBE_FACE_NAMES[face_index]} mask",
            (base_x + 200.0, base_y),
        )
        links.new(axis_max_masks[major_axis], face_mask.inputs[0])
        links.new(sign_mask.outputs[0], face_mask.inputs[1])

        def signed_axis_socket(axis_data, y_offset: float, label: str):
            axis, sign = axis_data
            socket = axis_sockets[axis]
            if sign < 0.0:
                return _negate_socket(
                    nodes,
                    links,
                    socket,
                    (base_x + 200.0, base_y + y_offset),
                    label,
                )
            return socket

        u_num = signed_axis_socket(u_data, -70.0, f"{SKY_CUBE_FACE_NAMES[face_index]} U negate")
        v_num = signed_axis_socket(v_data, -150.0, f"{SKY_CUBE_FACE_NAMES[face_index]} V negate")
        denom = abs_sockets[major_axis]
        u_socket = _normalize_to_uv(
            nodes,
            links,
            u_num,
            denom,
            (base_x + 400.0, base_y - 70.0),
            f"{SKY_CUBE_FACE_NAMES[face_index]} U",
        )
        v_socket = _normalize_to_uv(
            nodes,
            links,
            v_num,
            denom,
            (base_x + 400.0, base_y - 170.0),
            f"{SKY_CUBE_FACE_NAMES[face_index]} V",
        )

        combine = nodes.new("ShaderNodeCombineXYZ")
        combine.label = f"{SKY_CUBE_FACE_NAMES[face_index]} UV"
        combine.location = (base_x + 980.0, base_y - 120.0)
        links.new(u_socket, combine.inputs.get("X") or combine.inputs[0])
        links.new(v_socket, combine.inputs.get("Y") or combine.inputs[1])

        image_node = nodes.new("ShaderNodeTexImage")
        image_node.label = f"Side_{face_index} {SKY_CUBE_FACE_NAMES[face_index]}"
        image_node.location = (base_x + 1200.0, base_y - 120.0)
        image_node.image = face_images[face_index]
        try:
            image_node.extension = "EXTEND"
        except Exception:
            pass
        links.new(combine.outputs.get("Vector") or combine.outputs[0], image_node.inputs.get("Vector") or image_node.inputs[0])

        color_socket = image_node.outputs.get("Color") or image_node.outputs[0]
        if current_color is None:
            current_color = color_socket
        else:
            current_color = _make_color_mix(
                nodes,
                links,
                current_color,
                color_socket,
                face_mask.outputs[0],
                (base_x + 1500.0, base_y - 120.0),
            )

    return current_color


def _create_or_update_world(
    world_name: str,
    fog_props: dict,
    source_json: str,
    switch_labels: list[str] | None,
    source_asset_path: str | None,
) -> tuple[bpy.types.World, bool]:
    world = bpy.data.worlds.get(world_name)
    created = world is None
    if world is None:
        world = bpy.data.worlds.new(world_name)

    world["ff7r_source_json"] = source_json
    if source_asset_path:
        world["ff7r_source_asset_path"] = source_asset_path
    if switch_labels:
        world["ff7r_world_sky_switch_labels"] = ", ".join(switch_labels)

    context_a = fog_props.get("ContextA", {}) if isinstance(fog_props, dict) else {}
    if isinstance(context_a, dict):
        albedo = color_from_ue_linear_or_srgb(context_a.get("Albedo", {}))
        world["ff7r_fog_albedo"] = list(albedo)
        if isinstance(context_a.get("Albedo"), dict) and context_a["Albedo"].get("Hex"):
            world["ff7r_fog_albedo_hex"] = context_a["Albedo"].get("Hex")
        for key in ("AlbedoBasis", "DensityBasis", "DensityFalloff"):
            if key in context_a:
                world[f"ff7r_fog_{key}"] = _float_or_default(context_a.get(key))

    distant_view = fog_props.get("DistantViewEnvironment", {}) if isinstance(fog_props, dict) else {}
    if isinstance(distant_view, dict):
        for key in ("BuildDataId", "AverageBrightness"):
            if key in distant_view:
                world[f"ff7r_distant_view_{key}"] = distant_view.get(key)

    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputWorld")
    output.location = (900.0, 80.0)
    background = nodes.new("ShaderNodeBackground")
    background.location = (620.0, 160.0)
    links.new(background.outputs.get("Background") or background.outputs[0], output.inputs.get("Surface") or output.inputs[0])

    context_a = fog_props.get("ContextA", {}) if isinstance(fog_props, dict) else {}
    albedo = color_from_ue_linear_or_srgb(context_a.get("Albedo", {}) if isinstance(context_a, dict) else {})
    world.color = albedo[:3]
    _set_node_input(background, ("Color",), albedo)
    _set_node_input(background, ("Strength",), 1.0)

    texture_root, texture_extension = _get_texture_settings()
    face_images = _load_distant_view_face_images(
        fog_props,
        texture_root,
        texture_extension,
        world_name,
    )
    cube_color_socket = _build_cube_skybox_nodes(world.node_tree, face_images)
    if cube_color_socket is not None:
        links.new(cube_color_socket, background.inputs.get("Color") or background.inputs[0])

    return world, created


def _finite_fog_extent_from_props(fog_props: dict, location_scale: float) -> float:
    scale_ratio = location_scale / DEFAULT_LOCATION_SCALE
    min_extent = MIN_FINITE_FOG_EXTENT * scale_ratio
    max_extent = MAX_FINITE_FOG_EXTENT * scale_ratio
    extent = DEFAULT_FINITE_FOG_EXTENT * scale_ratio
    if not isinstance(fog_props, dict):
        return extent

    distance = fog_props.get("Distance", {})
    rel_location = fog_props.get("RelativeLocation", {})
    if isinstance(distance, dict) and isinstance(rel_location, dict):
        fallback_height = distance.get("FallbackHeight")
        rel_z = rel_location.get("Z")
        if fallback_height is not None and rel_z is not None:
            height_extent = abs(_float_or_default(rel_z) - _float_or_default(fallback_height)) * location_scale
            if height_extent > 0.0:
                extent = max(extent, height_extent)

    context_a = fog_props.get("ContextA", {})
    if isinstance(context_a, dict):
        falloff = abs(_float_or_default(context_a.get("DensityFalloff", 0.0)))
        if falloff > 0.0:
            extent = max(extent, min(max_extent, (1.0 / falloff) * location_scale))

    return min(max_extent, max(min_extent, extent))


def _ensure_cube_mesh(mesh_name: str) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.get(mesh_name)
    if mesh is not None and mesh.get("ff7r_finite_fog_cube_version") == FINITE_FOG_CUBE_VERSION:
        return mesh

    if mesh is None:
        mesh = bpy.data.meshes.new(mesh_name)
    else:
        mesh.clear_geometry()

    verts = [
        (-1.0, -1.0, -1.0),
        (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0),
        (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0),
        (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0),
    ]
    faces = [
        (0, 3, 2, 1),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 5, 6, 2),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ]
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    mesh.update()
    mesh["ff7r_finite_fog_cube_version"] = FINITE_FOG_CUBE_VERSION
    return mesh


def _ensure_fog_volume_material(
    material_name: str,
    fog_props: dict,
) -> bpy.types.Material:
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        mat = bpy.data.materials.new(material_name)

    mat.use_nodes = True
    try:
        mat.blend_method = "BLEND"
    except Exception:
        pass

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (620.0, 0.0)

    context_a = fog_props.get("ContextA", {}) if isinstance(fog_props, dict) else {}
    albedo = color_from_ue_linear_or_srgb(context_a.get("Albedo", {}) if isinstance(context_a, dict) else {})
    mat.diffuse_color = albedo
    density = _float_or_default(context_a.get("DensityBasis", 0.0)) if isinstance(context_a, dict) else 0.0
    light = fog_props.get("Light", {}) if isinstance(fog_props, dict) else {}
    anisotropy = _fog_anisotropy(fog_props)

    try:
        volume = nodes.new("ShaderNodeVolumePrincipled")
        volume.location = (360.0, 0.0)
        volume.label = "Finite ExponentialHeightFog approximation"
        _set_node_input(volume, ("Color", "Base Color"), albedo)
        _set_node_input(volume, ("Density",), density)
        _set_node_input(volume, ("Anisotropy",), anisotropy)
        volume_output = _node_output(volume, "Volume")
    except Exception:
        scatter = nodes.new("ShaderNodeVolumeScatter")
        scatter.location = (140.0, 90.0)
        absorption = nodes.new("ShaderNodeVolumeAbsorption")
        absorption.location = (140.0, -90.0)
        add = nodes.new("ShaderNodeAddShader")
        add.location = (380.0, 0.0)

        _set_node_input(scatter, ("Color",), albedo)
        _set_node_input(scatter, ("Density",), density)
        _set_node_input(scatter, ("Anisotropy",), anisotropy)
        _set_node_input(absorption, ("Color",), albedo)
        _set_node_input(absorption, ("Density",), density)

        links.new(_node_output(scatter, "Volume"), add.inputs[0])
        links.new(_node_output(absorption, "Volume"), add.inputs[1])
        volume_output = add.outputs[0]

    volume_socket = output.inputs.get("Volume")
    if volume_socket is not None and volume_output is not None:
        links.new(volume_output, volume_socket)

    mat["ff7r_fog_density_basis"] = density
    mat["ff7r_fog_albedo"] = list(albedo)
    mat["ff7r_fog_anisotropy"] = anisotropy
    if isinstance(light, dict) and "Modulator" in light:
        mat["ff7r_fog_light_modulator"] = _float_or_default(light.get("Modulator", 0.0))
    if isinstance(context_a, dict):
        albedo_prop = context_a.get("Albedo", {})
        if isinstance(albedo_prop, dict) and albedo_prop.get("Hex"):
            mat["ff7r_fog_albedo_hex"] = str(albedo_prop.get("Hex"))
        for key in ("AlbedoBasis", "DensityFalloff"):
            if key in context_a:
                mat[f"ff7r_fog_{key}"] = _float_or_default(context_a.get(key))

    return mat


def create_finite_fog_volume_from_level_data(
    data: list,
    filepath: str,
    location_scale: float = 0.01,
) -> int:
    fog_entry = _find_height_fog_with_distant_view(data)
    if fog_entry is None:
        return 0

    fog_props = fog_entry.get("Properties", {})
    if not isinstance(fog_props, dict):
        return 0

    world_name = os.path.splitext(os.path.basename(filepath))[0]
    obj_name = f"{world_name}_FiniteFog"
    material_name = f"{world_name}_FiniteFog_Mat"
    mesh_name = f"{obj_name}_Mesh"

    root_collection = ensure_parent_collection_for_file(filepath)
    fog_collection = ensure_typed_import_collection(root_collection, "_Fog")
    mesh = _ensure_cube_mesh(mesh_name)
    obj = bpy.data.objects.get(obj_name)
    created = obj is None
    if obj is None:
        obj = bpy.data.objects.new(obj_name, mesh)
    else:
        obj.data = mesh

    loc_dict = fog_props.get("RelativeLocation", {})
    obj.location = location_from_relative(loc_dict, scale_factor=location_scale)
    extent = _finite_fog_extent_from_props(fog_props, location_scale)
    rel_scale = scale_from_relative(fog_props.get("RelativeScale3D", {}))
    obj.scale = (
        extent * rel_scale.x,
        extent * rel_scale.y,
        extent * rel_scale.z,
    )
    try:
        obj.display_type = "TEXTURED"
    except Exception:
        pass
    obj.show_name = True

    mat = _ensure_fog_volume_material(material_name, fog_props)
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    obj.color = mat.diffuse_color

    obj["ff7r_source_json"] = filepath
    obj["ff7r_fog_volume_extent"] = extent
    obj["ff7r_fog_volume_extent_units"] = "Blender units"
    obj["ff7r_note"] = "Bounded volume approximation; Blender World volume is infinite."

    if fog_collection.objects.get(obj.name) is None:
        try:
            fog_collection.objects.link(obj)
        except RuntimeError:
            pass

    print(
        f"[End JSON Import]   Finite fog volume ready: {obj.name} "
        f"(extent {extent:.1f} Blender units)."
    )
    return 1 if created else 0


def _load_distant_view_face_images(
    fog_props: dict,
    texture_root: str,
    texture_extension: str,
    world_name: str,
) -> dict[int, bpy.types.Image]:
    distant_view = fog_props.get("DistantViewEnvironment", {}) if isinstance(fog_props, dict) else {}
    face_dir = _distant_view_folder(distant_view, texture_root)
    if not face_dir:
        if not texture_root:
            print(
                f"[End JSON Import]   World '{world_name}' has DistantViewEnvironment, "
                "but Texture Content Root is not set."
            )
        return {}

    side_paths = _find_cube_side_paths(face_dir, texture_extension)
    if len(side_paths) != SKY_CUBE_FACE_COUNT:
        print(
            f"[End JSON Import]   World '{world_name}' skybox has "
            f"{len(side_paths)}/{SKY_CUBE_FACE_COUNT} cube side texture(s): {face_dir}"
        )
        return {}

    images: dict[int, bpy.types.Image] = {}
    datablock_prefix = os.path.basename(face_dir.rstrip(os.sep)) or world_name
    for side_index, side_path in sorted(side_paths.items()):
        image = _load_image(side_path, datablock_prefix)
        if image is not None:
            images[side_index] = image

    if len(images) == SKY_CUBE_FACE_COUNT:
        print(f"[End JSON Import]   Loaded WorldSky cube textures for '{world_name}': {face_dir}")
    return images


def create_world_from_level_data(
    data: list,
    filepath: str,
    switch_labels: list[str] | None = None,
    source_asset_path: str | None = None,
) -> bool:
    fog_entry = _find_height_fog_with_distant_view(data)
    if fog_entry is None:
        return False

    props = fog_entry.get("Properties", {})
    if not isinstance(props, dict):
        return False

    distant_view = props.get("DistantViewEnvironment", {})
    texture = distant_view.get("Texture", {}) if isinstance(distant_view, dict) else {}
    if not isinstance(texture, dict) or not texture.get("ObjectName"):
        return False

    world_name = os.path.splitext(os.path.basename(filepath))[0]
    _create_or_update_world(
        world_name=world_name,
        fog_props=props,
        source_json=filepath,
        switch_labels=switch_labels,
        source_asset_path=source_asset_path,
    )
    print(f"[End JSON Import]   WorldSky datablock ready: {world_name}")
    return True


def _make_light_probe_data(name: str):
    lightprobes = getattr(bpy.data, "lightprobes", None)
    if lightprobes is None:
        return None

    for probe_type in ("SPHERE", "CUBEMAP"):
        try:
            return lightprobes.new(name, probe_type)
        except Exception:
            continue
    return None


def _set_probe_radius(probe_data, radius: float) -> None:
    for attr in ("influence_distance", "influence_radius"):
        if hasattr(probe_data, attr):
            try:
                setattr(probe_data, attr, radius)
            except Exception:
                pass
    if hasattr(probe_data, "clip_end") and radius > 0.0:
        try:
            probe_data.clip_end = max(probe_data.clip_end, radius)
        except Exception:
            pass


def create_reflection_capture_probes(
    data: list,
    filepath: str,
    location_scale: float = 0.01,
) -> int:
    """Create Blender Light Probe Sphere objects from SphereReflectionCaptureComponent entries."""
    components = [
        entry for entry in data
        if isinstance(entry, dict) and entry.get("Type") == "SphereReflectionCaptureComponent"
    ]
    if not components:
        return 0

    root_collection = ensure_parent_collection_for_file(filepath)
    probes_collection = ensure_typed_import_collection(root_collection, "_LightProbes")
    created_count = 0

    for entry in components:
        props = entry.get("Properties", {})
        if not isinstance(props, dict):
            props = {}

        outer_name = entry.get("Outer") if isinstance(entry.get("Outer"), str) else ""
        component_name = entry.get("Name", "SphereReflectionCaptureComponent")
        probe_name = outer_name or component_name

        probe_data = _make_light_probe_data(probe_name)
        if probe_data is None:
            probe_obj = bpy.data.objects.new(probe_name, None)
            probe_obj.empty_display_type = "SPHERE"
            probe_obj.empty_display_size = 1.0
            print(
                f"[End JSON Import]   Light Probe Sphere type is unavailable; "
                f"created fallback sphere empty '{probe_name}'."
            )
        else:
            probe_obj = bpy.data.objects.new(probe_name, probe_data)

        loc_dict = props.get("RelativeLocation", {})
        probe_obj.location = location_from_relative(loc_dict, scale_factor=location_scale)
        probe_obj.scale = scale_from_relative(props.get("RelativeScale3D", {}))

        influence_radius = _float_or_default(props.get("InfluenceRadius", 0.0))
        probe_obj["ue_influence_radius"] = influence_radius
        probe_obj["ue_influence_radius_units"] = "centimeters"
        probe_obj["ff7r_source_json"] = filepath
        if outer_name:
            probe_obj["ff7r_reflection_capture_actor"] = outer_name
        if props.get("MapBuildDataId"):
            probe_obj["ff7r_map_build_data_id"] = props.get("MapBuildDataId")
        if "MaxValueRGBM" in props:
            probe_obj["ff7r_max_value_rgbm"] = _float_or_default(props.get("MaxValueRGBM"))

        if probe_data is not None:
            probe_data["ue_influence_radius"] = influence_radius
            probe_data["ff7r_source_json"] = filepath
            _set_probe_radius(probe_data, influence_radius * location_scale)

        probes_collection.objects.link(probe_obj)
        created_count += 1

    print(
        f"[End JSON Import]   Created {created_count} Light Probe Sphere object(s) "
        f"for '{os.path.basename(filepath)}'."
    )
    return created_count


def import_world_sky_references(
    data: list,
    game_root: str,
    imported_world_sky_paths: set[str],
    location_scale: float = 0.01,
) -> WorldSkyImportResult:
    result = WorldSkyImportResult()
    refs = collect_world_sky_asset_references(data)
    if not refs:
        return result

    if not game_root:
        print("[End JSON Import]   WorldSky switch volume found but Game Root is not set - skipping.")
        return result

    print(f"[End JSON Import]   WorldSky switch volume references {len(refs)} unique sky level(s).")

    for ref in refs:
        json_path = resolve_game_asset_file_path(ref.asset_path_name, game_root, ".json")
        if not json_path:
            continue
        json_path = os.path.realpath(json_path)
        if json_path in imported_world_sky_paths:
            continue
        imported_world_sky_paths.add(json_path)

        if not os.path.exists(json_path):
            print(f"[End JSON Import]   WorldSky JSON not found: {json_path}")
            result.missing_json_paths.add(json_path)
            continue

        sky_data = _load_json_list(json_path)
        if sky_data is None:
            continue

        if create_world_from_level_data(
            sky_data,
            json_path,
            switch_labels=ref.switch_labels,
            source_asset_path=ref.asset_path_name,
        ):
            result.worlds_created += 1
        result.fog_volumes_created += create_finite_fog_volume_from_level_data(
            sky_data,
            json_path,
            location_scale=location_scale,
        )
        result.probes_created += create_reflection_capture_probes(
            sky_data,
            json_path,
            location_scale=location_scale,
        )

    return result
