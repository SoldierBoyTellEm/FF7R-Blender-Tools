"""Generate the first live KineDriver layer on an audited Blender armature.

This stage creates TargetTranslate/TargetScale scalar drivers,
TargetBendSTRoll/TargetBendRoll/TargetRotate quaternion drivers, and explicit
TargetPoscns/TargetOricns world-space anchor drivers.  It supports the
source/effector combinations used by PC0000_00 and applies Maya-style segment
scale compensation by setting immediate children of KDI scale targets to
``inherit_scale = 'NONE'``.  KDI helper and physics chains are also hidden.
Every affected item is recorded for scoped cleanup.

Run from Blender's Text Editor, keep the audited armature active, and choose
the ``*_KDI_BLENDER_AUDIT.json`` produced by kdi_step1_audit.py.
"""

from __future__ import annotations

import json
import math
import traceback
import zlib
from pathlib import Path
from typing import Any

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import audit as kdi_audit


bl_info = {
    "name": "FF7R KineDriver Import",
    "author": "OpenAI Codex",
    "version": (0, 3, 0),
    "blender": (4, 0, 0),
    "location": "Search > KDI Step 2",
    "description": "Build audited KDI scalar, rotation, and anchor drivers",
    "category": "Animation",
}


SCRIPT_VERSION = "0.3.1"
REGISTRY_PROPERTY = "kdi_scalar_registry_json"

# Debug toggle for an unresolved naming question. The stereographic decomposition
# itself is settled -- our formulas match CEDEC slide 24 term for term -- but
# which of the two resulting angles the data calls BendS and which BendT is a
# labelling convention we inferred rather than confirmed. We read BendS as the
# up-axis angle and BendT as the cross-axis angle; a third-party reference reads
# them the other way round (though its phrasing is ambiguous). Our assignment is
# self-consistent between decomposition and recomposition, which is why rigs work,
# so this cannot be settled by reasoning -- only by looking at a shoulder or hip
# deform with it on and off. Enabling it swaps the interpretation on both sides at
# once, which is a pure relabel; it is NOT a no-op, because a BendS->BendS link
# then transports the cross-axis angle where it used to transport the up-axis one.
SWAP_BEND_ST_DESCRIPTION = (
    "Debug: reinterpret BendS as the cross-axis angle and BendT as the up-axis "
    "angle (the opposite of this add-on's default reading). Swaps both the source "
    "decomposition and the target recomposition together. Leave off unless you are "
    "specifically testing which convention matches the game"
)
GENERATED_PROPERTY = "kdi_scalar_generated_json"
SOURCE_PROPERTY = "kdi_scalar_source_audit"
KDI_BONE_COLLECTION = "KDI Helpers (Hidden)"
PHYSICS_BONE_COLLECTION = "Physics Bones (Hidden)"
LEAF_BONE_COLLECTION = "Leaf Bones (Hidden)"
RUNTIME: dict[int, dict[str, Any]] = {}


AXIS_ORDER_ITEMS = (
    ("XYZ", "X → X, Y → Y, Z → Z", "Use the direct KDI-to-Blender channel mapping"),
    ("XZY", "X → X, Y → Z, Z → Y", "Swap the Blender Y and Z target channels"),
    ("YXZ", "X → Y, Y → X, Z → Z", "Swap the Blender X and Y target channels"),
    ("YZX", "X → Y, Y → Z, Z → X", "Cycle the KDI X/Y/Z channels onto Blender Y/Z/X"),
    ("ZXY", "X → Z, Y → X, Z → Y", "Cycle the KDI X/Y/Z channels onto Blender Z/X/Y"),
    ("ZYX", "X → Z, Y → Y, Z → X", "Swap the Blender X and Z target channels"),
)

# The loose JSON KDI workflow is the reference frame used by this module.  A
# package skeleton made by skeleton/importer.py has the same heads and hierarchy
# as that armature, but after its intentional +90 degree bone roll its local
# axes are related by this constant basis matrix.  It maps package-local vectors
# into the loose-JSON/reference local frame.  The inverse is its transpose.
COORDINATE_PROFILE_REFERENCE = "REFERENCE"
COORDINATE_PROFILE_PACKAGE_SKELETON_ROLL_90 = "PACKAGE_SKELETON_ROLL_90"
PACKAGE_TO_REFERENCE_BASIS = (
    (0.0, 1.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
)


def coordinate_basis_to_reference(profile: str) -> list[list[float]]:
    if profile == COORDINATE_PROFILE_REFERENCE:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if profile == COORDINATE_PROFILE_PACKAGE_SKELETON_ROLL_90:
        return [list(row) for row in PACKAGE_TO_REFERENCE_BASIS]
    raise ValueError(f"Unsupported KDI coordinate profile: {profile!r}")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_quaternion(q: list[float]) -> list[float]:
    length = math.sqrt(sum(component * component for component in q))
    if length < 1.0e-12:
        return [1.0, 0.0, 0.0, 0.0]
    return [component / length for component in q]


def quaternion_rotate(q: list[float], v: list[float]) -> list[float]:
    w, x, y, z = normalize_quaternion(q)
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return [
        v[0] + w * tx + (y * tz - z * ty),
        v[1] + w * ty + (z * tx - x * tz),
        v[2] + w * tz + (x * ty - y * tx),
    ]


def weighted_quaternion(q: list[float], weight: float) -> list[float]:
    """Shortest-path identity-to-q interpolation used by weighted sources."""
    q = normalize_quaternion(q)
    if q[0] < 0.0:
        q = [-component for component in q]
    angle = math.acos(clamp(q[0], -1.0, 1.0))
    sine = math.sin(angle)
    if abs(sine) < 1.0e-10:
        return q
    scaled_angle = angle * weight
    factor = math.sin(scaled_angle) / sine
    return normalize_quaternion([
        math.cos(scaled_angle),
        q[1] * factor,
        q[2] * factor,
        q[3] * factor,
    ])


def multiply_quaternion(a: list[float], b: list[float]) -> list[float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return normalize_quaternion([
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ])


def axis_angle_quaternion(axis: list[float], angle: float) -> list[float]:
    axis = normalize3(axis)
    half = 0.5 * float(angle)
    sine = math.sin(half)
    return normalize_quaternion([
        math.cos(half),
        axis[0] * sine,
        axis[1] * sine,
        axis[2] * sine,
    ])


def shortest_arc_quaternion(source: list[float], target: list[float]) -> list[float]:
    source = normalize3(source)
    target = normalize3(target)
    cosine = clamp(dot3(source, target), -1.0, 1.0)
    if cosine < -1.0 + 1.0e-8:
        fallback = [1.0, 0.0, 0.0] if abs(source[0]) < 0.9 else [0.0, 1.0, 0.0]
        axis = normalize3(cross3(source, fallback))
        return [0.0, axis[0], axis[1], axis[2]]
    axis = cross3(source, target)
    return normalize_quaternion([1.0 + cosine, axis[0], axis[1], axis[2]])


def bending_quaternion(q: list[float], aim: list[float]) -> list[float]:
    return shortest_arc_quaternion(aim, quaternion_rotate(q, aim))


def compose_bend_roll(
    bend_s: float,
    bend_t: float,
    roll: float,
    aim: list[float],
    up: list[float],
    cross: list[float],
    reverse_order: bool,
) -> list[float]:
    """Recompose KineDriver's stereographic BendS/BendT plus axial Roll."""
    tangent_h = math.tan(-0.5 * float(bend_t))
    tangent_v = math.tan(0.5 * float(bend_s))
    scale = 2.0 / (tangent_h * tangent_h + tangent_v * tangent_v + 1.0)
    aimed = normalize3([
        (scale - 1.0) * aim[index]
        + scale * tangent_v * up[index]
        + scale * tangent_h * cross[index]
        for index in range(3)
    ])
    bend = shortest_arc_quaternion(aim, aimed)
    twist = axis_angle_quaternion(aim, roll)
    return multiply_quaternion(twist, bend) if reverse_order else multiply_quaternion(bend, twist)


def matrix3_to_quaternion(m: list[list[float]]) -> list[float]:
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        q = [
            0.25 * scale,
            (m[2][1] - m[1][2]) / scale,
            (m[0][2] - m[2][0]) / scale,
            (m[1][0] - m[0][1]) / scale,
        ]
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        scale = math.sqrt(max(0.0, 1.0 + m[0][0] - m[1][1] - m[2][2])) * 2.0
        q = [
            (m[2][1] - m[1][2]) / scale,
            0.25 * scale,
            (m[0][1] + m[1][0]) / scale,
            (m[0][2] + m[2][0]) / scale,
        ]
    elif m[1][1] > m[2][2]:
        scale = math.sqrt(max(0.0, 1.0 + m[1][1] - m[0][0] - m[2][2])) * 2.0
        q = [
            (m[0][2] - m[2][0]) / scale,
            (m[0][1] + m[1][0]) / scale,
            0.25 * scale,
            (m[1][2] + m[2][1]) / scale,
        ]
    else:
        scale = math.sqrt(max(0.0, 1.0 + m[2][2] - m[0][0] - m[1][1])) * 2.0
        q = [
            (m[1][0] - m[0][1]) / scale,
            (m[0][2] + m[2][0]) / scale,
            (m[1][2] + m[2][1]) / scale,
            0.25 * scale,
        ]
    return normalize_quaternion(q)


def transpose3(m: list[list[float]]) -> list[list[float]]:
    return [[m[column][row] for column in range(3)] for row in range(3)]


def multiply3(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][k] * b[k][column] for k in range(3)) for column in range(3)]
        for row in range(3)
    ]


def multiply3_vector(m: list[list[float]], v: list[float]) -> list[float]:
    return [sum(m[row][column] * v[column] for column in range(3)) for row in range(3)]


def source_vector_in_reference_frame(config: dict[str, Any], vector: list[float]) -> list[float]:
    """Convert a package-skeleton pose-space vector to the established KDI frame."""
    return multiply3_vector(
        coordinate_basis_to_reference(config.get("coordinate_profile", COORDINATE_PROFILE_REFERENCE)),
        vector,
    )


def source_quaternion_in_reference_frame(config: dict[str, Any], quaternion: list[float]) -> list[float]:
    """Express a pose-space rotation in the loose-JSON KDI reference frame."""
    basis = coordinate_basis_to_reference(config.get("coordinate_profile", COORDINATE_PROFILE_REFERENCE))
    rotation = quaternion_to_matrix3(quaternion)
    return matrix3_to_quaternion(multiply3(multiply3(basis, rotation), transpose3(basis)))


def reference_quaternion_in_target_frame(config: dict[str, Any], quaternion: list[float]) -> list[float]:
    """Convert a KDI result from the reference frame back to the target bone frame."""
    basis = coordinate_basis_to_reference(config.get("coordinate_profile", COORDINATE_PROFILE_REFERENCE))
    inverse_basis = transpose3(basis)
    rotation = quaternion_to_matrix3(quaternion)
    return matrix3_to_quaternion(multiply3(multiply3(inverse_basis, rotation), basis))


def dot3(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cross3(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def normalize3(value: list[float]) -> list[float]:
    length = math.sqrt(dot3(value, value)) or 1.0
    return [component / length for component in value]


def normalized_rows(values: tuple[float, ...] | list[float], offset: int = 0) -> list[list[float]]:
    """Extract an orthonormal rotation from nine row-major matrix values."""
    raw = [[float(values[offset + row * 3 + column]) for column in range(3)] for row in range(3)]
    column_x = normalize3([raw[row][0] for row in range(3)])
    raw_y = [raw[row][1] for row in range(3)]
    projection = dot3(raw_y, column_x)
    column_y = normalize3([raw_y[index] - projection * column_x[index] for index in range(3)])
    column_z = normalize3(cross3(column_x, column_y))
    raw_z = [raw[row][2] for row in range(3)]
    if dot3(column_z, raw_z) < 0.0:
        column_z = [-value for value in column_z]
    return [
        [column_x[row], column_y[row], column_z[row]] for row in range(3)
    ]


def converted_axis(body: dict[str, Any], key: str, fallback: tuple[float, float, float]) -> list[float]:
    value = body.get(key) or {"X": fallback[0], "Y": fallback[1], "Z": fallback[2]}
    # KDI/UE vector -> Blender vector: mirror Y.
    return [float(value.get("X", fallback[0])), -float(value.get("Y", fallback[1])), float(value.get("Z", fallback[2]))]


def rotation_parameter(config: dict[str, Any], q: list[float]) -> float:
    body = config["source_body"]
    parameter = config["source_parameter"]
    aim = converted_axis(body, "AimVector", (1.0, 0.0, 0.0))
    up = converted_axis(body, "UpVector", (0.0, 1.0, 0.0))
    cross = converted_axis(body, "CrossVector", (0.0, 0.0, 1.0))
    q = normalize_quaternion(q)
    aimed = quaternion_rotate(q, aim)
    denominator = sum(a * b for a, b in zip(aim, aimed)) + 1.0
    bend_from_cross = -2.0 * math.atan2(
        sum(a * b for a, b in zip(cross, aimed)),
        denominator,
    )
    bend_from_up = 2.0 * math.atan2(
        sum(a * b for a, b in zip(up, aimed)),
        denominator,
    )

    projection = sum(q[index + 1] * aim[index] for index in range(3))
    twist = normalize_quaternion([q[0], aim[0] * projection, aim[1] * projection, aim[2] * projection])
    roll = 2.0 * math.atan2(
        twist[1] * aim[0] + twist[2] * aim[1] + twist[3] * aim[2],
        twist[0],
    )
    if roll > math.pi:
        roll -= 2.0 * math.pi
    elif roll < -math.pi:
        roll += 2.0 * math.pi

    if config.get("swap_bend_st"):
        # Debug relabel: reinterpret which physical angle each channel name
        # denotes. See the note on SWAP_BEND_ST_DESCRIPTION. The target side
        # (kdi_rotation) swaps in step, so this stays a pure relabel.
        bend_from_up, bend_from_cross = bend_from_cross, bend_from_up

    if parameter == "BendS":
        return bend_from_up
    if parameter == "BendT":
        return bend_from_cross
    if parameter == "Roll":
        return roll
    if parameter == "BendingAngle":
        dot = clamp(sum(a * b for a, b in zip(aim, aimed)), -1.0, 1.0)
        return math.acos(dot)
    if parameter == "RotateAngle":
        return 2.0 * math.atan2(math.sqrt(sum(value * value for value in q[1:])), abs(q[0]))
    raise ValueError(f"Unsupported SourceRotate parameter: {parameter}")


def translation_parameter(config: dict[str, Any], value: list[float]) -> float:
    # Blender meters/right-handed -> KDI centimeters/UE handedness.
    converted = [value[0] * 100.0, -value[1] * 100.0, value[2] * 100.0]
    parameter = config["source_parameter"]
    if parameter == "TranslateX":
        return converted[0]
    if parameter == "TranslateY":
        return converted[1]
    if parameter == "TranslateZ":
        return converted[2]
    if parameter == "Distance":
        return math.sqrt(sum(component * component for component in converted))
    raise ValueError(f"Unsupported SourceTranslate parameter: {parameter}")


def bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    one_minus = 1.0 - t
    return (
        one_minus ** 3 * p0
        + 3.0 * one_minus ** 2 * t * p1
        + 3.0 * one_minus * t ** 2 * p2
        + t ** 3 * p3
    )


def apply_effector(config: dict[str, Any], value: float) -> float:
    value *= float(config.get("input_coefficient", 1.0))
    body = config["effector_body"]
    effector_type = config["effector_type"]
    if effector_type == "EffectorEZParamLinkLinear":
        result = value * float(body.get("Scale", 1.0)) + float(body.get("Offset", 0.0))
        if body.get("EnableMin"):
            result = max(result, float(body.get("ClampMin", result)))
        if body.get("EnableMax"):
            result = min(result, float(body.get("ClampMax", result)))
    elif effector_type == "EffectorEZParamLink":
        x0 = float(body["PX0"])
        dx0 = float(body["VX1_0"])
        dx1 = float(body["VX2_1"])
        x1 = x0 + dx0
        x2 = x1 + dx1
        if value <= x0:
            result = float(body["PY0"]) + float(body["Grad0"]) * (value - x0)
        elif value <= x1:
            t = (value - x0) / dx0 if abs(dx0) > 1.0e-12 else 0.0
            result = bezier(float(body["PY0"]), float(body["PY0A"]), float(body["PY0B"]), float(body["PY1"]), t)
        elif value <= x2:
            t = (value - x1) / dx1 if abs(dx1) > 1.0e-12 else 0.0
            result = bezier(float(body["PY1"]), float(body["PY1A"]), float(body["PY1B"]), float(body["PY2"]), t)
        else:
            result = float(body["PY2"]) + float(body["Grad1"]) * (value - x2)
        if body.get("ByCoef"):
            # The curve yields a dimensionless coefficient scaling the input
            # rather than an absolute output value -- see the note on
            # ByCoef above build_config. Multiply by the same (already
            # input_coefficient-scaled) value the curve was evaluated at, so the
            # wire's unit conversion cancels against output_coefficient.
            result *= value
    else:
        raise ValueError(f"Unsupported scalar effector: {effector_type}")
    return result * float(config.get("output_coefficient", 1.0))


def target_value(config: dict[str, Any], value: float) -> float:
    if config["target_type"] == "TargetScale":
        return value * float(config.get("target_axis_sign", 1.0))
    if config["target_type"] == "TargetTranslate":
        sign = -1.0 if config["target_parameter"] == "TranslateY" else 1.0
        return sign * float(config.get("target_axis_sign", 1.0)) * value * 0.01
    raise ValueError(f"Unsupported target type: {config['target_type']}")


def matrix4_identity() -> list[list[float]]:
    return [[1.0 if row == column else 0.0 for column in range(4)] for row in range(4)]


def multiply4(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][k] * b[k][column] for k in range(4)) for column in range(4)]
        for row in range(4)
    ]


def transform_point(matrix: list[list[float]], point: list[float]) -> list[float]:
    return [
        sum(matrix[row][column] * point[column] for column in range(3)) + matrix[row][3]
        for row in range(3)
    ]


def inverse3(matrix: list[list[float]]) -> list[list[float]]:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) < 1.0e-12:
        raise ValueError("Singular parent matrix in KDI anchor")
    inverse_det = 1.0 / determinant
    return [
        [(e * i - f * h) * inverse_det, (c * h - b * i) * inverse_det, (b * f - c * e) * inverse_det],
        [(f * g - d * i) * inverse_det, (a * i - c * g) * inverse_det, (c * d - a * f) * inverse_det],
        [(d * h - e * g) * inverse_det, (b * g - a * h) * inverse_det, (a * e - b * d) * inverse_det],
    ]


def inverse_transform_point(matrix: list[list[float]], point: list[float]) -> list[float]:
    rotation_scale = [[matrix[row][column] for column in range(3)] for row in range(3)]
    inverse_rotation_scale = inverse3(rotation_scale)
    translated = [point[row] - matrix[row][3] for row in range(3)]
    return multiply3_vector(inverse_rotation_scale, translated)


def weighted_quaternion_average(quaternions: list[list[float]], weights: list[float]) -> list[float]:
    if not quaternions:
        return [1.0, 0.0, 0.0, 0.0]
    reference = normalize_quaternion(quaternions[0])
    accumulated = [0.0, 0.0, 0.0, 0.0]
    for quaternion, weight in zip(quaternions, weights):
        quaternion = normalize_quaternion(quaternion)
        if sum(a * b for a, b in zip(reference, quaternion)) < 0.0:
            quaternion = [-value for value in quaternion]
        for index in range(4):
            accumulated[index] += float(weight) * quaternion[index]
    result = normalize_quaternion(accumulated)
    if result[0] < 0.0:
        result = [-value for value in result]
    return result


def values_to_matrix3(values: tuple[float, ...], offset: int) -> list[list[float]]:
    return normalized_rows(values, offset)


def values_to_matrix4(values: tuple[float, ...], offset: int) -> list[list[float]]:
    matrix = matrix4_identity()
    cursor = offset
    for row in range(3):
        for column in range(4):
            matrix[row][column] = float(values[cursor])
            cursor += 1
    return matrix


def kdi_anchor(config_id: int, component: int, *values: float) -> float:
    """Evaluate one position or orientation component of a maintain-offset anchor."""
    try:
        config = RUNTIME[int(config_id)]
        source_count = len(config["weights"])
        weights = [float(weight) for weight in config["weights"]]
        weight_sum = sum(weights) or 1.0
        weights = [weight / weight_sum for weight in weights]
        cursor = 0
        if config["anchor_type"] == "POSITION":
            candidates = []
            for source_index in range(source_count):
                source_position = [float(values[cursor + index]) for index in range(3)]
                cursor += 3
                if config["position_reads_source_matrix"]:
                    raw_source_matrix = [
                        [float(values[cursor + row * 3 + column]) for column in range(3)]
                        for row in range(3)
                    ]
                    cursor += 9
                else:
                    raw_source_matrix = config["source_rest_rotations"][source_index]
                if config["orient_affect"]:
                    offset_transform = (
                        raw_source_matrix
                        if config["scale_affect"]
                        else normalized_rows([value for row in raw_source_matrix for value in row])
                    )
                elif config["scale_affect"]:
                    rest_rotation = config["source_rest_rotations"][source_index]
                    scales = [
                        math.sqrt(sum(raw_source_matrix[row][column] ** 2 for row in range(3)))
                        for column in range(3)
                    ]
                    offset_transform = [
                        [rest_rotation[row][column] * scales[column] for column in range(3)]
                        for row in range(3)
                    ]
                else:
                    offset_transform = config["source_rest_rotations"][source_index]
                rotated_offset = multiply3_vector(
                    offset_transform,
                    config["source_local_offsets"][source_index],
                )
                candidates.append([
                    source_position[axis] + rotated_offset[axis] for axis in range(3)
                ])
            desired_position = [
                sum(weights[index] * candidates[index][axis] for index in range(source_count))
                for axis in range(3)
            ]
            parent_matrix = values_to_matrix4(values, cursor) if config.get("parent_bone") else matrix4_identity()
            base_matrix = multiply4(parent_matrix, config["rest_local"])
            basis_location = inverse_transform_point(base_matrix, desired_position)
            return basis_location[int(component)]

        candidates = []
        for offset_matrix in config["offset_matrices"]:
            source_rotation = values_to_matrix3(values, cursor)
            cursor += 9
            offset_rotation = [[offset_matrix[row][column] for column in range(3)] for row in range(3)]
            candidates.append(matrix3_to_quaternion(multiply3(source_rotation, offset_rotation)))
        desired_quaternion = weighted_quaternion_average(candidates, weights)
        desired_rotation = quaternion_to_matrix3(desired_quaternion)
        if config.get("parent_bone"):
            parent_rotation = values_to_matrix3(values, cursor)
        else:
            parent_rotation = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        rest_rotation = [[config["rest_local"][row][column] for column in range(3)] for row in range(3)]
        basis_rotation = multiply3(transpose3(multiply3(parent_rotation, rest_rotation)), desired_rotation)
        basis_quaternion = matrix3_to_quaternion(basis_rotation)
        return basis_quaternion[int(component)]
    except Exception:
        return 0.0


def quaternion_to_matrix3(q: list[float]) -> list[list[float]]:
    w, x, y, z = normalize_quaternion(q)
    return [
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
    ]


def config_source_bones(config: dict[str, Any]) -> list[str]:
    """The source bones of a scalar config, newest key first.

    Registries written before multi-source support only carry the singular
    ``source_bone``/``source_weight``, so fall back to those: an existing .blend
    keeps evaluating exactly as it did.
    """
    bones = config.get("source_bones")
    if bones:
        return list(bones)
    return [config["source_bone"]]


def config_source_weights(config: dict[str, Any]) -> list[float]:
    weights = config.get("source_weights")
    if weights:
        return [float(weight) for weight in weights]
    return [float(config.get("source_weight", 1.0))]


def blend_weights(weights: list[float]) -> tuple[list[float], float]:
    """Split authored weights into a normalized mix and an overall magnitude.

    KineDriver's weights carry two meanings at once. Their *ratio* picks the
    blend between sources, and their *total* scales how much of the resulting
    rotation is applied -- a lone source at 0.25 yields a quarter of its
    rotation. The corpus authors both styles: 134 multi-source nodes have
    weights summing to 1.0 (true skin-style weights, e.g. [0.2, 0.8]) while 133
    are all-ones summing to 2.0/3.0/4.0, which plainly means "average these
    equally" rather than "apply it three times". Clamping the magnitude at 1.0
    satisfies both, and since no single-source weight in the corpus exceeds 1.0
    it reproduces the previous single-source behaviour exactly.
    """
    total = sum(weights)
    if total <= 0.0:
        count = len(weights) or 1
        return [1.0 / count] * count, 0.0
    return [weight / total for weight in weights], min(total, 1.0)


def source_value_count(config: dict[str, Any]) -> int:
    count = len(config_source_bones(config))
    # Per source, plus the base-space bone's data once where the mode needs it.
    return {
        "PARENT_ROTATION": 4 * count,
        "NODE_ROTATION": 9 * count + 9,
        "PARENT_TRANSLATION": 3 * count,
        "NODE_TRANSLATION": 3 * count + 12,
    }[config["source_mode"]]


def source_rotation_delta(config: dict[str, Any], values: tuple[float, ...] | list[float]) -> list[float]:
    """Blend every source bone's rotation delta into the node's single output.

    A Source operator exposes one set of output ports, so it must resolve its
    sources into one rotation before decomposing into BendS/BendT/Roll -- the
    blend happens here, on the transforms, not afterwards on the decomposed
    angles (which would not be equivalent, the decomposition being non-linear).
    """
    mode = config["source_mode"]
    count = len(config_source_bones(config))
    mix, magnitude = blend_weights(config_source_weights(config))

    if mode == "PARENT_ROTATION":
        deltas = [list(values[index * 4:index * 4 + 4]) for index in range(count)]
    elif mode == "NODE_ROTATION":
        base_matrix = normalized_rows(values, 9 * count)
        rest_relatives = config.get("rest_relative_rotations") or [config["rest_relative_rotation"]]
        deltas = []
        for index in range(count):
            source_matrix = normalized_rows(values, 9 * index)
            current_relative = multiply3(transpose3(base_matrix), source_matrix)
            rest_relative = rest_relatives[min(index, len(rest_relatives) - 1)]
            deltas.append(matrix3_to_quaternion(multiply3(transpose3(rest_relative), current_relative)))
    else:
        raise ValueError(f"Source mode {mode} does not produce a quaternion")

    blended = deltas[0] if count == 1 else weighted_quaternion_average(deltas, mix)
    return source_quaternion_in_reference_frame(config, weighted_quaternion(blended, magnitude))


def blended_source_vector(
        config: dict[str, Any],
        per_source: list[list[float]],
) -> list[float]:
    """Weighted average of each source's translation delta, scaled by magnitude."""
    mix, magnitude = blend_weights(config_source_weights(config))
    return [
        magnitude * sum(mix[index] * per_source[index][axis] for index in range(len(per_source)))
        for axis in range(3)
    ]


def scalar_source_value(config: dict[str, Any], values: tuple[float, ...] | list[float]) -> float:
    mode = config["source_mode"]
    if mode in {"PARENT_ROTATION", "NODE_ROTATION"}:
        return rotation_parameter(config, source_rotation_delta(config, values))
    count = len(config_source_bones(config))
    if mode == "PARENT_TRANSLATION":
        per_source = [list(values[index * 3:index * 3 + 3]) for index in range(count)]
        return translation_parameter(
            config,
            source_vector_in_reference_frame(config, blended_source_vector(config, per_source)),
        )
    if mode == "NODE_TRANSLATION":
        base_position = list(values[3 * count:3 * count + 3])
        base_matrix = normalized_rows(values, 3 * count + 3)
        per_source = []
        for index in range(count):
            source_position = list(values[index * 3:index * 3 + 3])
            delta = [source_position[axis] - base_position[axis] for axis in range(3)]
            per_source.append(multiply3_vector(transpose3(base_matrix), delta))
        return translation_parameter(
            config,
            source_vector_in_reference_frame(config, blended_source_vector(config, per_source)),
        )
    raise ValueError(f"Unsupported source mode: {mode}")


def kdi_rotation(config_id: int, component: int, *values: float) -> float:
    """Evaluate one quaternion component of a KineDriver rotation target."""
    try:
        config = RUNTIME[int(config_id)]
        channels = dict(config["defaults"])
        cursor = 0
        for scalar_config in config["scalar_inputs"]:
            count = source_value_count(scalar_config)
            source_value = scalar_source_value(scalar_config, values[cursor:cursor + count])
            cursor += count
            channels[scalar_config["target_parameter"]] = apply_effector(scalar_config, source_value)

        rotation_type = config["rotation_type"]
        if rotation_type == "TargetBendSTRoll":
            target_body = config["target_body"]
            bend_s = channels.get("BendS", 0.0)
            bend_t = channels.get("BendT", 0.0)
            if config.get("swap_bend_st"):
                # compose_bend_roll's first argument is always the up-axis angle
                # and its second the cross-axis angle, so under the swapped
                # labelling the channels feed the opposite slots.
                bend_s, bend_t = bend_t, bend_s
            quaternion = compose_bend_roll(
                bend_s,
                bend_t,
                channels.get("Roll", 0.0),
                converted_axis(target_body, "AimVector", (1.0, 0.0, 0.0)),
                converted_axis(target_body, "UpVector", (0.0, 1.0, 0.0)),
                converted_axis(target_body, "CrossVector", (0.0, 0.0, 1.0)),
                bool(target_body.get("ReverseOrder", False)),
            )
        else:
            direct_config = config.get("direct_source")
            direct_quaternion = [1.0, 0.0, 0.0, 0.0]
            if direct_config:
                count = source_value_count(direct_config)
                source_quaternion = source_rotation_delta(direct_config, values[cursor:cursor + count])
                cursor += count
                if rotation_type == "TargetBendRoll":
                    source_aim = converted_axis(direct_config["source_body"], "AimVector", (1.0, 0.0, 0.0))
                    source_quaternion = bending_quaternion(source_quaternion, source_aim)
                direct_quaternion = weighted_quaternion(source_quaternion, config["quaternion_weight"])

            if rotation_type == "TargetRotate":
                quaternion = direct_quaternion
            elif rotation_type == "TargetBendRoll":
                target_body = config["target_body"]
                twist = axis_angle_quaternion(
                    converted_axis(target_body, "AimVector", (1.0, 0.0, 0.0)),
                    channels.get("Roll", 0.0),
                )
                quaternion = (
                    multiply_quaternion(twist, direct_quaternion)
                    if target_body.get("ReverseOrder", False)
                    else multiply_quaternion(direct_quaternion, twist)
                )
            else:
                raise ValueError(f"Unsupported rotation target: {rotation_type}")
        quaternion = reference_quaternion_in_target_frame(config, quaternion)
        quaternion = normalize_quaternion(quaternion)
        if quaternion[0] < 0.0:
            quaternion = [-value for value in quaternion]
        return quaternion[int(component)]
    except Exception:
        return 1.0 if int(component) == 0 else 0.0


def kdi_scalar(config_id: int, *values: float) -> float:
    """Short driver-namespace entry point used by every generated FCurve."""
    try:
        config = RUNTIME[int(config_id)]
        source_value = scalar_source_value(config, values)
        return target_value(config, apply_effector(config, source_value))
    except Exception:
        # Driver evaluation must remain numeric; setup preflight reports details.
        return 0.0


def install_runtime_namespace() -> None:
    """Register driver-callable functions even while Blender data is restricted."""
    bpy.app.driver_namespace["kdi_scalar"] = kdi_scalar
    bpy.app.driver_namespace["kdi_anchor"] = kdi_anchor
    bpy.app.driver_namespace["kdi_rotation"] = kdi_rotation


def load_runtime_from_armatures() -> bool:
    """Load saved KDI configs when the current Blender file data is available.

    Blender exposes a restricted ``bpy.data`` while enabling add-ons from its
    preferences window.  The namespace is still safe to install at that point,
    but armatures must be scanned later via the timer below.
    """
    RUNTIME.clear()
    objects = getattr(bpy.data, "objects", None)
    install_runtime_namespace()
    if objects is None:
        return False
    for obj in objects:
        if obj.type != "ARMATURE" or REGISTRY_PROPERTY not in obj:
            continue
        try:
            payload = json.loads(obj[REGISTRY_PROPERTY])
            for config in payload.get("configs", []):
                RUNTIME[int(config["id"])] = config
        except Exception:
            traceback.print_exc()
    return True


def deferred_runtime_load() -> float | None:
    """Retry after add-on enable once Blender has released restricted data."""
    return None if load_runtime_from_armatures() else 0.1


def schedule_runtime_load() -> None:
    if not load_runtime_from_armatures() and not bpy.app.timers.is_registered(deferred_runtime_load):
        bpy.app.timers.register(deferred_runtime_load, first_interval=0.0)


@persistent
def kdi_scalar_load_post(_unused: Any) -> None:
    schedule_runtime_load()


def escaped_bone_path(name: str, property_name: str) -> str:
    return f"pose.bones[{json.dumps(name)}].{property_name}"


def pose_matrix_component_path(name: str, row: int, column: int) -> str:
    """Return an RNA path for a semantic row/column of PoseBone.matrix.

    Blender's nested matrix data paths are column-major even though Python
    Matrix access is row-major, so the two indices must be reversed here.
    """
    return escaped_bone_path(name, f"matrix[{column}][{row}]")


def add_single_property_variable(driver: Any, name: str, armature: Any, data_path: str) -> None:
    variable = driver.variables.new()
    variable.name = name
    variable.type = "SINGLE_PROP"
    variable.targets[0].id = armature
    variable.targets[0].data_path = data_path


def add_matrix_variables(driver: Any, armature: Any, bone_name: str, prefix: str, rotation_only: bool) -> list[str]:
    names = []
    if not rotation_only:
        for row, suffix in enumerate(("x", "y", "z")):
            name = f"{prefix}p{suffix}"
            add_single_property_variable(driver, name, armature, pose_matrix_component_path(bone_name, row, 3))
            names.append(name)
    for row in range(3):
        for column in range(3):
            name = f"{prefix}{row}{column}"
            add_single_property_variable(driver, name, armature, pose_matrix_component_path(bone_name, row, column))
            names.append(name)
    return names


def matrix3_from_matrix4(matrix: Any) -> list[list[float]]:
    return [[float(matrix[row][column]) for column in range(3)] for row in range(3)]


def matrix4_from_blender(matrix: Any) -> list[list[float]]:
    return [[float(matrix[row][column]) for column in range(4)] for row in range(4)]


def build_source_config(source_node: dict[str, Any], armature: Any) -> dict[str, Any]:
    source_body = source_node["body"]
    source_bones = list(source_body.get("SourceBoneNameArray") or [])
    weights = [float(weight) for weight in (source_body.get("WeightArray") or [])]
    if not source_bones:
        raise ValueError(f"Source node {source_node['operator_index']} names no source bone")
    if len(weights) != len(source_bones):
        # Weights are optional in principle; an equal mix is the only sane
        # reading when the arrays disagree, and it matches the all-ones
        # authoring style that half the corpus's multi-source nodes use.
        weights = [1.0] * len(source_bones)
    if source_body.get("MirrorParams", {}).get("EnableMirroring"):
        raise ValueError(f"Scalar stage does not yet support mirrored source node {source_node['operator_index']}")
    if source_body.get("ReverseOrder"):
        raise ValueError(f"Scalar stage does not yet support ReverseOrder source node {source_node['operator_index']}")
    base_info = source_body.get("BaseSpaceInfo") or {}
    base_type = str(base_info.get("BaseSpaceType", "PARENT")).rsplit("_", 1)[-1]
    source_type = source_node["operator_type"]
    source_mode = f"{base_type}_{'ROTATION' if source_type == 'SourceRotate' else 'TRANSLATION'}"
    config = {
        "source_mode": source_mode,
        "source_type": source_type,
        "source_bones": source_bones,
        "source_weights": weights,
        # Retained so a registry written by this version stays readable by the
        # single-source accessors, and vice versa.
        "source_bone": source_bones[0],
        "source_weight": weights[0],
        "base_bone": base_info.get("BoneName"),
        "source_body": source_body,
        "source_operator_index": source_node["operator_index"],
    }
    if source_mode.startswith("NODE_") and not config["base_bone"]:
        raise ValueError(f"Source node {source_node['operator_index']} has NODE base space without a base bone")
    if source_mode == "NODE_ROTATION":
        base_rest = armature.data.bones[config["base_bone"]].matrix_local.to_3x3()
        rest_relatives = [
            matrix3_from_matrix4(base_rest.inverted() @ armature.data.bones[bone].matrix_local.to_3x3())
            for bone in source_bones
        ]
        config["rest_relative_rotations"] = rest_relatives
        config["rest_relative_rotation"] = rest_relatives[0]
    return config


def build_config(link: dict[str, Any], node_by_index: dict[int, dict[str, Any]], armature: Any, config_id: int) -> dict[str, Any]:
    source_node = node_by_index[int(link["source"]["operator_index"])]
    effector_node = node_by_index[int(link["effector_operator_index"])]
    target_node = node_by_index[int(link["target"]["operator_index"])]
    target_body = target_node["body"]
    if effector_node["operator_type"] not in {"EffectorEZParamLink", "EffectorEZParamLinkLinear"}:
        raise ValueError(f"Unsupported scalar effector {effector_node['operator_type']}")
    # ByCoef makes the effector's curve return a coefficient that scales its own
    # input (output = input * curve(input)) instead of an absolute output value.
    # Inferred from the corpus rather than from documentation, but the evidence
    # is uniform across all 932 occurrences in 239 of 531 sampled KDI files:
    # every one is same-channel (BendS->BendS or BendT->BendT), every one has
    # input_coefficient * output_coefficient == 1.0 exactly (so the effector is
    # operating on a unitless ratio), and every curve value lies within
    # [-1.0, 0.6] -- fractions, never the absolute angles seen elsewhere (which
    # reach 600). The rigs read correctly too: the usual shape is
    # "L_UpperArm_a BendS -> L_Bust_Spo BendS at 0.3", i.e. secondary motion
    # following its driver at a fraction of the angle. The flag never appears on
    # EffectorEZParamLinkLinear (0 of 18,036 bodies), hence it is handled only in
    # the Bezier branch of apply_effector.
    if target_node["operator_type"] == "TargetScale":
        if target_body.get("InputAsLogarithm") or target_body.get("ClampZero"):
            raise ValueError(f"Unsupported special scale mode on target node {target_node['operator_index']}")
    config = build_source_config(source_node, armature)
    config.update({
        "id": config_id,
        "source_parameter": link["source"]["parameter"],
        "effector_type": effector_node["operator_type"],
        "effector_body": effector_node["body"],
        "input_coefficient": link.get("input_coefficient", 1.0),
        "output_coefficient": link.get("output_coefficient", 1.0),
        "target_type": target_node["operator_type"],
        "target_bone": link["target_bone"],
        "target_parameter": link["target"]["parameter"],
        "target_operator_index": target_node["operator_index"],
    })
    return config


def config_identifier(audit: dict[str, Any], link: dict[str, Any], occupied: set[int]) -> int:
    token = ":".join((
        audit["graph"]["source"]["sha256"],
        str(link["effector_operator_index"]),
        str(link["target"]["operator_index"]),
        str(link["target"]["parameter"]),
    ))
    identifier = zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF
    while identifier in occupied:
        identifier = (identifier + 1) & 0x7FFFFFFF
    occupied.add(identifier)
    return identifier


def anchor_identifier(audit: dict[str, Any], node: dict[str, Any], occupied: set[int]) -> int:
    token = ":".join((
        audit["graph"]["source"]["sha256"],
        "ANCHOR",
        str(node["operator_index"]),
        str(node["operator_type"]),
    ))
    identifier = zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF
    while identifier in occupied:
        identifier = (identifier + 1) & 0x7FFFFFFF
    occupied.add(identifier)
    return identifier


def rotation_identifier(audit: dict[str, Any], node: dict[str, Any], occupied: set[int]) -> int:
    token = ":".join((
        audit["graph"]["source"]["sha256"],
        "ROTATION",
        str(node["operator_index"]),
        str(node["operator_type"]),
    ))
    identifier = zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF
    while identifier in occupied:
        identifier = (identifier + 1) & 0x7FFFFFFF
    occupied.add(identifier)
    return identifier


def build_rotation_config(
    node: dict[str, Any],
    incoming_links: list[dict[str, Any]],
    node_by_index: dict[int, dict[str, Any]],
    armature: Any,
    config_id: int,
) -> dict[str, Any]:
    body = node["body"]
    rotation_type = node["operator_type"]
    target_name = body["TargetObjectBoneName"]
    base_info = body.get("BaseSpaceInfo") or {}
    base_type = str(base_info.get("BaseSpaceType", "PARENT")).rsplit("_", 1)[-1]
    if base_type != "PARENT":
        raise ValueError(f"Rotation target {node['operator_index']} uses unsupported {base_type} base space")
    if body.get("MirrorParams", {}).get("EnableMirroring"):
        raise ValueError(f"Rotation target {node['operator_index']} uses unsupported mirroring")
    if body.get("AsQuatAngle"):
        raise ValueError(f"Rotation target {node['operator_index']} uses unsupported AsQuatAngle")

    allowed_parameters = {
        "TargetBendSTRoll": {"BendS", "BendT", "Roll"},
        "TargetBendRoll": {"Roll"},
        "TargetRotate": set(),
    }[rotation_type]
    scalar_inputs = []
    seen_parameters = set()
    for link in sorted(incoming_links, key=lambda item: item["target"]["parameter"]):
        parameter = link["target"]["parameter"]
        if parameter not in allowed_parameters:
            raise ValueError(
                f"Rotation target {node['operator_index']} has unsupported scalar input {parameter}"
            )
        if parameter in seen_parameters:
            raise ValueError(f"Rotation target {node['operator_index']} has duplicate input {parameter}")
        seen_parameters.add(parameter)
        scalar_inputs.append(build_config(link, node_by_index, armature, -1))

    source_quat_index = int(body.get("SourceQuat", -1))
    direct_source = None
    if source_quat_index >= 0:
        source_node = node_by_index.get(source_quat_index)
        if not source_node or source_node["operator_type"] != "SourceRotate":
            raise ValueError(f"Rotation target {node['operator_index']} has invalid SourceQuat {source_quat_index}")
        direct_source = build_source_config(source_node, armature)
    if rotation_type == "TargetRotate" and direct_source is None:
        raise ValueError(f"TargetRotate {node['operator_index']} has no quaternion source")
    if rotation_type == "TargetBendSTRoll" and direct_source is not None:
        raise ValueError(f"TargetBendSTRoll {node['operator_index']} unexpectedly has a quaternion source")

    return {
        "id": config_id,
        "rotation_type": rotation_type,
        "target_operator_index": node["operator_index"],
        "target_bone": target_name,
        "target_body": body,
        "scalar_inputs": scalar_inputs,
        "direct_source": direct_source,
        "quaternion_weight": float(body.get("QuatWeight", 1.0)),
        "defaults": {
            parameter: float(body.get(parameter, 0.0))
            for parameter in ("BendS", "BendT", "Roll")
        },
    }


def build_anchor_config(node: dict[str, Any], armature: Any, config_id: int) -> dict[str, Any]:
    body = node["body"]
    target_name = body["TargetObjectBoneName"]
    source_names = list(body.get("SourceBoneNameArray") or [])
    weights = [float(value) for value in (body.get("WeightArray") or [])]
    if not source_names or len(source_names) != len(weights):
        raise ValueError(f"Invalid anchor sources/weights on operator {node['operator_index']}")
    target_bone = armature.data.bones[target_name]
    target_rest = target_bone.matrix_local.copy()
    parent = target_bone.parent
    rest_local = parent.matrix_local.inverted() @ target_rest if parent else target_rest.copy()
    offsets = []
    source_local_offsets = []
    source_rest_rotations = []
    for source_name in source_names:
        source_rest = armature.data.bones[source_name].matrix_local
        # Source-local maintain offset: current_source @ offset = target.
        # Applying this on the right correctly cancels motion shared by the
        # source and target's actual armature parent.
        offsets.append(matrix4_from_blender(source_rest.inverted() @ target_rest))
        source_local_offsets.append([
            float(value) for value in (source_rest.inverted() @ target_rest.translation)
        ])
        source_rest_rotations.append(matrix3_from_matrix4(source_rest.to_3x3()))
    orient_affect = bool(body.get("OrientAffect", False))
    scale_affect = bool(body.get("ScaleAffect", False))
    return {
        "id": config_id,
        "anchor_type": "POSITION" if node["operator_type"] == "TargetPoscns" else "ORIENTATION",
        "target_operator_index": node["operator_index"],
        "target_bone": target_name,
        "parent_bone": parent.name if parent else None,
        "source_bones": source_names,
        "weights": weights,
        "offset_matrices": offsets,
        "source_local_offsets": source_local_offsets,
        "source_rest_rotations": source_rest_rotations,
        "orient_affect": orient_affect,
        "scale_affect": scale_affect,
        "position_reads_source_matrix": orient_affect or scale_affect,
        "rest_local": matrix4_from_blender(rest_local),
    }


def target_path_and_index(config: dict[str, Any]) -> tuple[str, int]:
    parameter = config["target_parameter"]
    source_axis = parameter[-1]
    axis_order = config.get("target_axis_order", "XYZ")
    if sorted(axis_order) != ["X", "Y", "Z"]:
        raise ValueError(f"Invalid target axis order: {axis_order!r}")
    target_axis = axis_order["XYZ".index(source_axis)]
    axis = {"X": 0, "Y": 1, "Z": 2}[target_axis]
    prop = "location" if config["target_type"] == "TargetTranslate" else "scale"
    return escaped_bone_path(config["target_bone"], prop), axis


VARIABLE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def add_ordered_variables(driver: Any, armature: Any, data_paths: list[str]) -> list[str]:
    """One driver variable per distinct property; names returned in call order.

    The binding constraint on a generated driver is Blender's ~256 character
    limit on the expression itself, so both halves of this matter:

    - **Deduplicating.** A rotation target reads the same bones from several of
      its scalar inputs -- most often the shared base-space bone's whole matrix.
      Repeating a name in the argument list is free, so the values the evaluator
      slices stay identical while the distinct-variable count collapses, which
      in turn keeps every name inside the single-character alphabet.
    - **Single-character names.** Multi-source reads cost 9 values per source,
      and Tifa's heaviest rotation target takes 81 arguments; as ``v10,v11,...``
      that alone would overrun the limit, where ``a,b,c,...`` fits.
    """
    assigned: dict[str, str] = {}
    names: list[str] = []
    for data_path in data_paths:
        name = assigned.get(data_path)
        if name is None:
            index = len(assigned)
            # Two-character fallbacks cannot collide with the one-character names.
            name = (
                VARIABLE_ALPHABET[index] if index < len(VARIABLE_ALPHABET)
                else f"v{index - len(VARIABLE_ALPHABET)}"
            )
            assigned[data_path] = name
            add_single_property_variable(driver, name, armature, data_path)
        names.append(name)
    return names


def add_source_variables(driver: Any, config: dict[str, Any], armature: Any) -> list[str]:
    """Create this config's driver variables, in ``source_data_paths`` order.

    Generated straight from that function so the variable order cannot drift
    from the order ``scalar_source_value`` slices ``values`` in -- which the
    per-mode branches this replaced had to keep in sync by hand, and which
    multi-source made considerably easier to get wrong.
    """
    return add_ordered_variables(driver, armature, source_data_paths(config))


def source_data_paths(config: dict[str, Any]) -> list[str]:
    """Driver data paths for a scalar config, in the order the evaluator slices.

    Layout is every source's own data first, then the shared base-space bone's,
    matching ``source_value_count`` and the ``values`` indexing in
    ``source_rotation_delta``/``scalar_source_value``.
    """
    sources = config_source_bones(config)
    mode = config["source_mode"]
    if mode == "PARENT_ROTATION":
        return [
            escaped_bone_path(source, f"rotation_quaternion[{index}]")
            for source in sources
            for index in range(4)
        ]
    if mode == "PARENT_TRANSLATION":
        return [
            escaped_bone_path(source, f"location[{index}]")
            for source in sources
            for index in range(3)
        ]
    if mode == "NODE_ROTATION":
        return [
            pose_matrix_component_path(bone_name, row, column)
            for bone_name in (*sources, config["base_bone"])
            for row in range(3)
            for column in range(3)
        ]
    if mode == "NODE_TRANSLATION":
        base = config["base_bone"]
        return (
            [pose_matrix_component_path(source, row, 3) for source in sources for row in range(3)]
            + [pose_matrix_component_path(base, row, 3) for row in range(3)]
            + [pose_matrix_component_path(base, row, column) for row in range(3) for column in range(3)]
        )
    raise ValueError(f"Unsupported source mode {mode}")


def add_rotation_variables(driver: Any, config: dict[str, Any], armature: Any) -> list[str]:
    source_configs = list(config["scalar_inputs"])
    if config.get("direct_source"):
        source_configs.append(config["direct_source"])
    data_paths = [
        data_path
        for source_config in source_configs
        for data_path in source_data_paths(source_config)
    ]
    return add_ordered_variables(driver, armature, data_paths)


def add_anchor_matrix_component(
    driver: Any,
    armature: Any,
    bone_name: str,
    row: int,
    column: int,
    names: list[str],
) -> None:
    index = len(names)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    name = alphabet[index] if index < len(alphabet) else f"a{index - len(alphabet)}"
    add_single_property_variable(
        driver,
        name,
        armature,
        pose_matrix_component_path(bone_name, row, column),
    )
    names.append(name)


def add_anchor_variables(driver: Any, config: dict[str, Any], armature: Any) -> list[str]:
    names: list[str] = []
    if config["anchor_type"] == "POSITION":
        for source_name in config["source_bones"]:
            for row in range(3):
                add_anchor_matrix_component(driver, armature, source_name, row, 3, names)
            if config["position_reads_source_matrix"]:
                for row in range(3):
                    for column in range(3):
                        add_anchor_matrix_component(driver, armature, source_name, row, column, names)
        if config.get("parent_bone"):
            for row in range(3):
                for column in range(4):
                    add_anchor_matrix_component(driver, armature, config["parent_bone"], row, column, names)
    else:
        for source_name in config["source_bones"]:
            for row in range(3):
                for column in range(3):
                    add_anchor_matrix_component(driver, armature, source_name, row, column, names)
        if config.get("parent_bone"):
            for row in range(3):
                for column in range(3):
                    add_anchor_matrix_component(driver, armature, config["parent_bone"], row, column, names)
    return names


def anchor_channels(config: dict[str, Any]) -> list[tuple[str, int]]:
    property_name = "location" if config["anchor_type"] == "POSITION" else "rotation_quaternion"
    component_count = 3 if config["anchor_type"] == "POSITION" else 4
    path = escaped_bone_path(config["target_bone"], property_name)
    return [(path, component) for component in range(component_count)]


def rotation_channels(config: dict[str, Any]) -> list[tuple[str, int]]:
    path = escaped_bone_path(config["target_bone"], "rotation_quaternion")
    return [(path, component) for component in range(4)]


def remove_generated(armature: Any) -> tuple[int, int]:
    removed = 0
    restored = 0
    if GENERATED_PROPERTY in armature:
        payload = json.loads(armature[GENERATED_PROPERTY])
        for item in payload.get("drivers", []):
            try:
                armature.driver_remove(item["data_path"], int(item["array_index"]))
                removed += 1
            except (TypeError, RuntimeError):
                pass
        compensated_children = payload.get("scale_compensated_children", [])
        for bone_name in compensated_children:
            bone = armature.data.bones.get(bone_name)
            if bone:
                # This importer is intentionally scoped to clean UE imports,
                # whose initial inheritance mode is uniformly FULL.
                bone.inherit_scale = "FULL"
                restored += 1
        for bone_name in payload.get("hidden_helper_bones", []):
            bone = armature.data.bones.get(bone_name)
            if bone:
                bone.hide = False
        for collection_name in payload.get("hidden_bone_collections", []):
            collection = armature.data.collections.get(collection_name)
            if collection:
                armature.data.collections.remove(collection)
    for prop in (REGISTRY_PROPERTY, GENERATED_PROPERTY, SOURCE_PROPERTY):
        if prop in armature:
            del armature[prop]
    schedule_runtime_load()
    return removed, restored


def apply_scale_compensation(armature: Any, node_by_index: dict[int, dict[str, Any]]) -> list[str]:
    compensated: set[str] = set()
    targets = {
        node["body"]["TargetObjectBoneName"]
        for node in node_by_index.values()
        if node["operator_type"] == "TargetScale"
        and node.get("body", {}).get("SegmentScaleCompensate")
    }
    for target_name in targets:
        target = armature.data.bones.get(target_name)
        if not target:
            continue
        for child in target.children:
            child.inherit_scale = "NONE"
            compensated.add(child.name)
    return sorted(compensated)


def descendants(armature: Any, seeds: set[str]) -> set[str]:
    result = set(seeds)
    stack = [armature.data.bones[name] for name in seeds]
    while stack:
        bone = stack.pop()
        for child in bone.children:
            if child.name not in result:
                result.add(child.name)
                stack.append(child)
    return result


def get_or_create_bone_collection(armature: Any, name: str) -> Any:
    collection = armature.data.collections.get(name)
    if collection is None:
        collection = armature.data.collections.new(name)
    collection.is_solo = False
    return collection


def move_bones_to_collection(armature: Any, bone_names: set[str], collection: Any) -> None:
    for bone_name in bone_names:
        bone = armature.data.bones[bone_name]
        # A bone remains visible when it belongs to any visible collection,
        # so this must be a move rather than an additional assignment.
        for old_collection in list(bone.collections):
            old_collection.unassign(bone)
        collection.assign(bone)
        bone.hide = True


def hide_noninteractive_bones(armature: Any, node_by_index: dict[int, dict[str, Any]]) -> dict[str, list[str]]:
    """Move procedural, physics, and terminal bones into hidden collections."""
    target_bones = {
        node.get("body", {}).get("TargetObjectBoneName")
        for node in node_by_index.values()
        if node["operator_type"].startswith("Target")
    }
    target_bones.discard(None)
    kdi_seeds = {
        bone.name
        for bone in armature.data.bones
        if bone.name in target_bones
        or bone.name == "C_KDIRoot"
        or bone.name.endswith("Kdi")
        or bone.name.endswith("_Spo")
    }
    physics_seeds = {
        bone.name for bone in armature.data.bones if bone.name.endswith("_Phy")
    }
    leaf_bones = {bone.name for bone in armature.data.bones if bone.name.endswith("_End")}
    physics_bones = descendants(armature, physics_seeds) - leaf_bones
    kdi_bones = descendants(armature, kdi_seeds) - physics_bones - leaf_bones
    kdi_collection = get_or_create_bone_collection(armature, KDI_BONE_COLLECTION)
    physics_collection = get_or_create_bone_collection(armature, PHYSICS_BONE_COLLECTION)
    leaf_collection = get_or_create_bone_collection(armature, LEAF_BONE_COLLECTION)
    move_bones_to_collection(armature, kdi_bones, kdi_collection)
    move_bones_to_collection(armature, physics_bones, physics_collection)
    move_bones_to_collection(armature, leaf_bones, leaf_collection)
    kdi_collection.is_visible = False
    physics_collection.is_visible = False
    leaf_collection.is_visible = False
    return {
        "bones": sorted(kdi_bones | physics_bones | leaf_bones),
        "collections": [KDI_BONE_COLLECTION, PHYSICS_BONE_COLLECTION, LEAF_BONE_COLLECTION],
    }


def _stamp_swap_bend_st(configs: list[dict[str, Any]]) -> None:
    """Mark every config, including the ones nested inside rotation targets.

    Rotation configs carry their own scalar configs in ``scalar_inputs`` and
    ``direct_source``; those are the dicts ``rotation_parameter`` actually
    receives, so stamping only the top level would swap the target side without
    the source side and produce a genuinely wrong rig rather than a relabel.
    """
    for config in configs:
        if not isinstance(config, dict):
            continue
        config["swap_bend_st"] = True
        nested = list(config.get("scalar_inputs") or [])
        direct = config.get("direct_source")
        if isinstance(direct, dict):
            nested.append(direct)
        if nested:
            _stamp_swap_bend_st(nested)


def build_scalar_drivers(
    armature: Any,
    audit: dict[str, Any],
    translation_axis_order: str = "XZY",
    scale_axis_order: str = "XZY",
    coordinate_profile: str = COORDINATE_PROFILE_REFERENCE,
    swap_bend_st: bool = False,
    additive: bool = False,
) -> dict[str, Any]:
    for axis_order in (translation_axis_order, scale_axis_order):
        if sorted(axis_order) != ["X", "Y", "Z"]:
            raise ValueError(f"Invalid target axis order: {axis_order!r}")
    coordinate_basis_to_reference(coordinate_profile)
    graph = audit["graph"]
    node_by_index = {int(node["operator_index"]): node for node in graph["nodes"]}
    scalar_links = [
        link for link in graph["driver_links"]
        if node_by_index[int(link["target"]["operator_index"])]["operator_type"]
        in {"TargetTranslate", "TargetScale"}
    ]
    occupied: set[int] = set()
    previous_registry: dict[str, Any] = {}
    previous_ownership: dict[str, Any] = {}
    if additive:
        if REGISTRY_PROPERTY in armature:
            previous_registry = json.loads(armature[REGISTRY_PROPERTY])
        if GENERATED_PROPERTY in armature:
            previous_ownership = json.loads(armature[GENERATED_PROPERTY])
        # Config ids are salted with the source file's hash so cross-layer
        # collisions are already unlikely, but the RUNTIME map is keyed by id
        # alone -- a collision would make one layer evaluate the other's config.
        occupied.update(int(config["id"]) for config in previous_registry.get("configs", []))
    configs = [
        build_config(link, node_by_index, armature, config_identifier(audit, link, occupied))
        for link in scalar_links
    ]
    for config in configs:
        config["target_axis_order"] = (
            translation_axis_order
            if config["target_type"] == "TargetTranslate"
            else scale_axis_order
        )
        config["coordinate_profile"] = coordinate_profile
        # The +90-degree package-bone roll maps reference Z to negative package
        # X. Scale does not carry that sign, but translation does.
        config["target_axis_sign"] = (
            -1.0
            if coordinate_profile == COORDINATE_PROFILE_PACKAGE_SKELETON_ROLL_90
            and config["target_type"] == "TargetTranslate"
            and config["target_parameter"] == "TranslateZ"
            else 1.0
        )
    anchor_nodes = [
        node for node in graph["nodes"]
        if node["operator_type"] in {"TargetPoscns", "TargetOricns"}
    ]
    anchor_configs = [
        build_anchor_config(node, armature, anchor_identifier(audit, node, occupied))
        for node in anchor_nodes
    ]
    for config in anchor_configs:
        config["coordinate_profile"] = coordinate_profile
    rotation_nodes = [
        node for node in graph["nodes"]
        if node["operator_type"] in {"TargetBendSTRoll", "TargetBendRoll", "TargetRotate"}
    ]
    rotation_configs = [
        build_rotation_config(
            node,
            [
                link for link in graph["driver_links"]
                if int(link["target"]["operator_index"]) == int(node["operator_index"])
            ],
            node_by_index,
            armature,
            rotation_identifier(audit, node, occupied),
        )
        for node in rotation_nodes
    ]
    for config in rotation_configs:
        config["coordinate_profile"] = coordinate_profile
        for scalar_input in config["scalar_inputs"]:
            scalar_input["coordinate_profile"] = coordinate_profile
        if config["direct_source"] is not None:
            config["direct_source"]["coordinate_profile"] = coordinate_profile

    existing = {
        (curve.data_path, curve.array_index)
        for curve in (armature.animation_data.drivers if armature.animation_data else [])
    }
    skipped_conflicts = 0
    if additive:
        # A secondary KDI pass may re-drive channels the main pass already owns
        # (real for the hair _Phy chains on PC0010). Blender allows only one
        # driver per channel, and the pre-physics layer is the one we can
        # evaluate faithfully, so the incumbent wins and the newcomer is dropped.
        # Rotation and anchor configs are dropped whole: a quaternion driven on
        # only some of its components would be worse than not driving it at all.
        def _keep(channels: list[tuple[str, int]]) -> bool:
            nonlocal skipped_conflicts
            if any(channel in existing for channel in channels):
                skipped_conflicts += 1
                return False
            return True

        configs = [c for c in configs if _keep([target_path_and_index(c)])]
        rotation_configs = [c for c in rotation_configs if _keep(rotation_channels(c))]
        anchor_configs = [c for c in anchor_configs if _keep(anchor_channels(c))]

    desired_channels = [target_path_and_index(config) for config in configs]
    for config in anchor_configs:
        desired_channels.extend(anchor_channels(config))
    for config in rotation_configs:
        desired_channels.extend(rotation_channels(config))
    if len(desired_channels) != len(set(desired_channels)):
        raise ValueError("Generated scalar, rotation, and anchor layers request the same target channel")
    collisions = [channel for channel in desired_channels if channel in existing]
    if collisions:
        raise ValueError(f"Existing drivers occupy {len(collisions)} requested KDI channels")

    compensated_children = apply_scale_compensation(armature, node_by_index)
    hidden_info = hide_noninteractive_bones(armature, node_by_index)
    hidden_helper_bones = hidden_info["bones"]
    generated = []
    try:
        for config in configs:
            data_path, array_index = target_path_and_index(config)
            fcurve = armature.driver_add(data_path, array_index)
            driver = fcurve.driver
            driver.type = "SCRIPTED"
            arguments = add_source_variables(driver, config, armature)
            driver.expression = f"kdi_scalar({config['id']},{','.join(arguments)})"
            if len(driver.expression) > 255:
                raise ValueError(
                    f"Scalar driver on bone {config['target_bone']} needs a "
                    f"{len(driver.expression)}-character expression; Blender's limit is 255"
                )
            generated.append({
                "data_path": data_path,
                "array_index": array_index,
                "config_id": config["id"],
                "target_bone": config["target_bone"],
                "target_parameter": config["target_parameter"],
            })
        for config in rotation_configs:
            pose_bone = armature.pose.bones[config["target_bone"]]
            pose_bone.rotation_mode = "QUATERNION"
            for data_path, array_index in rotation_channels(config):
                fcurve = armature.driver_add(data_path, array_index)
                driver = fcurve.driver
                driver.type = "SCRIPTED"
                arguments = add_rotation_variables(driver, config, armature)
                driver.expression = f"kdi_rotation({config['id']},{array_index},{','.join(arguments)})"
                if len(driver.expression) > 255:
                    raise ValueError(
                        f"Rotation driver expression exceeds Blender's limit on operator "
                        f"{config['target_operator_index']}"
                    )
                generated.append({
                    "data_path": data_path,
                    "array_index": array_index,
                    "config_id": config["id"],
                    "target_bone": config["target_bone"],
                    "target_parameter": f"RotationQuat[{array_index}]",
                })
        for config in anchor_configs:
            for data_path, array_index in anchor_channels(config):
                fcurve = armature.driver_add(data_path, array_index)
                driver = fcurve.driver
                driver.type = "SCRIPTED"
                arguments = add_anchor_variables(driver, config, armature)
                driver.expression = f"kdi_anchor({config['id']},{array_index},{','.join(arguments)})"
                if len(driver.expression) > 255:
                    raise ValueError(
                        f"Anchor driver expression exceeds Blender's limit on operator {config['target_operator_index']}"
                    )
                generated.append({
                    "data_path": data_path,
                    "array_index": array_index,
                    "config_id": config["id"],
                    "target_bone": config["target_bone"],
                    "target_parameter": (
                        f"Position[{array_index}]" if config["anchor_type"] == "POSITION"
                        else f"OrientationQuat[{array_index}]"
                    ),
                })
    except Exception:
        for item in generated:
            try:
                armature.driver_remove(item["data_path"], item["array_index"])
            except (TypeError, RuntimeError):
                pass
        for bone_name in compensated_children:
            bone = armature.data.bones.get(bone_name)
            if bone:
                bone.inherit_scale = "FULL"
        for bone_name in hidden_helper_bones:
            bone = armature.data.bones.get(bone_name)
            if bone:
                bone.hide = False
        for collection_name in hidden_info["collections"]:
            collection = armature.data.collections.get(collection_name)
            if collection:
                armature.data.collections.remove(collection)
        raise

    all_configs = configs + rotation_configs + anchor_configs
    if swap_bend_st:
        # Stamped onto the serialized configs rather than held in a module global
        # so the choice travels with the .blend and survives a reload -- otherwise
        # reopening the file would silently evaluate the drivers the other way.
        _stamp_swap_bend_st(all_configs)

    def _merge(previous: list[Any], added: list[Any]) -> list[Any]:
        """Union, order-preserving, for the ownership lists remove_generated walks."""
        merged = list(previous)
        seen = {json.dumps(item, sort_keys=True) for item in merged}
        for item in added:
            key = json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                merged.append(item)
        return merged

    registry = {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "source_sha256": graph["source"]["sha256"],
        "swap_bend_st": bool(swap_bend_st),
        "configs": previous_registry.get("configs", []) + all_configs,
    }
    ownership = {
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "drivers": previous_ownership.get("drivers", []) + generated,
        "scale_compensated_children": _merge(
            previous_ownership.get("scale_compensated_children", []), compensated_children
        ),
        "hidden_helper_bones": _merge(
            previous_ownership.get("hidden_helper_bones", []), hidden_helper_bones
        ),
        "hidden_bone_collections": _merge(
            previous_ownership.get("hidden_bone_collections", []), hidden_info["collections"]
        ),
        "translation_axis_order": translation_axis_order,
        "scale_axis_order": scale_axis_order,
        "coordinate_profile": coordinate_profile,
    }
    armature[REGISTRY_PROPERTY] = json.dumps(registry, separators=(",", ":"))
    armature[GENERATED_PROPERTY] = json.dumps(ownership, separators=(",", ":"))
    schedule_runtime_load()
    bpy.context.view_layer.update()
    return {
        "driver_count": len(generated),
        "skipped_conflict_count": skipped_conflicts,
        "scalar_driver_count": len(configs),
        "rotation_driver_count": sum(len(rotation_channels(config)) for config in rotation_configs),
        "rotation_target_count": len(rotation_configs),
        "bend_s_t_roll_target_count": sum(
            config["rotation_type"] == "TargetBendSTRoll" for config in rotation_configs
        ),
        "bend_roll_target_count": sum(
            config["rotation_type"] == "TargetBendRoll" for config in rotation_configs
        ),
        "direct_rotate_target_count": sum(
            config["rotation_type"] == "TargetRotate" for config in rotation_configs
        ),
        "anchor_driver_count": sum(len(anchor_channels(config)) for config in anchor_configs),
        "position_anchor_count": sum(config["anchor_type"] == "POSITION" for config in anchor_configs),
        "orientation_anchor_count": sum(config["anchor_type"] == "ORIENTATION" for config in anchor_configs),
        "scale_compensated_child_count": len(compensated_children),
        "hidden_helper_bone_count": len(hidden_helper_bones),
        "configs": configs + rotation_configs,
    }


class KDI_OT_step2_scalar_drivers(bpy.types.Operator, ImportHelper):
    """Audit a KineDriver JSON file and build its drivers on the active armature"""

    bl_idname = "import_scene.ff7r_kinedriver_json"
    bl_label = "FF7R KineDriver JSON"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    replace_previous_generated: BoolProperty(
        name="Replace previous generated KDI layer",
        default=True,
    )
    translation_axis_order: EnumProperty(
        name="Translation axis mapping",
        description="Map KDI Translate X/Y/Z outputs onto Blender location channels",
        items=AXIS_ORDER_ITEMS,
        default="XZY",
    )
    scale_axis_order: EnumProperty(
        name="Scale axis mapping",
        description="Map KDI Scale X/Y/Z outputs onto Blender scale channels",
        items=AXIS_ORDER_ITEMS,
        default="XZY",
    )
    coordinate_profile: StringProperty(
        default=COORDINATE_PROFILE_REFERENCE,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    swap_bend_st: BoolProperty(
        name="Swap BendS/BendT interpretation",
        description=SWAP_BEND_ST_DESCRIPTION,
        default=False,
    )
    additive: BoolProperty(
        name="Add to the existing KDI layer",
        description=(
            "Keep any KDI layer already on this armature and add this one beside it, "
            "skipping channels the existing layer already drives. Used to stack a "
            "character's secondary _KDI_Extra1/_Head/_Hood passes onto the main one"
        ),
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    def draw(self, _context: Any) -> None:
        layout = self.layout
        layout.prop(self, "replace_previous_generated")
        layout.separator()
        layout.label(text="Experimental target-axis mapping")
        layout.prop(self, "translation_axis_order")
        layout.prop(self, "scale_axis_order")
        layout.separator()
        layout.label(text="Debug")
        layout.prop(self, "swap_bend_st")

    def execute(self, context: Any) -> set[str]:
        armature = context.active_object
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select the imported armature and make it active")
            return {"CANCELLED"}
        try:
            source_path = Path(self.filepath)
            asset, raw = kdi_audit.read_kdi(source_path)
            graph = kdi_audit.build_graph(asset, source_path, raw)
            context.view_layer.update()
            armature_audit = kdi_audit.build_armature_audit(armature, graph)
            audit = kdi_audit.compose_report(graph, armature_audit)
            report_text = json.dumps(audit, indent=2, ensure_ascii=False)
            text_name = f"{kdi_audit.TEXT_BLOCK_PREFIX}{asset.get('Name') or source_path.stem}"
            text_block = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
            text_block.clear()
            text_block.write(report_text)
            if not audit.get("ready_for_driver_generation"):
                blocker_count = len(audit.get("blockers", []))
                raise ValueError(f"KDI audit found {blocker_count} blocker(s); see Blender Text {text_name}")
            if GENERATED_PROPERTY in armature and not self.additive:
                # Additive imports deliberately keep the existing layer; the
                # merge in build_scalar_drivers is what stacks onto it.
                if not self.replace_previous_generated:
                    raise ValueError("A generated KDI layer already exists")
                remove_generated(armature)
            result = build_scalar_drivers(
                armature,
                audit,
                translation_axis_order=self.translation_axis_order,
                scale_axis_order=self.scale_axis_order,
                coordinate_profile=self.coordinate_profile,
                swap_bend_st=self.swap_bend_st,
                additive=self.additive,
            )
            armature[SOURCE_PROPERTY] = str(source_path.resolve())
            success_message = (
                f"Created {result['scalar_driver_count']} scalar, "
                f"{result['rotation_driver_count']} rotation, and "
                f"{result['anchor_driver_count']} anchor drivers; "
                f"disabled inherited scale on "
                f"{result['scale_compensated_child_count']} children; hid "
                f"{result['hidden_helper_bone_count']} helper/physics/leaf bones"
                f"; translate {self.translation_axis_order}, scale {self.scale_axis_order}"
                f"; frame {self.coordinate_profile}"
                + ("; BendS/BendT SWAPPED" if self.swap_bend_st else "")
                + (
                    f"; skipped {result['skipped_conflict_count']} already-driven channel(s)"
                    if result["skipped_conflict_count"] else ""
                )
                + f"; audit: {text_name}"
            )
            self.report({"INFO"}, success_message)
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, f"KDI generation failed: {exc}")
            return {"CANCELLED"}


class KDI_OT_remove_scalar_drivers(bpy.types.Operator):
    """Remove generated KDI drivers and restore inherited-scale settings"""

    bl_idname = "kdi.remove_scalar_drivers"
    bl_label = "KDI: Remove Generated Driver Layer"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: Any) -> set[str]:
        armature = context.active_object
        if not armature or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select the generated armature and make it active")
            return {"CANCELLED"}
        try:
            removed, restored = remove_generated(armature)
            bpy.context.view_layer.update()
            self.report({"INFO"}, f"Removed {removed} drivers and restored {restored} scale-inheritance settings")
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, f"KDI cleanup failed: {exc}")
            return {"CANCELLED"}


CLASSES = (KDI_OT_step2_scalar_drivers, KDI_OT_remove_scalar_drivers)


def register() -> None:
    for cls in CLASSES:
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
        bpy.utils.register_class(cls)
    if kdi_scalar_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(kdi_scalar_load_post)
    schedule_runtime_load()


def unregister() -> None:
    if kdi_scalar_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(kdi_scalar_load_post)
    if bpy.app.timers.is_registered(deferred_runtime_load):
        bpy.app.timers.unregister(deferred_runtime_load)
    bpy.app.driver_namespace.pop("kdi_scalar", None)
    bpy.app.driver_namespace.pop("kdi_anchor", None)
    bpy.app.driver_namespace.pop("kdi_rotation", None)
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
    bpy.ops.kdi.step2_scalar_drivers("INVOKE_DEFAULT")
