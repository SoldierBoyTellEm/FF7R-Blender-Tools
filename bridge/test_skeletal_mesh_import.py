"""Blender background smoke test for a Rebirth package SkeletalMesh import."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) < 3:
        raise SystemExit(
            "Usage: blender --background --python test_skeletal_mesh_import.py -- "
            "GAME_ROOT OODLE_DLL USMAP [ASSET_PATH] [OUTPUT_BLEND]"
        )
    game_root, oodle_dll, usmap_path = args[:3]
    asset_path = args[3] if len(args) > 3 else (
        "End/Content/Character/Player/PC0004_00_RedXIII_Standard/Model/PC0004_00.uasset"
    )
    output_blend = args[4] if len(args) > 4 else ""

    # The add-on lives in this repo as addon/, but its modules import each
    # other by package name, so bind that directory to the real package name
    # before importing anything from it.
    addon_directory = Path(__file__).resolve().parent.parent / "addon"
    spec = importlib.util.spec_from_file_location(
        "ff7r_rebirth_tools",
        addon_directory / "__init__.py",
        submodule_search_locations=[str(addon_directory)],
    )
    ff7r_rebirth_tools = importlib.util.module_from_spec(spec)
    sys.modules["ff7r_rebirth_tools"] = ff7r_rebirth_tools
    spec.loader.exec_module(ff7r_rebirth_tools)
    from ff7r_rebirth_tools.game_packages import (
        PackageAssetSession,
        import_skeletal_mesh_asset,
    )
    from ff7r_rebirth_tools.skeleton.importer import (
        _bones_from_bridge,
        build_armature_from_bones,
    )

    ff7r_rebirth_tools.register()
    with PackageAssetSession(game_root, oodle_dll, usmap_path) as session:
        skeletal_mesh = session.skeletal_mesh(asset_path)
        skeleton_path = skeletal_mesh.get("skeletonPath")
        assert skeleton_path, skeletal_mesh
        skeleton = session.skeleton_asset(skeleton_path)

    positions = [Vector((point[0], -point[1], point[2])) for point in skeletal_mesh["positions"]]
    geometric_normals = [Vector() for _ in positions]
    indices = skeletal_mesh["indices"]
    for offset in range(0, len(indices), 3):
        a, b, c = (positions[indices[offset + item]] for item in range(3))
        face_normal = (b - a).cross(c - a)
        for vertex_index in indices[offset:offset + 3]:
            geometric_normals[vertex_index] += face_normal
    agreement_total = 0.0
    agreement_count = 0
    for geometric_normal, source_normal in zip(geometric_normals, skeletal_mesh["normals"]):
        normal = Vector((source_normal[0], -source_normal[1], source_normal[2]))
        if geometric_normal.length_squared and normal.length_squared:
            geometric_normal.normalize()
            normal.normalize()
            agreement_total += abs(geometric_normal.dot(normal))
            agreement_count += 1
    normal_agreement = agreement_total / agreement_count

    armature_obj = build_armature_from_bones(
        bpy.context,
        skeleton.get("name") or Path(skeleton_path).stem,
        _bones_from_bridge(skeleton),
        skeleton.get("sockets") or [],
        scale_factor=0.01,
    )
    obj, weighted_vertices, influence_count = import_skeletal_mesh_asset(
        bpy.context,
        skeletal_mesh,
        asset_path,
        armature_obj=armature_obj,
        scale_factor=0.01,
    )
    mesh = obj.data
    modifier = next((item for item in obj.modifiers if item.type == 'ARMATURE'), None)
    result = {
        "name": obj.name,
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.polygons),
        "uvLayers": [layer.name for layer in mesh.uv_layers],
        "materials": len(mesh.materials),
        "vertexGroups": len(obj.vertex_groups),
        "armature": armature_obj.name,
        "armatureBones": len(armature_obj.data.bones),
        "weightedVertices": weighted_vertices,
        "influences": influence_count,
        "linkedSkeleton": obj.get("ff7r_linked_skeleton_path"),
        "geometricNormalAgreement": normal_agreement,
    }
    assert result["vertices"] == 129090, result
    assert result["triangles"] == 111992, result
    assert result["uvLayers"] == ["Coordinate0", "Coordinate1", "Coordinate2", "Coordinate3"], result
    assert result["materials"] == 13, result
    # PC0004's shared skeleton contains additional, unweighted helper bones;
    # the mesh itself references 326 of them through its section bone maps.
    assert result["armatureBones"] == 403, result
    assert modifier is not None and modifier.object == armature_obj, result
    assert result["vertexGroups"] > 0, result
    assert result["weightedVertices"] == result["vertices"], result
    assert result["influences"] > result["vertices"], result
    assert result["geometricNormalAgreement"] > 0.95, result
    if output_blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(output_blend).resolve()))
    print("SKELETAL_MESH_IMPORT_RESULT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
