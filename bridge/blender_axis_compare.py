"""Compare bone rest orientation: existing baseline armature vs this importer's.

The KDI driver defaults (translation/scale axis order) were tuned against the
armature already in cloud baseline.blend. To pick correct defaults for armatures
built by skeleton/importer.py we need the exact rotation relating the two bone
conventions, per bone, and whether it is constant across the skeleton.
"""
import bpy
from collections import Counter

BLEND = r"O:\Blender\Assets\FF7\cloud baseline.blend"
FMODEL_JSON = r"O:\Games\Rebirth Tools\FModel\UpdateOutput\Exports\End\Content\Character\Player\PC0000_00_Cloud_Standard\Model\PC0000_00_Skeleton.json"

bpy.ops.wm.open_mainfile(filepath=BLEND)
bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")

baseline = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE"), None)
print("BASELINE_ARMATURE", baseline.name if baseline else None,
      len(baseline.data.bones) if baseline else 0)

bpy.ops.import_scene.ff7r_rebirth_skeleton_json(
    filepath=FMODEL_JSON, armature_name="Imported_Cmp")
mine = bpy.data.objects["Imported_Cmp"]
print("IMPORTED_ARMATURE", mine.name, len(mine.data.bones))


def classify(matrix):
    """Describe a 3x3 as a signed axis permutation when it is close to one."""
    labels = []
    for column in range(3):
        best_row, best_val = None, 0.0
        for row in range(3):
            if abs(matrix[row][column]) > abs(best_val):
                best_row, best_val = row, matrix[row][column]
        if abs(best_val) < 0.99:
            return None
        labels.append(("+" if best_val > 0 else "-") + "XYZ"[best_row])
    return ",".join(labels)


# R takes a vector expressed in MY bone axes to the same vector in BASELINE axes.
patterns = Counter()
examples = {}
for bone in mine.data.bones:
    other = baseline.data.bones.get(bone.name)
    if other is None:
        continue
    rel = other.matrix_local.to_3x3().normalized().inverted() @ bone.matrix_local.to_3x3().normalized()
    key = classify(rel) or "non-axis-aligned"
    patterns[key] += 1
    examples.setdefault(key, bone.name)

print("SHARED_BONES", sum(patterns.values()))
print("REST_ORIENTATION_DELTA (mine expressed in baseline's axes):")
for key, count in patterns.most_common(8):
    print(f"   {key:24s} {count:5d}   e.g. {examples[key]}")
