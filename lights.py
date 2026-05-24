"""Shared light parsing and creation helpers for FF7R JSON imports."""

from __future__ import annotations

import math

import bpy


UE_LIGHT_TYPES: dict[str, str] = {
    "SpotLight": "SPOT",
    "SpotLightComponent": "SPOT",
    "PointLight": "POINT",
    "PointLightComponent": "POINT",
}

VOLUMETRIC_SCATTERING_PROP = "VolumetricScatteringIntensity"
UE_ATTENUATION_RADIUS_PROP = "ue_attenuation_radius"
UE_ATTENUATION_RADIUS_UNITS_PROP = "ue_attenuation_radius_units"
UE_INVERSE_SQUARED_FALLOFF_PROP = "ue_use_inverse_squared_falloff"
UE_LIGHT_FALLOFF_EXPONENT_PROP = "ue_light_falloff_exponent"

UE_LIGHT_OBJECT_PROPS = (
    UE_ATTENUATION_RADIUS_PROP,
    UE_ATTENUATION_RADIUS_UNITS_PROP,
    UE_INVERSE_SQUARED_FALLOFF_PROP,
    UE_LIGHT_FALLOFF_EXPONENT_PROP,
)

DEFAULT_SOURCE_RADIUS_CM = 50.0
DEFAULT_SPOT_FULL_CONE_ANGLE_DEG = 44.0


def blender_light_type_from_name(name: str) -> str | None:
    """Return Blender light type from a UE object/template name."""
    for ue_name, bl_name in UE_LIGHT_TYPES.items():
        if ue_name in name:
            return bl_name
    return None


def float_or_default(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bool_or_default(value, default: bool = False) -> bool:
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


def get_light_temperature(props: dict) -> float | None:
    """Return whichever temperature key the export uses."""
    if "Temperature" in props:
        return float_or_default(props.get("Temperature"), 6500.0)
    if "ColorTemperature" in props:
        return float_or_default(props.get("ColorTemperature"), 6500.0)
    return None


def apply_default_light_options(
    light_data: bpy.types.Light,
    location_scale: float = 0.01,
) -> None:
    """Apply importer-wide Blender light defaults when the running version supports them."""
    if light_data is None:
        return
    if hasattr(light_data, "normalize"):
        light_data.normalize = False
    if hasattr(light_data, "shadow_soft_size"):
        light_data.shadow_soft_size = DEFAULT_SOURCE_RADIUS_CM * location_scale


def _new_math(nodes, operation: str, label: str = ""):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    if label:
        node.label = label
    return node


def apply_ue_attenuation_nodes(
    light_data: bpy.types.Light,
    energy: float,
    color: tuple[float, float, float],
    attenuation_radius: float,
    source_radius: float,
    use_inverse_squared: bool,
    falloff_exponent: float,
) -> bool:
    """Build a shader matching UE local-light falloff (inverse-squared with radius mask, or exponent-based)."""
    if attenuation_radius <= 0.0 or not hasattr(light_data, "node_tree"):
        return False

    try:
        light_data.use_nodes = True
        tree = light_data.node_tree
        nodes = tree.nodes
        links = tree.links
        nodes.clear()

        output = nodes.new("ShaderNodeOutputLight")
        emission = nodes.new("ShaderNodeEmission")
        falloff = nodes.new("ShaderNodeLightFalloff")

        output.location = (720, 0)
        emission.location = (500, 0)
        falloff.location = (-700, 0)

        emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        falloff.inputs["Strength"].default_value = 1.0
        if "Smooth" in falloff.inputs:
            falloff.inputs["Smooth"].default_value = max(source_radius, 0.0)

        # distance = Constant / Linear. The Light Falloff node computes these
        # from the point being shaded, so this gives us distance inside a light
        # node tree without a Geometry node.
        distance = _new_math(nodes, "DIVIDE", "UE distance")
        distance.location = (-480, -160)
        links.new(falloff.outputs["Constant"], distance.inputs[0])
        links.new(falloff.outputs["Linear"], distance.inputs[1])

        normalized = _new_math(nodes, "DIVIDE", "distance / radius")
        normalized.location = (-260, -160)
        normalized.inputs[1].default_value = attenuation_radius
        links.new(distance.outputs[0], normalized.inputs[0])
        try:
            radius_attr = nodes.new("ShaderNodeAttribute")
            radius_attr.label = "UE attenuation radius"
            radius_attr.location = (-480, -420)
            radius_attr.attribute_name = UE_ATTENUATION_RADIUS_PROP
            if hasattr(radius_attr, "attribute_type"):
                radius_attr.attribute_type = "INSTANCER"
            links.new(radius_attr.outputs["Fac"], normalized.inputs[1])
        except Exception:
            pass

        if use_inverse_squared:
            power4 = _new_math(nodes, "POWER", "(d/r)^4")
            power4.location = (-40, -160)
            power4.inputs[1].default_value = 4.0
            links.new(normalized.outputs[0], power4.inputs[0])

            one_minus = _new_math(nodes, "SUBTRACT", "1 - (d/r)^4")
            one_minus.location = (170, -160)
            one_minus.inputs[0].default_value = 1.0
            one_minus.use_clamp = True
            links.new(power4.outputs[0], one_minus.inputs[1])

            mask = _new_math(nodes, "MULTIPLY", "UE radius mask")
            mask.location = (360, -160)
            links.new(one_minus.outputs[0], mask.inputs[0])
            links.new(one_minus.outputs[0], mask.inputs[1])

            strength = _new_math(nodes, "MULTIPLY", "Quadratic * UE mask")
            strength.location = (360, 80)
            links.new(falloff.outputs["Quadratic"], strength.inputs[0])
            links.new(mask.outputs[0], strength.inputs[1])
        else:
            one_minus = _new_math(nodes, "SUBTRACT", "1 - d/r")
            one_minus.location = (-40, -160)
            one_minus.inputs[0].default_value = 1.0
            one_minus.use_clamp = True
            links.new(normalized.outputs[0], one_minus.inputs[1])

            strength = _new_math(nodes, "POWER", "UE falloff exponent")
            strength.location = (180, 80)
            strength.inputs[1].default_value = max(falloff_exponent, 0.0)
            links.new(one_minus.outputs[0], strength.inputs[0])

            multiply = _new_math(nodes, "MULTIPLY", "Constant * UE exponent")
            multiply.location = (360, 80)
            links.new(falloff.outputs["Constant"], multiply.inputs[0])
            links.new(strength.outputs[0], multiply.inputs[1])
            strength = multiply

        links.new(strength.outputs[0], emission.inputs["Strength"])
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
        return True

    except Exception as exc:
        print(
            f"[FF7R JSON Import]   Failed to build UE attenuation nodes for "
            f"'{light_data.name}': {exc}"
        )
        return False


def apply_common_static_light_properties(
    light_data: bpy.types.Light,
    props: dict,
    location_scale: float = 0.01,
    exposure_mult: float = 1.0,
    attenuation_radius_mult: float = 1.0,
) -> tuple[float, tuple[float, float, float]]:
    """Apply static UE light properties shared by point and spot components."""
    if "Intensity" in props:
        raw_intensity = props.get("Intensity", 0.0)
    else:
        raw_intensity = props.get("IntensityNits", 0.0)

    intensity = float_or_default(raw_intensity)
    light_data.energy = intensity * exposure_mult

    color_dict = props.get("LightColor", {})
    r = float_or_default(color_dict.get("R", 255.0), 255.0) / 255.0
    g = float_or_default(color_dict.get("G", 255.0), 255.0) / 255.0
    b = float_or_default(color_dict.get("B", 255.0), 255.0) / 255.0
    light_data.color = (r, g, b)

    light_data.use_shadow = bool_or_default(props.get("CastShadows", True), True)

    raw_source_radius = props.get("SourceRadius", DEFAULT_SOURCE_RADIUS_CM)
    if raw_source_radius is None:
        raw_source_radius = DEFAULT_SOURCE_RADIUS_CM
    src_radius = float_or_default(raw_source_radius) * location_scale
    if hasattr(light_data, "shadow_soft_size"):
        light_data.shadow_soft_size = src_radius

    att_radius = (
        float_or_default(props.get("AttenuationRadius", 0.0))
        * location_scale
        * max(float_or_default(attenuation_radius_mult, 1.0), 0.0)
    )
    use_inverse_squared = bool_or_default(props.get("bUseInverseSquaredFalloff", True), True)
    falloff_exponent = float_or_default(props.get("LightFalloffExponent", 8.0), 8.0)

    light_data[UE_ATTENUATION_RADIUS_PROP] = att_radius
    light_data[UE_ATTENUATION_RADIUS_UNITS_PROP] = "Blender units"
    light_data[UE_INVERSE_SQUARED_FALLOFF_PROP] = use_inverse_squared
    light_data[UE_LIGHT_FALLOFF_EXPONENT_PROP] = falloff_exponent

    if hasattr(light_data, "use_custom_distance"):
        light_data.use_custom_distance = att_radius > 0.0
    if hasattr(light_data, "cutoff_distance"):
        light_data.cutoff_distance = att_radius

    apply_ue_attenuation_nodes(
        light_data=light_data,
        energy=light_data.energy,
        color=light_data.color,
        attenuation_radius=att_radius,
        source_radius=src_radius,
        use_inverse_squared=use_inverse_squared,
        falloff_exponent=falloff_exponent,
    )

    temperature = get_light_temperature(props)
    if temperature is not None:
        if hasattr(light_data, "use_temperature"):
            light_data.use_temperature = True
        if hasattr(light_data, "temperature"):
            light_data.temperature = temperature

    if VOLUMETRIC_SCATTERING_PROP in props:
        light_data[VOLUMETRIC_SCATTERING_PROP] = float_or_default(
            props.get(VOLUMETRIC_SCATTERING_PROP),
            0.0,
        )

    return light_data.energy, light_data.color


def apply_ue_light_object_properties(
    light_obj: bpy.types.Object,
    remove_from_data: bool = True,
) -> None:
    """Move UE light custom properties from the light datablock to its object."""
    if light_obj is None:
        return
    light_data = getattr(light_obj, "data", None)
    if light_data is None:
        return

    for prop_name in UE_LIGHT_OBJECT_PROPS:
        if prop_name not in light_data:
            continue
        light_obj[prop_name] = light_data[prop_name]
        if remove_from_data:
            try:
                del light_data[prop_name]
            except Exception:
                pass


def apply_spot_cone_properties(light_data: bpy.types.Light, props: dict) -> None:
    """Apply UE full cone angles to a Blender spot light."""
    outer_full_deg = float_or_default(
        props.get("OuterFullConeAngle", DEFAULT_SPOT_FULL_CONE_ANGLE_DEG),
        DEFAULT_SPOT_FULL_CONE_ANGLE_DEG,
    )
    inner_full_deg = float_or_default(
        props.get("InnerFullConeAngle", DEFAULT_SPOT_FULL_CONE_ANGLE_DEG),
        DEFAULT_SPOT_FULL_CONE_ANGLE_DEG,
    )
    if outer_full_deg <= 0.0:
        return

    light_data.spot_size = math.radians(outer_full_deg)
    light_data.spot_blend = min(1.0, max(0.0, 1.0 - (inner_full_deg / outer_full_deg)))


def create_static_light_data(
    name: str,
    light_type: str,
    props: dict,
    location_scale: float = 0.01,
    exposure_mult: float = 1.0,
    attenuation_radius_mult: float = 1.0,
) -> bpy.types.Light:
    """Create and configure Blender light data from static UE component props."""
    light_data = bpy.data.lights.new(name, light_type)
    apply_default_light_options(light_data, location_scale)
    apply_common_static_light_properties(
        light_data,
        props,
        location_scale,
        exposure_mult,
        attenuation_radius_mult,
    )
    if light_type == "SPOT":
        apply_spot_cone_properties(light_data, props)
    return light_data
