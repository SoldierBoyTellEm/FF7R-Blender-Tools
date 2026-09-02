"""Blender background smoke test for direct Rebirth package StaticMesh import."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import bpy
from mathutils.kdtree import KDTree


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) < 3:
        raise SystemExit(
            "Usage: blender --background --python test_static_mesh_import.py -- "
            "GAME_ROOT OODLE_DLL USMAP [ASSET_PATH] [OUTPUT_BLEND] [REFERENCE_GLB]"
        )
    game_root, oodle_dll, usmap_path = args[:3]
    asset_path = args[3] if len(args) > 3 else (
        "End/Content/Environment/Machine/Model/Machine_MagicStore_01A.uasset"
    )
    output_blend = args[4] if len(args) > 4 else ""
    reference_glb = args[5] if len(args) > 5 else ""

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
        import_static_mesh_asset,
    )

    ff7r_rebirth_tools.register()
    with PackageAssetSession(game_root, oodle_dll, usmap_path) as session:
        static_mesh = session.static_mesh(asset_path)
    obj = import_static_mesh_asset(
        bpy.context,
        static_mesh,
        asset_path,
        scale_factor=0.01,
    )
    mesh = obj.data
    material_faces = Counter(polygon.material_index for polygon in mesh.polygons)
    bounds_min = [min(vertex.co[axis] for vertex in mesh.vertices) for axis in range(3)]
    bounds_max = [max(vertex.co[axis] for vertex in mesh.vertices) for axis in range(3)]
    result = {
        "name": obj.name,
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.polygons),
        "loops": len(mesh.loops),
        "uvLayers": [layer.name for layer in mesh.uv_layers],
        "materials": [material.name for material in mesh.materials],
        "materialFaceCounts": dict(sorted(material_faces.items())),
        "hasCustomNormals": mesh.has_custom_normals,
        "hasTangents": mesh.attributes.get("ff7r_tangent") is not None,
        "hasTangentSigns": mesh.attributes.get("ff7r_tangent_sign") is not None,
        "hasColors": mesh.color_attributes.get("Color") is not None,
        "boundsMin": bounds_min,
        "boundsMax": bounds_max,
        "virtualPath": obj.get("ff7r_virtual_path"),
    }
    assert result["vertices"] == 9424, result
    assert result["triangles"] == 9712, result
    assert result["materialFaceCounts"] == {0: 8944, 1: 768}, result
    assert result["uvLayers"] == ["Coordinate0", "Coordinate1", "Coordinate2", "Coordinate3"], result
    assert result["hasCustomNormals"], result
    assert result["hasTangents"] and result["hasTangentSigns"], result
    if reference_glb:
        existing_objects = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(Path(reference_glb).resolve()))
        imported_meshes = [
            candidate for candidate in bpy.data.objects
            if candidate not in existing_objects and candidate.type == 'MESH'
        ]
        reference_obj = max(imported_meshes, key=lambda candidate: len(candidate.data.polygons))

        def triangle_geometry(mesh, precision):
            coordinates = [
                tuple(round(value, precision) for value in vertex.co)
                for vertex in mesh.vertices
            ]
            return Counter(
                tuple(sorted(coordinates[index] for index in polygon.vertices))
                for polygon in mesh.polygons
            )

        overlap_by_precision = {}
        for precision in (6, 5, 4, 3):
            imported_triangles = triangle_geometry(mesh, precision)
            reference_triangles = triangle_geometry(reference_obj.data, precision)
            overlap_by_precision[str(precision)] = sum(
                (imported_triangles & reference_triangles).values()
            )
        result["referenceTriangleOverlap"] = overlap_by_precision
        result["referenceVertices"] = len(reference_obj.data.vertices)
        vertex_tree = KDTree(len(reference_obj.data.vertices))
        for vertex_index, vertex in enumerate(reference_obj.data.vertices):
            vertex_tree.insert(vertex.co, vertex_index)
        vertex_tree.balance()
        nearest_vertex_distances = [
            vertex_tree.find(vertex.co)[2] for vertex in mesh.vertices
        ]
        result["referenceMaxVertexDistance"] = max(nearest_vertex_distances)

        face_tree = KDTree(len(reference_obj.data.polygons))
        for face_index, polygon in enumerate(reference_obj.data.polygons):
            centroid = sum(
                (reference_obj.data.vertices[index].co for index in polygon.vertices),
                reference_obj.data.vertices[polygon.vertices[0]].co.copy() * 0.0,
            ) / len(polygon.vertices)
            face_tree.insert(centroid, face_index)
        face_tree.balance()
        nearest_face_distances = []
        max_normal_distance = 0.0
        max_uv_distances = [0.0] * min(len(mesh.uv_layers), len(reference_obj.data.uv_layers))
        for polygon in mesh.polygons:
            centroid = sum(
                (mesh.vertices[index].co for index in polygon.vertices),
                mesh.vertices[polygon.vertices[0]].co.copy() * 0.0,
            ) / len(polygon.vertices)
            _, reference_face_index, face_distance = face_tree.find(centroid)
            nearest_face_distances.append(face_distance)
            reference_polygon = reference_obj.data.polygons[reference_face_index]
            for loop_index in polygon.loop_indices:
                vertex = mesh.vertices[mesh.loops[loop_index].vertex_index]
                reference_loop_index = min(
                    reference_polygon.loop_indices,
                    key=lambda candidate: (
                        reference_obj.data.vertices[
                            reference_obj.data.loops[candidate].vertex_index
                        ].co - vertex.co
                    ).length_squared,
                )
                normal_distance = (
                    mesh.corner_normals[loop_index].vector
                    - reference_obj.data.corner_normals[reference_loop_index].vector
                ).length
                max_normal_distance = max(max_normal_distance, normal_distance)
                for uv_index in range(len(max_uv_distances)):
                    uv_distance = (
                        mesh.uv_layers[uv_index].data[loop_index].uv
                        - reference_obj.data.uv_layers[uv_index].data[reference_loop_index].uv
                    ).length
                    max_uv_distances[uv_index] = max(max_uv_distances[uv_index], uv_distance)
        result["referenceMaxFaceCentroidDistance"] = max(nearest_face_distances)
        result["referenceFaceCentroidsOutside1e5"] = sum(
            distance > 1e-5 for distance in nearest_face_distances
        )
        result["referenceMaxCornerNormalDistance"] = max_normal_distance
        result["referenceMaxUvDistances"] = max_uv_distances
        assert result["referenceMaxVertexDistance"] < 1e-5, result
        assert result["referenceFaceCentroidsOutside1e5"] == 0, result
        assert max(max_uv_distances, default=0.0) < 1e-6, result
        # The update reduced the tangent-frame precision; a sub-degree normal
        # difference from the old reference export is expected.
        assert max_normal_distance < 0.02, result
        for imported in imported_meshes:
            imported_data = imported.data
            bpy.data.objects.remove(imported, do_unlink=True)
            if imported_data.users == 0:
                bpy.data.meshes.remove(imported_data)
    if output_blend:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(output_blend).resolve()))
    print("STATIC_MESH_IMPORT_RESULT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
