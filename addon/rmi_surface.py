"""Package StaticMesh integration for the researched FF7R RMI_Surface port.

The authored RMI node builder and its variant trimmer remain in the companion
``material related/blender`` research project.  Loading them from their source
location avoids maintaining a divergent copy of a 3,000-line generated shader
builder in this add-on.  This module is called by the direct package
StaticMesh and SkeletalMesh importers -- and, through PackageStaticMeshResolver,
by the package UMAP importer's StaticMesh actors, since they share the same
importer.  Both mesh types reach it the same way, because the bridge reports a
section's material the same way for either (SkeletalMaterials vs
StaticMaterials).  Loose MEC UMAPs, JSON imports, and non-RMI material instances
retain their existing material paths.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Callable

import bpy


_MODULES: tuple[Any, Any, dict[str, list[str]]] | None = None
_MATERIAL_CACHE: dict[str, bpy.types.Material] = {}
_GAZE_NORMAL_OVERRIDE_IMAGE: bpy.types.Image | None = None
_COMMON_EYE_PLAYER_NG = "/game/character/common/eye/texture/common_eye_player_ng"
_RENDERER_CONSTANT_TEXTURE = re.compile(
    r"(?:^|/)([0-9a-f]{8})_(srgb|bc4|bc5|hdr)(?:\.[^/]*)?$", re.IGNORECASE
)


def _source_files() -> tuple[Path, Path, Path]:
    """Locate packaged RMI sources, with the research checkout as a dev fallback."""
    addon_directory = Path(__file__).resolve().parent
    packaged = addon_directory / "rmi_sources"
    development = addon_directory.parent.parent / "material related" / "blender"
    candidates = (
        (packaged / "ff7r_rmi_surface.py", packaged / "ff7r_rmi_surface_variant.py",
         packaged / "renderer_ground_truth.json"),
        (development / "ff7r_rmi_surface.py", development / "ff7r_rmi_surface_variant.py",
         development.parent / "scripts" / "renderer_ground_truth.json"),
    )
    for master, variant, ground_truth in candidates:
        if master.is_file() and variant.is_file() and ground_truth.is_file():
            return master, variant, ground_truth
    raise FileNotFoundError(
        "RMI_Surface runtime sources are missing. Expected packaged sources in "
        f"'{packaged}' or the sibling material-research checkout. See README_ADDON.md."
    )


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load RMI_Surface source module '{path}'.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules() -> tuple[Any, Any, dict[str, list[str]]]:
    global _MODULES
    if _MODULES is not None:
        return _MODULES
    master_path, variant_path, ground_truth_path = _source_files()
    master = _load_module("ff7r_rebirth_tools_rmi_master", master_path)
    variant = _load_module("ff7r_rebirth_tools_rmi_variant", variant_path)
    with ground_truth_path.open(encoding="utf-8") as stream:
        ground_truth = json.load(stream)
    _MODULES = master, variant, ground_truth
    return _MODULES


def _properties(material_data: dict[str, Any]) -> dict[str, Any]:
    """Turn the bridge's converted FPropertyTag sequence into a name map."""
    result: dict[str, Any] = {}
    for property_data in material_data.get("properties") or []:
        if not isinstance(property_data, dict):
            continue
        name = property_data.get("name")
        if name:
            result[str(name)] = _unwrap_struct(property_data.get("value"))
    return result


def _unwrap_struct(value: Any) -> Any:
    """Remove CUE4Parse's synthetic StructType layer from cooked array items."""
    while isinstance(value, dict) and set(value) == {"StructType"}:
        value = value["StructType"]
    return value


def _first(mapping: Any, *keys: str) -> Any:
    mapping = _unwrap_struct(mapping)
    if not isinstance(mapping, dict):
        return None
    folded = {str(key).casefold(): value for key, value in mapping.items()}
    for key in keys:
        value = folded.get(key.casefold())
        if value is not None:
            return value
    return None


def _parameter_name(entry: Any) -> str:
    entry = _unwrap_struct(entry)
    info = _first(entry, "ParameterInfo", "Parameter")
    info = _unwrap_struct(info)
    name = _first(info, "Name") if isinstance(info, dict) else None
    return str(name or "")


def _object_path(value: Any) -> str:
    value = _unwrap_struct(value)
    if not isinstance(value, dict):
        return ""
    return str(_first(value, "ObjectPath", "Path") or "")


def _colour(value: Any) -> tuple[float, float, float, float] | None:
    """Read CUE4Parse's FLinearColor/FColor forms without guessing strings."""
    value = _unwrap_struct(value)
    if isinstance(value, (list, tuple)) and 3 <= len(value) <= 4:
        channels = list(value) + [1.0]
    elif isinstance(value, dict):
        channels = [_first(value, "R", "X"), _first(value, "G", "Y"),
                    _first(value, "B", "Z"), _first(value, "A", "W")]
        if any(channel is None for channel in channels[:3]):
            nested = _first(value, "Color", "Value")
            if nested is not value:
                return _colour(nested)
        if channels[3] is None:
            channels[3] = 1.0
    else:
        return None
    try:
        result = tuple(float(channel) for channel in channels[:4])
    except (TypeError, ValueError):
        return None
    # FColor is byte based; FLinearColor is already 0..1.
    if max(result) > 1.0:
        result = tuple(channel / 255.0 for channel in result)
    return result


def _srgb_to_linear(channel: int) -> float:
    """Decode one 8-bit sRGB component for a Blender shader colour socket."""
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _renderer_constant_texture(texture_path: str) -> tuple[float, float, float, float] | None:
    """Convert UE's named renderer constant textures (e.g. FFFFFFFF_sRGB,
    8080FFFF_BC5, FFFFFFFF_BC4, FFFFFFFF_HDR) to a plain RGBA colour.

    These are baked colour constants, not image assets that need exporting --
    the 8 hex digits are the four RGBA bytes of a solid-colour texture the
    renderer substitutes at that format. The bridge cannot create a DDS for
    their uncompressed payload, and more importantly Blender should represent
    them as a constant node rather than sampling a texture.

    Only the ``_sRGB`` suffix is gamma-encoded. ``_BC4`` (single-channel masks)
    and ``_BC5`` (tangent-space normal maps) are UE UNORM formats that are
    never sRGB, and ``_HDR`` constants are already linear -- all three decode
    with a straight /255 normalize.
    """
    match = _RENDERER_CONSTANT_TEXTURE.search(texture_path.replace("\\", "/"))
    if match is None:
        return None
    encoded = bytes.fromhex(match.group(1))
    if match.group(2).casefold() == "srgb":
        return (*(_srgb_to_linear(channel) for channel in encoded[:3]), encoded[3] / 255.0)
    return tuple(channel / 255.0 for channel in encoded[:4])


def _normalized(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _role_values(props: dict[str, Any]) -> tuple[dict[str, str], dict[str, tuple[float, float, float, float]], dict[str, float]]:
    textures: dict[str, str] = {}
    colours: dict[str, tuple[float, float, float, float]] = {}
    scalars: dict[str, float] = {}
    for entry in props.get("TextureParameterValues") or []:
        entry = _unwrap_struct(entry)
        name = _parameter_name(entry)
        value = _unwrap_struct(_first(entry, "ParameterValue", "Value"))
        path = _object_path(value)
        if name and path:
            textures[_normalized(name)] = path
    for entry in props.get("VectorParameterValues") or []:
        entry = _unwrap_struct(entry)
        name = _parameter_name(entry)
        colour = _colour(_unwrap_struct(_first(entry, "ParameterValue", "Value")))
        if name and colour is not None:
            colours[_normalized(name)] = colour
    for entry in props.get("ScalarParameterValues") or []:
        entry = _unwrap_struct(entry)
        name = _parameter_name(entry)
        value = _unwrap_struct(_first(entry, "ParameterValue", "Value"))
        try:
            if name:
                scalars[_normalized(name)] = float(value)
        except (TypeError, ValueError):
            pass
    return textures, colours, scalars


def _role_match(values: dict[str, Any], role: str) -> Any:
    candidates = [_normalized(role)]
    aliases = {"screlanormal": "scleranormal"}
    candidates.append(aliases.get(candidates[0], candidates[0]))
    for candidate in candidates:
        if candidate in values:
            return values[candidate]
    return None


def _role_default_colour(kind: str, default: Any) -> tuple[float, float, float, float]:
    """Expand a ROLES neutral default (master.ROLES' last column) to RGBA.

    'val' roles store a bare scalar (replicated across RGB); 'col'/'nrm'
    roles already store a 4-tuple.
    """
    if kind == "val":
        value = float(default)
        return (value, value, value, 1.0)
    if isinstance(default, tuple) and len(default) == 4:
        return default
    return (0.0, 0.0, 0.0, 1.0)


def _make_colour_node(tree: bpy.types.NodeTree, image_node: bpy.types.Node, colour: tuple[float, float, float, float]) -> None:
    """Replace an RMI image slot with a real RGB constant, preserving consumers."""
    outputs = [(link.to_node, link.to_socket) for link in image_node.outputs["Color"].links]
    name = image_node.name
    parent, location, label = image_node.parent, image_node.location.copy(), image_node.label
    tree.nodes.remove(image_node)
    constant = tree.nodes.new("ShaderNodeRGB")
    constant.name = name
    constant.label = f"{label}  [UE color constant]"
    constant.parent = parent
    constant.location = location
    constant.outputs["Color"].default_value = colour
    for node, socket in outputs:
        tree.links.new(constant.outputs["Color"], socket)


def _set_image_color_space(image: bpy.types.Image, color_space: str) -> None:
    """Apply the RMI slot's authored data type to its shared image datablock."""
    try:
        image.colorspace_settings.name = color_space
    except (AttributeError, TypeError, ValueError):
        print(f"  Warning: Blender has no '{color_space}' color space for '{image.name}'.")


def _bundled_gaze_normal(texture_path: str) -> bpy.types.Image | None:
    """Use the lossless deblocked shared-eye normal for its exact game path."""
    normalized_path = texture_path.replace("\\", "/").split(".", 1)[0].casefold()
    if normalized_path != _COMMON_EYE_PLAYER_NG:
        return None

    global _GAZE_NORMAL_OVERRIDE_IMAGE
    try:
        cached = _GAZE_NORMAL_OVERRIDE_IMAGE
        if cached is not None and cached.name in bpy.data.images:
            return cached
    except ReferenceError:
        _GAZE_NORMAL_OVERRIDE_IMAGE = None

    asset_path = Path(__file__).resolve().parent / "assets" / "Common_Eye_Player_NG_filtered.png"
    try:
        image = bpy.data.images.load(str(asset_path), check_existing=True)
        image.name = "Common_Eye_Player_NG [FF7R deblocked]"
        image.colorspace_settings.name = "Non-Color"
        image["ff7r_virtual_path"] = texture_path
        image["ff7r_override_source"] = "Common_Eye_Player_NG BC5"
        image["ff7r_override_filter"] = "lossless 16-bit, 5x5 binomial"
        image.pack()
        _GAZE_NORMAL_OVERRIDE_IMAGE = image
        return image
    except Exception as exc:
        print(f"  Warning: bundled GazeNormal override could not be loaded: {exc}")
        return None


def _variant_name(props: dict[str, Any]) -> str:
    parent = _first(props, "Parent")
    path = _object_path(parent)
    name = path.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    return name if name.startswith("RMI_Surface_") else ""


def _ensure_master(master: Any) -> bpy.types.Material:
    material = bpy.data.materials.get(master.MAT_NAME)
    if material is None or material.node_tree is None or material.node_tree.nodes.get("TEX_Color") is None:
        material = master.build()
    return material


def build_material(
        material_name: str,
        material_path: str,
        material_data: dict[str, Any],
        image_loader: Callable[[str], bpy.types.Image | None],
) -> bpy.types.Material | None:
    """Create and populate a trimmed RMI material for one package material.

    Returns ``None`` for non-RMI material instances, which lets the existing
    placeholder material behavior remain intact for unrelated game shaders.
    """
    props = _properties(material_data)
    variant_name = _variant_name(props)
    if not variant_name:
        return None
    master, variant_helper, ground_truth = _modules()
    switches = ground_truth.get(variant_name)
    if switches is None:
        print(f"  RMI material '{material_name}' skipped: unknown variant '{variant_name}'.")
        return None
    cache_key = f"{material_path.casefold()}::{variant_name}"
    cached = _MATERIAL_CACHE.get(cache_key)
    if cached is not None:
        try:
            still_valid = cached.name in bpy.data.materials
        except ReferenceError:
            # _MATERIAL_CACHE is module-level and outlives File > New (only an
            # addon reload/Blender restart clears it), so a cached entry can be
            # a Material from a file that no longer exists. Reading .name on
            # its freed RNA struct is itself what raises here.
            still_valid = False
        if still_valid:
            return cached
        del _MATERIAL_CACHE[cache_key]

    master_material = _ensure_master(master)
    material = master_material.copy()
    material.name = material_name
    material["ff7r_virtual_path"] = material_path
    variant_helper.apply_variant(material, master, variant_name, set(switches))

    texture_values, colour_values, scalar_values = _role_values(props)
    assigned_images = assigned_colours = 0
    for _frame, role, kind, color_space, default in master.ROLES:
        node = material.node_tree.nodes.get("TEX_" + role)
        if node is None:
            continue
        texture_path = _role_match(texture_values, role)
        colour = _role_match(colour_values, role)
        texture_constant = _renderer_constant_texture(texture_path) if texture_path else None
        if texture_constant is not None and node.bl_idname == "ShaderNodeTexImage":
            _make_colour_node(material.node_tree, node, texture_constant)
            assigned_colours += 1
        elif texture_path:
            image = _bundled_gaze_normal(texture_path) if role == "GazeNormal" else None
            if image is None:
                image = image_loader(texture_path)
            if image is not None and hasattr(node, "image"):
                _set_image_color_space(image, color_space)
                node.image = image
                assigned_images += 1
            elif node.bl_idname == "ShaderNodeTexImage":
                _make_colour_node(material.node_tree, node, _role_default_colour(kind, default))
                assigned_colours += 1
        elif colour is not None and node.bl_idname == "ShaderNodeTexImage":
            _make_colour_node(material.node_tree, node, colour)
            assigned_colours += 1
        elif node.bl_idname == "ShaderNodeTexImage":
            # Not every role gets overridden anywhere in a game instance's
            # Parent chain -- e.g. DetailCoverage is never set on
            # PC0011_00_BodyB or any of its ancestors because the game always
            # points it at a neutral renderer-constant texture (FFFFFFFF_BC4
            # = 1.0) that never varies per character. Cooked Material assets
            # strip the per-parameter name from their CachedExpressionData
            # texture defaults, so that neutral value cannot be read back
            # from game data at all -- fall back to this role's own
            # reverse-engineered neutral default instead of leaving the node
            # with no image and no constant.
            _make_colour_node(material.node_tree, node, _role_default_colour(kind, default))
            assigned_colours += 1

    # Preserve named scalar overrides in the port's material custom properties.
    for name, _default, _min, _max, _description in master.FLOAT_PROPS:
        value = scalar_values.get(_normalized(name))
        if value is not None and name in material:
            material[name] = value
    material["ff7r_rmi_variant"] = variant_name
    material["ff7r_rmi_texture_images"] = assigned_images
    material["ff7r_rmi_color_constants"] = assigned_colours
    _MATERIAL_CACHE[cache_key] = material
    print(f"  RMI '{material.name}': {variant_name}; {assigned_images} image(s), {assigned_colours} color constant(s).")
    return material
