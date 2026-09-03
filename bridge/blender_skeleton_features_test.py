"""Verify the auto-KDI-import and connect-bones checkboxes.

1. Package-browser skeleton import with import_kdi default-on: a single operator
   call should build the armature AND attach KDI drivers, no second call needed.
2. connect_bones=True must not perturb any bone's head beyond what the umodel
   ground-truth comparison already tolerates (0.2mm) -- the whole point of the
   distance threshold is that a real connect changes nothing visible.
"""
import bpy
import json
import math
import re
from pathlib import Path

BLEND = r"O:\Blender\Assets\FF7\cloud baseline.blend"
UMODEL_TXT = str(Path(__file__).resolve().parent / "pc0000_00 hierarchy.txt")
SKEL = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_Skeleton.uasset"
TOLERANCE = 0.0002

bpy.ops.wm.open_mainfile(filepath=BLEND)
bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")
prefs = bpy.context.preferences.addons["ff7r_rebirth_tools"].preferences
prefs.rebirth_install_root = r"G:\SteamLibrary\steamapps\common\FINAL FANTASY VII REBIRTH"
prefs.rebirth_oodle_dll = r"G:\Games\Final Fantasy VII - Remake Intergrade\Engine\Binaries\ThirdParty\Oodle\Win64\oo2core_7_win64.dll"
prefs.rebirth_usmap_path = r"C:\Users\Ghouls\Downloads\4.26.1-0+++UE4+Release-4.26-End (1).usmap"

for obj in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
    bpy.data.objects.remove(obj, do_unlink=True)

truth = {}
pattern = re.compile(r"^\s*([A-Za-z0-9_]+)\s*\(head=\(([-0-9.]+),\s*([-0-9.]+),\s*([-0-9.]+)\)")
with open(UMODEL_TXT, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        m = pattern.match(line)
        if m:
            truth[m.group(1)] = tuple(float(x) for x in m.groups()[1:])

# --- Test 1: default import_kdi=True, single call should do both. ---
print("TEST1_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT", virtual_path=SKEL, armature_name="AutoKdi_Rig"))
arm_obj = bpy.data.objects.get("AutoKdi_Rig")
print("TEST1_ARMATURE_FOUND", arm_obj is not None)
drivers = arm_obj.animation_data.drivers if arm_obj and arm_obj.animation_data else []
print("TEST1_DRIVER_COUNT", len(drivers))
audits = [t.name for t in bpy.data.texts if t.name.startswith("KDI_AUDIT_")]
print("TEST1_AUDIT_TEXT_PRESENT", bool(audits))
print("TEST1_VERDICT", "PASS" if arm_obj and len(drivers) > 0 else "FAIL")

# --- Test 2: import_kdi=False should skip it (no drivers, no crash). ---
print("TEST2_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT", virtual_path=SKEL, armature_name="NoKdi_Rig", import_kdi=False))
arm2 = bpy.data.objects.get("NoKdi_Rig")
drivers2 = arm2.animation_data.drivers if arm2 and arm2.animation_data else []
print("TEST2_DRIVER_COUNT", len(drivers2))
print("TEST2_VERDICT", "PASS" if arm2 and len(drivers2) == 0 else "FAIL")

# --- Test 3: connect_bones=True must not move heads beyond tolerance. ---
print("TEST3_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT", virtual_path=SKEL, armature_name="Connected_Rig",
    import_kdi=False, connect_bones=True))
arm3 = bpy.data.objects.get("Connected_Rig")
connected_count = sum(1 for b in arm3.data.bones if b.use_connect) if arm3 else 0
print("TEST3_CONNECTED_BONE_COUNT", connected_count)

errs = []
for bone in (arm3.data.bones if arm3 else []):
    want = truth.get(bone.name)
    if want is None:
        continue
    head = bone.head_local
    errs.append(max(abs(head[k] - want[k]) for k in range(3)))
max_err = max(errs) if errs else float("nan")
over = sum(1 for e in errs if e > TOLERANCE)
print(f"TEST3_MAX_ERR {max_err:.6f}  COMPARED {len(errs)}  OVER_TOLERANCE {over}")
print("TEST3_VERDICT", "PASS" if connected_count > 0 and over == 0 else "FAIL")
