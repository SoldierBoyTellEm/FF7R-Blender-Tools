"""Verify the skeleton importer against umodel's reference hierarchy.

Compares bone HEAD positions only. Bone tail/roll axis is a known-divergent issue
in both umodel and FModel (UE and Blender disagree on bone axis convention), and
is deliberately out of scope here.
"""
import bpy
import math
import re
from pathlib import Path

BLEND = r"O:\Blender\Assets\FF7\cloud baseline.blend"
FMODEL_JSON = r"O:\Games\Rebirth Tools\FModel\UpdateOutput\Exports\End\Content\Character\Player\PC0000_00_Cloud_Standard\Model\PC0000_00_Skeleton.json"
UMODEL_TXT = str(Path(__file__).resolve().parent / "pc0000_00 hierarchy.txt")
ASSET = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_Skeleton.uasset"

TOLERANCE = 0.0002  # 0.2mm; umodel's text output is only 4-decimal, so ~0.05mm is pure rounding

bpy.ops.wm.open_mainfile(filepath=BLEND)
bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")
prefs = bpy.context.preferences.addons["ff7r_rebirth_tools"].preferences
prefs.rebirth_install_root = r"G:\SteamLibrary\steamapps\common\FINAL FANTASY VII REBIRTH"
prefs.rebirth_oodle_dll = r"G:\Games\Final Fantasy VII - Remake Intergrade\Engine\Binaries\ThirdParty\Oodle\Win64\oo2core_7_win64.dll"
prefs.rebirth_usmap_path = r"C:\Users\Ghouls\Downloads\4.26.1-0+++UE4+Release-4.26-End (1).usmap"

truth = {}
pattern = re.compile(r"^\s*([A-Za-z0-9_]+)\s*\(head=\(([-0-9.]+),\s*([-0-9.]+),\s*([-0-9.]+)\)")
with open(UMODEL_TXT, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        match = pattern.match(line)
        if match:
            truth[match.group(1)] = (
                float(match.group(2)), float(match.group(3)), float(match.group(4))
            )
print("TEST_TRUTH_BONES", len(truth))


def verify(obj, label):
    if obj is None:
        print(f"TEST_{label}_NO_OBJECT")
        return
    arm = obj.data
    print(f"TEST_{label}_BONE_COUNT", len(arm.bones))
    print(f"TEST_{label}_ROOTS", [b.name for b in arm.bones if b.parent is None])

    worst = []
    compared = 0
    malformed = []
    for bone in arm.bones:
        head = bone.head_local
        if any(math.isnan(c) or math.isinf(c) for c in head):
            malformed.append((bone.name, "nan_or_inf"))
        elif (bone.tail_local - bone.head_local).length < 1e-6:
            malformed.append((bone.name, "zero_length"))
        want = truth.get(bone.name)
        if want is None:
            continue
        compared += 1
        err = max(abs(head[k] - want[k]) for k in range(3))
        worst.append((err, bone.name, tuple(round(c, 4) for c in head), want))
    worst.sort(reverse=True)

    print(f"TEST_{label}_COMPARED", compared, "of", len(arm.bones))
    print(f"TEST_{label}_MALFORMED", malformed[:5], "total=", len(malformed))
    if worst:
        max_err = worst[0][0]
        mean_err = sum(w[0] for w in worst) / len(worst)
        failures = [w for w in worst if w[0] > TOLERANCE]
        print(f"TEST_{label}_MAX_ERR {max_err:.6f}  MEAN_ERR {mean_err:.6f}")
        print(f"TEST_{label}_OVER_TOLERANCE", len(failures))
        for err, bname, got, want in worst[:5]:
            print(f"    worst {bname:16s} err={err:.6f} got={got} want={want}")
        print(f"TEST_{label}_VERDICT", "PASS" if not failures and not malformed else "FAIL")
    for probe in ("Trans", "C_Hip_a", "C_Spine_a", "C_Head_a", "L_Foot_a"):
        bone = arm.bones.get(probe)
        if bone:
            print(f"    {probe:10s} head={tuple(round(c, 4) for c in bone.head_local)}"
                  f"  want={truth.get(probe)}")

    # Bone axis check. UE bones run down local +X and children are offset along it,
    # so a single-child bone should AIM at its child. Measured as an angle, not a
    # distance, and skipping children coincident with their parent's head (184 of
    # them in this skeleton -- helper/attachment bones with a zero offset, which no
    # aim direction can ever "reach"). A handful of genuine outliers exist in the
    # source data (prop holders, facial and KDI helper bones), so this asserts on
    # the overwhelming majority rather than demanding perfection.
    angles = []
    for bone in arm.bones:
        if len(bone.children) != 1:
            continue
        to_child = bone.children[0].head_local - bone.head_local
        if to_child.length < 1e-5:
            continue
        aim = bone.tail_local - bone.head_local
        if aim.length < 1e-9:
            continue
        angles.append((math.degrees(aim.angle(to_child)), bone.name))
    angles.sort(reverse=True)
    if angles:
        aligned = [a for a in angles if a[0] <= 1.0]
        pct = 100.0 * len(aligned) / len(angles)
        print(f"TEST_{label}_AIMABLE_BONES {len(angles)}  WITHIN_1DEG {len(aligned)} ({pct:.1f}%)")
        for ang, bname in angles[:5]:
            print(f"    aim-vs-child {bname:22s} {ang:7.2f} deg")
        print(f"TEST_{label}_AXIS_VERDICT", "PASS" if pct >= 97.0 else "FAIL")

    lengths = [(b.tail_local - b.head_local).length for b in arm.bones]
    tiny = sum(1 for x in lengths if x < 0.002)
    print(f"TEST_{label}_LEN min={min(lengths):.5f} max={max(lengths):.4f} under_2mm={tiny}")


try:
    print("TEST_FILE_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_json(
        filepath=FMODEL_JSON, armature_name="Cloud_FModel_Skeleton"))
except RuntimeError as exc:
    print("TEST_FILE_ERROR", str(exc))
verify(bpy.data.objects.get("Cloud_FModel_Skeleton"), "FILE")

try:
    print("TEST_PACKAGE_RESULT", bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
        "EXEC_DEFAULT", virtual_path=ASSET, armature_name="Cloud_Package_Skeleton"))
except RuntimeError as exc:
    print("TEST_PACKAGE_ERROR", str(exc))
verify(bpy.data.objects.get("Cloud_Package_Skeleton"), "PACKAGE")
