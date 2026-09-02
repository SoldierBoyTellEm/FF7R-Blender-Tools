"""Import a skeleton from the paks, then run the pak KDI importer onto it.

Checks the Kdi-suffix length clamp, and that KDI driver generation completes
cleanly against an armature built by skeleton/importer.py using the operator's
own default axis mapping (no explicit override passed).
"""
import bpy
import json

BLEND = r"O:\Blender\Assets\FF7\cloud baseline.blend"
SKEL = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_Skeleton.uasset"
KDI = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_KDI.uasset"

bpy.ops.wm.open_mainfile(filepath=BLEND)
bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")
prefs = bpy.context.preferences.addons["ff7r_rebirth_tools"].preferences
prefs.rebirth_install_root = r"G:\SteamLibrary\steamapps\common\FINAL FANTASY VII REBIRTH"
prefs.rebirth_oodle_dll = r"G:\Games\Final Fantasy VII - Remake Intergrade\Engine\Binaries\ThirdParty\Oodle\Win64\oo2core_7_win64.dll"
prefs.rebirth_usmap_path = r"C:\Users\Ghouls\Downloads\4.26.1-0+++UE4+Release-4.26-End (1).usmap"

# Remove the pre-existing baseline armature so the KDI operator can only bind to
# the freshly imported one.
for obj in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
    bpy.data.objects.remove(obj, do_unlink=True)

print("TEST_SKEL_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT", virtual_path=SKEL, armature_name="Cloud_Rig"))
arm_obj = bpy.data.objects["Cloud_Rig"]
arm = arm_obj.data
print("TEST_BONE_COUNT", len(arm.bones))

kdi_bones = [b for b in arm.bones if b.name.casefold().endswith("kdi")]
other_bones = [b for b in arm.bones if not b.name.casefold().endswith("kdi")]


def lengths(bones):
    return [(b.tail_local - b.head_local).length for b in bones]


kl, ol = lengths(kdi_bones), lengths(other_bones)
print(f"TEST_KDI_BONES {len(kl)} max_len={max(kl):.5f} min_len={min(kl):.5f}")
print(f"TEST_OTHER_BONES {len(ol)} max_len={max(ol):.5f}")
over = [b.name for b in kdi_bones if (b.tail_local - b.head_local).length > 0.02 + 1e-6]
print("TEST_KDI_OVER_CLAMP", len(over), over[:5])
print("TEST_CLAMP_VERDICT", "PASS" if not over and max(ol) > 0.02 else "FAIL")

def blen(name):
    b = arm.bones.get(name)
    return (b.tail_local - b.head_local).length if b else float("nan")


print("TEST_LIMB_LENGTHS " + " ".join(
    f"{n}={blen(n):.4f}" for n in (
        "L_UpperArm_a", "R_UpperArm_a", "L_UpperLeg_a", "R_UpperLeg_a",
        "L_Forearm_a", "L_Foreleg_a", "C_Hip_a", "L_Shoulder_a")))

# Core limb bones must be left/right symmetric. R_UpperLeg_a in particular carries
# an extra off-axis prop-holder child (R_Holder_Spo, 0.18m at 41 degrees) that a
# nearest-child rule latches onto, producing a visibly stumpy right thigh.
# NOTE: only these core bones are checked. Cloud's skeleton is genuinely asymmetric
# elsewhere -- hair/mantle "_Phy" chains and his single left pauldron have different
# raw child offsets per side -- so a whole-skeleton symmetry assertion is invalid.
CORE_PAIRS = ("UpperArm_a", "Forearm_a", "Hand_a", "UpperLeg_a", "Foreleg_a", "Foot_a")
asym = []
for stem in CORE_PAIRS:
    left, right = arm.bones.get("L_" + stem), arm.bones.get("R_" + stem)
    if left is None or right is None:
        continue
    a = (left.tail_local - left.head_local).length
    b = (right.tail_local - right.head_local).length
    if abs(a - b) > 1e-4:
        asym.append((stem, round(a, 4), round(b, 4)))
print("TEST_CORE_ASYMMETRIC", len(asym), asym)
print("TEST_SYMMETRY_VERDICT", "PASS" if not asym else "FAIL")

bpy.context.view_layer.objects.active = arm_obj
arm_obj.select_set(True)

# Deliberately pass no axis arguments: exercise the operator's own defaults.
try:
    print("TEST_KDI_RESULT", bpy.ops.import_scene.ff7r_rebirth_kdi_game_packages(
        "EXEC_DEFAULT", virtual_path=KDI))
except RuntimeError as exc:
    print("TEST_KDI_ERROR", str(exc))

for text in bpy.data.texts:
    if text.name.startswith("KDI_AUDIT_"):
        audit = json.loads(text.as_string())
        print("TEST_KDI_BLOCKERS", json.dumps(audit.get("blockers", []))[:400])

drivers = arm_obj.animation_data.drivers if arm_obj.animation_data else []
print("TEST_DRIVER_COUNT", len(drivers))
invalid = [d.data_path for d in drivers if not d.is_valid]
print("TEST_INVALID_DRIVERS", len(invalid), invalid[:5])
