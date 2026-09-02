"""Smoke-test variant-exclusive bone collections from a package Skeleton import."""

import json
import re

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
    armature_name="Cloud_Variant_Collections",
    import_kdi=False,
    create_variant_bone_collections=True,
)
armature_obj = bpy.data.objects.get("Cloud_Variant_Collections")
metadata = json.loads(armature_obj.get("ff7r_variant_bone_usage", "{}")) if armature_obj else {}
collections = {
    collection.name: len(collection.bones)
    for collection in (armature_obj.data.collections if armature_obj else [])
    if re.fullmatch(r"PC\d{4}_\d{2}", collection.name)
}
print("VARIANT_COLLECTION_RESULT", result)
print("VARIANT_COLLECTIONS", collections)
print("VARIANT_USAGE_COUNTS", metadata.get("searchedMeshCount"), metadata.get("parsedMeshCount"))
print(
    "VARIANT_COLLECTION_VERDICT",
    "PASS" if armature_obj and metadata.get("parsedMeshCount") == 25 and collections else "FAIL",
)
expected = {"PC0000_00": 2, "PC0000_09": 30, "PC0000_10": 43, "PC0000_17": 2}
if metadata.get("searchedMeshCount") != 25 or metadata.get("parsedMeshCount") != 25:
    raise SystemExit(f"Unexpected scan counts: {metadata!r}")
if collections != expected:
    raise SystemExit(f"Unexpected variant collections: {collections!r}")
