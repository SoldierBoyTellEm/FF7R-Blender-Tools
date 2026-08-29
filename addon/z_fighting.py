"""The retained opposite-face cleanup tool from the legacy FF7R Actions add-on."""

from collections import defaultdict

import bmesh
import bpy


class MESH_OT_find_opposite_faces(bpy.types.Operator):
    """Find overlapping opposite-facing faces and add the established FF7R offset."""

    bl_idname = "mesh.find_opposite_faces"
    bl_label = "Find Opposite Faces"
    bl_description = "Mark opposite duplicate faces and add the FF7R alpha displacement fix"
    bl_options = {"REGISTER", "UNDO"}

    subsurface_weight: bpy.props.FloatProperty(
        name="Subsurface Weight",
        description="Subsurface weight for opposite faces",
        default=0.0,
        min=0.0,
        max=1.0,
    )

    @staticmethod
    def find_opposite_faces(obj):
        """Return vertices belonging to coincident faces with opposing normals."""
        mesh = obj.data
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()
            face_groups = defaultdict(list)
            for face in bm.faces:
                vertices = tuple(sorted(tuple(round(value, 6) for value in vert.co) for vert in face.verts))
                face_groups[vertices].append(face)

            opposite_vertices = set()
            for faces in face_groups.values():
                for index, face_a in enumerate(faces):
                    for face_b in faces[index + 1:]:
                        if face_a.normal.dot(face_b.normal) < -0.99:
                            opposite_vertices.update(vert.index for vert in face_a.verts)
                            opposite_vertices.update(vert.index for vert in face_b.verts)
            return opposite_vertices
        finally:
            bm.free()

    @staticmethod
    def setup_material_nodes(obj, vertex_group_name):
        """Expose the vertex-group mask to FF7R Principled materials, as before."""
        if not obj.data.materials:
            return

        try:
            if vertex_group_name not in obj.data.attributes:
                attribute = obj.data.attributes.new(name=vertex_group_name, type="FLOAT", domain="POINT")
            else:
                attribute = obj.data.attributes[vertex_group_name]
            group = obj.vertex_groups[vertex_group_name]
            weights = []
            for index in range(len(obj.data.vertices)):
                try:
                    weights.append(group.weight(index))
                except RuntimeError:
                    weights.append(0.0)
            attribute.data.foreach_set("value", weights)
        except Exception as exc:
            print(f"[FF7R Tools] Could not create opposite-face attribute for {obj.name}: {exc}")

        for material in obj.data.materials:
            if not material or not material.use_nodes:
                continue
            nodes = material.node_tree.nodes
            links = material.node_tree.links
            principled = next(
                (node for node in nodes if node.type == "BSDF_PRINCIPLED" and node.label == "FF7R Principled"),
                None,
            )
            if principled is None or "Subsurface Weight" not in principled.inputs:
                continue

            attribute_node = next(
                (node for node in nodes if node.type == "ATTRIBUTE" and node.attribute_name == vertex_group_name),
                None,
            )
            mix_node = next(
                (node for node in nodes if node.type == "MIX" and node.label == "FF7R_Subsurface_Mix"),
                None,
            )
            if attribute_node is None:
                attribute_node = nodes.new("ShaderNodeAttribute")
                attribute_node.attribute_name = vertex_group_name
                attribute_node.location = (principled.location.x - 600, principled.location.y - 200)
            if mix_node is None:
                mix_node = nodes.new("ShaderNodeMix")
                mix_node.data_type = "FLOAT"
                mix_node.label = "FF7R_Subsurface_Mix"
                mix_node.location = (principled.location.x - 300, principled.location.y - 200)
            mix_node.data_type = "FLOAT"
            mix_node.inputs[2].default_value = 0.0
            mix_node.inputs[3].default_value = 0.25

            for link in tuple(principled.inputs["Subsurface Weight"].links):
                links.remove(link)
            links.new(attribute_node.outputs["Fac"], mix_node.inputs["Factor"])
            links.new(mix_node.outputs["Result"], principled.inputs["Subsurface Weight"])

    def process_object(self, obj):
        if obj.type != "MESH":
            return False
        opposite_vertices = self.find_opposite_faces(obj)
        if not opposite_vertices:
            return False

        group_name = "Opposite_Faces"
        group = obj.vertex_groups.get(group_name) or obj.vertex_groups.new(name=group_name)
        group.add(list(opposite_vertices), 1.0, "REPLACE")
        modifier = obj.modifiers.get("Alpha Displace")
        if modifier is None:
            modifier = obj.modifiers.new(name="Alpha Displace", type="DISPLACE")
            modifier.strength = 0.0005
            modifier.vertex_group = group_name
        self.setup_material_nodes(obj, group_name)
        return True

    def execute(self, context):
        processed = 0
        for obj in context.selected_objects:
            candidates = obj.instance_collection.objects if obj.instance_type == "COLLECTION" and obj.instance_collection else (obj,)
            for candidate in candidates:
                processed += self.process_object(candidate)
        if processed:
            self.report({"INFO"}, f"Applied opposite-face fix to {processed} mesh object(s)")
        else:
            self.report({"WARNING"}, "No opposite-facing duplicate faces found in the selected objects")
        return {"FINISHED"}
