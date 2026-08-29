"""KineDriver step-one importer audit for Blender.

Run this file from Blender's Text Editor.  It registers an operator and opens
a JSON file picker.  The operator does not modify the armature: it parses the
compiled KDI graph, checks it against the active armature, and writes the
result to both a Blender Text data-block and a JSON file beside the KDI file.

Outside Blender the file can be run with a KDI path to exercise the parser:

    python kdi_step1_audit.py examples/PC0000_00_KDI.json
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import traceback
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import bpy
    from bpy_extras.io_utils import ImportHelper
    from bpy.props import BoolProperty, StringProperty
except ModuleNotFoundError:  # Allows parser testing with ordinary Python.
    bpy = None
    ImportHelper = object


SCRIPT_VERSION = "0.1.0"
TEXT_BLOCK_PREFIX = "KDI_AUDIT_"


KNOWN_OPERATOR_TYPES = {
    "SourceTranslate",
    "SourceRotate",
    "TargetTranslate",
    "TargetScale",
    "TargetBendSTRoll",
    "TargetBendRoll",
    "TargetPoscns",
    "TargetOricns",
    "TargetDircns",
    "TargetRotate",
    "EffectorEZParamLink",
    "EffectorEZParamLinkLinear",
    "EffectorLinkWith",
    "EffectorRBFInterp",
    "EffectorExpr",
    "EffectorInverse",
    "Connection",
}


def enum_leaf(value: Any) -> str:
    """Return the useful suffix from an SQEX enum serialization."""
    text = str(value or "")
    text = text.rsplit("::", 1)[-1]
    for marker in (
        "ESQEX_KD_OperatorType_",
        "ESQEX_KD_ParameterType_",
        "ESQEX_KD_BaseSpaceType_",
        "ESQEX_KD_ConnectionType_",
    ):
        if marker in text:
            return text.split(marker, 1)[1]
    return text


def read_kdi(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    root = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(root, list) or len(root) != 1:
        raise ValueError("Expected the KDI JSON root to be a one-element array")
    asset = root[0]
    if not isinstance(asset, dict) or not isinstance(asset.get("Properties"), dict):
        raise ValueError("KDI JSON does not contain an object with Properties")
    return asset, raw


def body_array_name(op_type: str) -> str:
    return f"{op_type}Body"


def collect_bone_references(value: Any, path: str = "") -> list[dict[str, str]]:
    """Collect schema bone-name fields without mistaking graph NodeName for bones."""
    found: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if "BoneName" in key and key != "NodeName":
                names = child if isinstance(child, list) else [child]
                for name in names:
                    if isinstance(name, str) and name and name != "None":
                        found.append({"bone": name, "field": child_path})
            found.extend(collect_bone_references(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(collect_bone_references(child, f"{path}[{index}]"))
    return found


def port_summary(port: Any) -> dict[str, Any]:
    if not isinstance(port, dict):
        return {"invalid": True, "raw": port}
    return {
        "operator_index": port.get("OperatorIndex"),
        "parameter": enum_leaf(port.get("ParameterType")),
        "multi_index": port.get("MultiIndex"),
        "node_name": port.get("NodeName"),
    }


def graph_cycle_report(node_indices: Iterable[int], edges: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = set(node_indices)
    outgoing: dict[int, set[int]] = defaultdict(set)
    indegree = {index: 0 for index in nodes}
    for edge in edges:
        source = edge["source"].get("operator_index")
        target = edge["target"].get("operator_index")
        if source not in nodes or target not in nodes or source == target:
            continue
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1

    queue = deque(sorted(index for index, degree in indegree.items() if degree == 0))
    visited = []
    while queue:
        node = queue.popleft()
        visited.append(node)
        for target in sorted(outgoing.get(node, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    cyclic = sorted(index for index, degree in indegree.items() if degree > 0)
    return {
        "is_acyclic": not cyclic,
        "visited_node_count": len(visited),
        "cyclic_or_cycle_blocked_operator_indices": cyclic,
    }


def build_graph(asset: dict[str, Any], source_path: Path, raw: bytes) -> dict[str, Any]:
    properties = asset["Properties"]
    operators = properties.get("Operators") or []
    if not isinstance(operators, list):
        raise ValueError("Properties.Operators is not an array")

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    live_connections: list[dict[str, Any]] = []
    operator_types = Counter()
    work_indices = []

    for operator_index, operator in enumerate(operators):
        if not isinstance(operator, dict):
            errors.append({"code": "INVALID_OPERATOR", "operator_index": operator_index})
            continue
        op_type = enum_leaf(operator.get("OpType"))
        operator_types[op_type] += 1
        body_index = operator.get("OperatorBody")
        array_name = body_array_name(op_type)
        body_array = properties.get(array_name)
        body = None
        if not isinstance(body_index, int):
            errors.append({
                "code": "INVALID_BODY_INDEX",
                "operator_index": operator_index,
                "value": body_index,
            })
        elif not isinstance(body_array, list):
            errors.append({
                "code": "MISSING_BODY_ARRAY",
                "operator_index": operator_index,
                "operator_type": op_type,
                "expected_array": array_name,
            })
        elif body_index < 0 or body_index >= len(body_array):
            errors.append({
                "code": "BODY_INDEX_OUT_OF_RANGE",
                "operator_index": operator_index,
                "operator_type": op_type,
                "body_index": body_index,
                "body_count": len(body_array),
            })
        else:
            body = body_array[body_index]

        work_index = operator.get("WorkIndex")
        if isinstance(work_index, int) and work_index >= 0:
            work_indices.append(work_index)

        node = {
            "operator_index": operator_index,
            "operator_type": op_type,
            "work_index": work_index,
            "body_index": body_index,
            "label": operator.get("Label"),
        }
        if op_type != "Connection":
            node["body"] = body
        nodes.append(node)

        if op_type == "Connection" and isinstance(body, dict):
            source = port_summary(body.get("InPortInfo"))
            target = port_summary(body.get("OutPortInfo"))
            connection = {
                "connection_operator_index": operator_index,
                "connection_body_index": body_index,
                "connection_type": enum_leaf(body.get("ConnectionType")),
                "source": source,
                "target": target,
                "coefficient": body.get("Coef", 1.0),
                "other_source_parameter_index": body.get("OtherSourceParamIndex"),
                "other_target_parameter_index": body.get("OtherTargetParamIndex"),
            }
            live_connections.append(connection)

    node_by_index = {node["operator_index"]: node for node in nodes}
    order_violations = []
    invalid_port_refs = []
    incoming: dict[int, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for connection in live_connections:
        source_index = connection["source"].get("operator_index")
        target_index = connection["target"].get("operator_index")
        if source_index not in node_by_index or target_index not in node_by_index:
            invalid_port_refs.append(connection["connection_operator_index"])
            continue
        incoming[target_index].append(connection)
        outgoing[source_index].append(connection)
        if source_index > target_index:
            order_violations.append(connection["connection_operator_index"])

    driver_links = []
    incomplete_effectors = []
    for node in nodes:
        if not node["operator_type"].startswith("Effector"):
            continue
        index = node["operator_index"]
        inputs = [
            edge for edge in incoming.get(index, [])
            if edge["target"].get("parameter") == "Input"
        ]
        outputs = [
            edge for edge in outgoing.get(index, [])
            if edge["source"].get("parameter") == "Output"
        ]
        if not inputs or not outputs:
            incomplete_effectors.append({
                "effector_operator_index": index,
                "operator_type": node["operator_type"],
                "input_count": len(inputs),
                "output_count": len(outputs),
            })
        for input_edge in inputs:
            for output_edge in outputs:
                source_node = node_by_index.get(input_edge["source"].get("operator_index"), {})
                source_body = source_node.get("body") or {}
                target_node = node_by_index.get(output_edge["target"].get("operator_index"), {})
                target_body = target_node.get("body") or {}
                driver_links.append({
                    "effector_operator_index": index,
                    "effector_type": node["operator_type"],
                    "effector_body_index": node["body_index"],
                    "source": input_edge["source"],
                    "source_operator_type": source_node.get("operator_type"),
                    "source_bones": source_body.get("SourceBoneNameArray", []),
                    "input_coefficient": input_edge["coefficient"],
                    "target": output_edge["target"],
                    "target_bone": target_body.get("TargetObjectBoneName"),
                    "output_coefficient": output_edge["coefficient"],
                    "input_connection_operator_index": input_edge["connection_operator_index"],
                    "output_connection_operator_index": output_edge["connection_operator_index"],
                })

    target_channel_writes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in driver_links:
        key = f"{link.get('target_bone')}::{link['target'].get('parameter')}"
        target_channel_writes[key].append({
            "effector_operator_index": link["effector_operator_index"],
            "target_operator_index": link["target"].get("operator_index"),
        })
    collisions = {
        key: writes for key, writes in target_channel_writes.items() if len(writes) > 1
    }

    all_bone_refs = collect_bone_references(properties)
    refs_by_bone: dict[str, list[str]] = defaultdict(list)
    for ref in all_bone_refs:
        refs_by_bone[ref["bone"]].append(ref["field"])

    unknown_types = sorted(set(operator_types) - KNOWN_OPERATOR_TYPES)
    if unknown_types:
        warnings.append({"code": "UNKNOWN_OPERATOR_TYPES", "values": unknown_types})
    if invalid_port_refs:
        errors.append({"code": "INVALID_CONNECTION_PORT_REFERENCES", "operators": invalid_port_refs})
    if incomplete_effectors:
        warnings.append({"code": "INCOMPLETE_EFFECTORS", "count": len(incomplete_effectors)})
    if order_violations:
        warnings.append({"code": "EDGE_ORDER_VIOLATIONS", "operators": order_violations})

    expected_work_num = max(work_indices, default=-1) + 1
    declared_work_num = properties.get("WorkNum")
    work_counts = Counter(work_indices)
    duplicate_work_indices = sorted(index for index, count in work_counts.items() if count > 1)
    missing_work_indices = sorted(set(range(expected_work_num)) - set(work_indices))
    # Empty cooked assets commonly omit WorkNum and Operators altogether.
    if declared_work_num is not None and declared_work_num != expected_work_num:
        errors.append({
            "code": "WORK_NUM_MISMATCH",
            "declared": declared_work_num,
            "calculated": expected_work_num,
        })

    coefficient_counts = Counter(str(edge["coefficient"]) for edge in live_connections)
    body_counts = {
        key: len(value)
        for key, value in properties.items()
        if key.endswith("Body") and isinstance(value, list)
    }
    dead_connection_body_count = max(
        0, body_counts.get("ConnectionBody", 0) - len({
            edge["connection_body_index"] for edge in live_connections
        })
    )

    graph_nodes = [node["operator_index"] for node in nodes if node["operator_type"] != "Connection"]
    graph_edges = [
        edge for edge in live_connections
        if edge["source"].get("operator_index") in graph_nodes
        and edge["target"].get("operator_index") in graph_nodes
    ]

    return {
        "schema": "ff7r-kdi-step1-graph",
        "schema_version": 1,
        "source": {
            "path": str(source_path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "asset_type": asset.get("Type"),
            "asset_name": asset.get("Name"),
        },
        "summary": {
            "declared_work_num": declared_work_num,
            "calculated_work_num": expected_work_num,
            "operator_count": len(operators),
            "operator_type_counts": dict(sorted(operator_types.items())),
            "body_counts": body_counts,
            "live_connection_count": len(live_connections),
            "dead_or_duplicate_connection_body_count": dead_connection_body_count,
            "driver_link_count": len(driver_links),
            "unique_referenced_bone_count": len(refs_by_bone),
            "coefficient_counts": dict(sorted(coefficient_counts.items())),
            "unknown_operator_types": unknown_types,
            "duplicate_work_indices": duplicate_work_indices,
            "missing_work_indices": missing_work_indices,
            "target_channel_collision_count": len(collisions),
        },
        "validation": {
            "errors": errors,
            "warnings": warnings,
            "graph": graph_cycle_report(graph_nodes, graph_edges),
            "incomplete_effectors": incomplete_effectors,
            "target_channel_collisions": collisions,
        },
        "bone_references": {
            bone: sorted(set(fields)) for bone, fields in sorted(refs_by_bone.items())
        },
        "nodes": nodes,
        "live_connections": live_connections,
        "driver_links": driver_links,
    }


def rounded(value: Any, digits: int = 9) -> Any:
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    return round(number, digits)


def vector_data(value: Any) -> list[float]:
    return [rounded(component) for component in value]


def matrix_data(value: Any) -> list[list[float]]:
    return [[rounded(component) for component in row] for row in value]


def safe_custom_property(value: Any) -> Any:
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return rounded(value)
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): safe_custom_property(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)) or hasattr(value, "to_list"):
        try:
            return [safe_custom_property(child) for child in value]
        except TypeError:
            pass
    return repr(value)


def id_properties(owner: Any) -> dict[str, Any]:
    return {
        key: safe_custom_property(owner[key])
        for key in owner.keys()
        if key != "_RNA_UI"
    }


def constraint_data(constraint: Any) -> dict[str, Any]:
    result = {
        "name": constraint.name,
        "type": constraint.type,
        "mute": constraint.mute,
        "influence": rounded(constraint.influence),
    }
    for attr in (
        "target",
        "subtarget",
        "owner_space",
        "target_space",
        "head_tail",
        "mix_mode",
        "track_axis",
        "up_axis",
        "use_offset",
        "use_x",
        "use_y",
        "use_z",
        "invert_x",
        "invert_y",
        "invert_z",
    ):
        if not hasattr(constraint, attr):
            continue
        value = getattr(constraint, attr)
        if attr == "target":
            value = value.name if value else None
        result[attr] = safe_custom_property(value)
    return result


def driver_fcurve_data(fcurve: Any) -> dict[str, Any]:
    driver = fcurve.driver
    variables = []
    for variable in driver.variables:
        targets = []
        for target in variable.targets:
            targets.append({
                "id_name": target.id.name if target.id else None,
                "id_type": target.id_type,
                "data_path": target.data_path,
                "bone_target": target.bone_target,
                "transform_type": target.transform_type,
                "transform_space": target.transform_space,
                "rotation_mode": getattr(target, "rotation_mode", None),
            })
        variables.append({"name": variable.name, "type": variable.type, "targets": targets})
    return {
        "data_path": fcurve.data_path,
        "array_index": fcurve.array_index,
        "mute": fcurve.mute,
        "is_valid": fcurve.is_valid,
        "driver_type": driver.type,
        "expression": driver.expression,
        "variables": variables,
    }


def animation_driver_data(owner: Any) -> list[dict[str, Any]]:
    animation_data = getattr(owner, "animation_data", None)
    if not animation_data:
        return []
    return [driver_fcurve_data(fcurve) for fcurve in animation_data.drivers]


def bone_rest_data(bone: Any) -> dict[str, Any]:
    parent = bone.parent
    rest_local = parent.matrix_local.inverted() @ bone.matrix_local if parent else bone.matrix_local.copy()
    return {
        "name": bone.name,
        "parent": parent.name if parent else None,
        "use_connect": bone.use_connect,
        "use_deform": bone.use_deform,
        "inherit_scale": bone.inherit_scale,
        "head_local": vector_data(bone.head_local),
        "tail_local": vector_data(bone.tail_local),
        "matrix_local": matrix_data(bone.matrix_local),
        "rest_relative_to_parent": matrix_data(rest_local),
        "custom_properties": id_properties(bone),
    }


def pose_bone_data(pose_bone: Any) -> dict[str, Any]:
    return {
        "name": pose_bone.name,
        "rotation_mode": pose_bone.rotation_mode,
        "location": vector_data(pose_bone.location),
        "rotation_quaternion": vector_data(pose_bone.rotation_quaternion),
        "rotation_axis_angle": vector_data(pose_bone.rotation_axis_angle),
        "rotation_euler": vector_data(pose_bone.rotation_euler),
        "scale": vector_data(pose_bone.scale),
        "matrix_basis": matrix_data(pose_bone.matrix_basis),
        "matrix": matrix_data(pose_bone.matrix),
        "constraints": [constraint_data(item) for item in pose_bone.constraints],
        "custom_properties": id_properties(pose_bone),
    }


def collect_ancestors(armature_object: Any, names: Iterable[str]) -> set[str]:
    result = set()
    for name in names:
        bone = armature_object.data.bones.get(name)
        while bone:
            result.add(bone.name)
            bone = bone.parent
    return result


def build_armature_audit(armature_object: Any, graph: dict[str, Any]) -> dict[str, Any]:
    referenced = set(graph["bone_references"])
    available = set(armature_object.data.bones.keys())
    missing = sorted(referenced - available)
    present = sorted(referenced & available)
    related = sorted(collect_ancestors(armature_object, present))
    non_quaternion = sorted(
        name for name in present
        if armature_object.pose.bones[name].rotation_mode != "QUATERNION"
    )

    target_bones = sorted({
        link["target_bone"] for link in graph["driver_links"] if link.get("target_bone")
    })
    source_bones = sorted({
        name
        for link in graph["driver_links"]
        for name in link.get("source_bones", [])
    })

    scene = bpy.context.scene
    return {
        "schema": "ff7r-kdi-step1-armature-audit",
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "script_version": SCRIPT_VERSION,
        "blender": {
            "version": bpy.app.version_string,
            "blend_file": bpy.data.filepath or None,
            "scene": scene.name,
            "frame": scene.frame_current,
            "unit_system": scene.unit_settings.system,
            "unit_scale_length": rounded(scene.unit_settings.scale_length),
            "length_unit": scene.unit_settings.length_unit,
        },
        "armature_object": {
            "name": armature_object.name,
            "data_name": armature_object.data.name,
            "matrix_world": matrix_data(armature_object.matrix_world),
            "location": vector_data(armature_object.location),
            "rotation_mode": armature_object.rotation_mode,
            "rotation_quaternion": vector_data(armature_object.rotation_quaternion),
            "rotation_euler": vector_data(armature_object.rotation_euler),
            "scale": vector_data(armature_object.scale),
            "custom_properties": id_properties(armature_object),
            "data_custom_properties": id_properties(armature_object.data),
        },
        "summary": {
            "armature_bone_count": len(available),
            "referenced_bone_count": len(referenced),
            "present_referenced_bone_count": len(present),
            "missing_referenced_bone_count": len(missing),
            "related_bone_count_including_ancestors": len(related),
            "source_bones_in_driver_links": len(source_bones),
            "target_bones_in_driver_links": len(target_bones),
            "non_quaternion_referenced_pose_bone_count": len(non_quaternion),
            "object_driver_count": len(animation_driver_data(armature_object)),
            "armature_data_driver_count": len(animation_driver_data(armature_object.data)),
        },
        "validation": {
            "missing_bones": missing,
            "non_quaternion_referenced_pose_bones": non_quaternion,
        },
        "source_bones_in_driver_links": source_bones,
        "target_bones_in_driver_links": target_bones,
        "all_bone_hierarchy": [
            {
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "use_connect": bone.use_connect,
                "use_deform": bone.use_deform,
                "inherit_scale": bone.inherit_scale,
            }
            for bone in armature_object.data.bones
        ],
        "related_rest_bones": {
            name: bone_rest_data(armature_object.data.bones[name]) for name in related
        },
        "referenced_pose_bones": {
            name: pose_bone_data(armature_object.pose.bones[name]) for name in present
        },
        "existing_object_drivers": animation_driver_data(armature_object),
        "existing_armature_data_drivers": animation_driver_data(armature_object.data),
    }


def compose_report(graph: dict[str, Any], armature: dict[str, Any]) -> dict[str, Any]:
    graph_errors = graph["validation"]["errors"]
    missing = armature["validation"]["missing_bones"]
    non_quaternion = armature["validation"]["non_quaternion_referenced_pose_bones"]
    ready = not graph_errors and not missing and not non_quaternion and graph["validation"]["graph"]["is_acyclic"]
    return {
        "schema": "ff7r-kdi-step1-audit-report",
        "schema_version": 1,
        "ready_for_driver_generation": ready,
        "blockers": {
            "graph_errors": graph_errors,
            "missing_bones": missing,
            "non_quaternion_referenced_pose_bones": non_quaternion,
            "graph_cycle_or_blockage": (
                [] if graph["validation"]["graph"]["is_acyclic"]
                else graph["validation"]["graph"]["cyclic_or_cycle_blocked_operator_indices"]
            ),
        },
        "graph": graph,
        "armature": armature,
    }


if bpy is not None:

    class KDI_OT_step1_audit(bpy.types.Operator, ImportHelper):
        """Parse a KDI graph and audit it against the active armature"""

        bl_idname = "kdi.step1_audit"
        bl_label = "KDI Step 1: Audit Active Armature"
        bl_options = {"REGISTER"}

        filename_ext = ".json"
        filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
        write_external_report: BoolProperty(
            name="Write JSON report beside KDI",
            description="Also save the report as a JSON file next to the selected KDI file",
            default=True,
        )

        def execute(self, context):
            armature_object = context.active_object
            if not armature_object or armature_object.type != "ARMATURE":
                self.report({"ERROR"}, "Select the imported armature and make it active")
                return {"CANCELLED"}

            source_path = Path(self.filepath)
            try:
                asset, raw = read_kdi(source_path)
                graph = build_graph(asset, source_path, raw)
                context.view_layer.update()
                armature = build_armature_audit(armature_object, graph)
                report = compose_report(graph, armature)
                report_text = json.dumps(report, indent=2, ensure_ascii=False)

                text_name = f"{TEXT_BLOCK_PREFIX}{asset.get('Name') or source_path.stem}"
                text_block = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
                text_block.clear()
                text_block.write(report_text)

                external_path = None
                if self.write_external_report:
                    external_path = source_path.with_name(f"{source_path.stem}_BLENDER_AUDIT.json")
                    try:
                        external_path.write_text(report_text, encoding="utf-8")
                    except OSError as exc:
                        self.report({"WARNING"}, f"Text report created, but JSON file could not be written: {exc}")
                        external_path = None

                missing_count = armature["summary"]["missing_referenced_bone_count"]
                status = "ready" if report["ready_for_driver_generation"] else "has blockers"
                message = f"KDI audit {status}; {missing_count} missing bones; Text: {text_name}"
                if external_path:
                    message += f"; File: {external_path.name}"
                self.report({"INFO"}, message)
                print(message)
                print(json.dumps({
                    "ready_for_driver_generation": report["ready_for_driver_generation"],
                    "graph_summary": graph["summary"],
                    "armature_summary": armature["summary"],
                    "blockers": report["blockers"],
                }, indent=2))
                return {"FINISHED"}
            except Exception as exc:
                traceback.print_exc()
                self.report({"ERROR"}, f"KDI audit failed: {exc}")
                return {"CANCELLED"}


    CLASSES = (KDI_OT_step1_audit,)


    def register() -> None:
        for cls in CLASSES:
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
            bpy.utils.register_class(cls)


    def unregister() -> None:
        for cls in reversed(CLASSES):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass


def cli_main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python kdi_step1_audit.py PATH_TO_KDI.json", file=sys.stderr)
        return 2
    source_path = Path(argv[0])
    asset, raw = read_kdi(source_path)
    graph = build_graph(asset, source_path, raw)
    print(json.dumps({
        "source": graph["source"],
        "summary": graph["summary"],
        "validation": graph["validation"],
    }, indent=2, ensure_ascii=False))
    return 0 if not graph["validation"]["errors"] else 1


if __name__ == "__main__":
    if bpy is None:
        raise SystemExit(cli_main(sys.argv[1:]))
    register()
    bpy.ops.kdi.step1_audit("INVOKE_DEFAULT")
