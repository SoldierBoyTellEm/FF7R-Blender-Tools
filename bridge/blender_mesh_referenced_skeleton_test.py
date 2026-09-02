"""Smoke-test a package skeleton reduced to mesh-referenced bones and ancestors."""

import bpy


SKELETON = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_Skeleton.uasset"

if "ff7r_rebirth_tools" not in bpy.context.preferences.addons:
    bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")
prefs = bpy.context.preferences.addons["ff7r_rebirth_tools"].preferences
prefs.rebirth_install_root = r"G:\SteamLibrary\steamapps\common\FINAL FANTASY VII REBIRTH"
prefs.rebirth_oodle_dll = r"G:\Games\Final Fantasy VII - Remake Intergrade\Engine\Binaries\ThirdParty\Oodle\Win64\oo2core_7_win64.dll"
prefs.rebirth_usmap_path = r"C:\Users\Ghouls\Downloads\4.26.1-0+++UE4+Release-4.26-End (1).usmap"

result = bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT",
    virtual_path=SKELETON,
    armature_name="Cloud_Mesh_Referenced",
    import_kdi=False,
    create_variant_bone_collections=True,
    import_mesh_referenced_bones_only=True,
)
armature_obj = bpy.data.objects.get("Cloud_Mesh_Referenced")
bones = armature_obj.data.bones if armature_obj else ()
bone_names = {bone.name for bone in bones}
missing_parents = [
    bone.name for bone in bones
    if bone.parent is not None and bone.parent.name not in bone_names
]
print("MESH_REFERENCED_RESULT", result)
print("MESH_REFERENCED_BONES", len(bones), "MISSING_PARENTS", missing_parents)
if not armature_obj or len(bones) != 557 or missing_parents:
    raise SystemExit("Reduced mesh-referenced skeleton verification failed.")
