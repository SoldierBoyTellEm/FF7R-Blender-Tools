"""Import a Rebirth AnimSequence onto a package armature and check the result.

Rebirth stores animation as an ACL 1.x compressed clip that CUE4Parse cannot read;
the bridge decodes it itself (see docs/REBIRTH_ANIMSEQUENCE_FORMAT.md). This covers
the whole path: bridge decode, JSON transport, and the axis conversion in
addon/animations.py.

Two independent things are asserted.

The ACL decode is checked structurally, because no second decoder for this format
exists to diff against: rotations must stay unit length, and acl range-reduces per
segment, so a mistake there shows up as a discontinuity at a segment seam and
nowhere else.

The axis conversion is checked against the source data directly. For a bone whose
parent is also keyed, Blender's parent-relative posed matrix must equal
``D^-1 @ L @ D``, where L is the mirror-converted UE local track transform and D is
the importer's display correction.

That comparison cannot reach the root, which has no keyed parent, so the root is
covered two further ways: distance travelled per frame (invariant under both D and
the forward-axis rotation), and the absolute check that the animated character
stands up. The second one matters -- the root is the one bone whose rest and
animated frames are not related by D alone, and while that was wrong the
per-frame-travel check still passed with the whole character lying on its side.
Both forward-axis conventions are exercised, since the choice is baked into the
root's rest matrix.
"""
import bpy
import math
from mathutils import Matrix, Quaternion, Vector

BLEND = r"O:\Blender\Assets\FF7\cloud baseline.blend"
SKELETON = "End/Content/Character/Player/PC0000_00_Cloud_Standard/Model/PC0000_00_Skeleton.uasset"
ANIMATION = "End/Content/Cut/Game/0000-COMON/EV_COMON_0010/Motion/EV_COMON_0010_PC0000_00_C0060.uasset"
START_FRAME = 1
# acl segments this clip at samples 0/19/38.
SEAMS = (19, 38)

bpy.ops.wm.open_mainfile(filepath=BLEND)
bpy.ops.preferences.addon_enable(module="ff7r_rebirth_tools")
prefs = bpy.context.preferences.addons["ff7r_rebirth_tools"].preferences
prefs.rebirth_install_root = r"G:\SteamLibrary\steamapps\common\FINAL FANTASY VII REBIRTH"
prefs.rebirth_oodle_dll = r"G:\Games\Final Fantasy VII - Remake Intergrade\Engine\Binaries\ThirdParty\Oodle\Win64\oo2core_7_win64.dll"
prefs.rebirth_usmap_path = r"C:\Users\Ghouls\Downloads\4.26.1-0+++UE4+Release-4.26-End (1).usmap"

from ff7r_rebirth_tools import animations, game_packages
from ff7r_rebirth_tools.skeleton import importer as skel

bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
    "EXEC_DEFAULT", virtual_path=SKELETON, armature_name="Cloud_Package_Skeleton",
    face_y_forward=skel.FACE_FORWARD_UNREAL)
armature = bpy.data.objects.get("Cloud_Package_Skeleton")
if armature is None:
    print("TEST_NO_ARMATURE")
    raise SystemExit

bpy.context.view_layer.objects.active = armature
armature.select_set(True)

# The same payload the operator consumes, kept as the reference to compare against.
with game_packages.PackageAssetSession(
        prefs.rebirth_install_root, prefs.rebirth_oodle_dll, prefs.rebirth_usmap_path) as session:
    source = session.animation_asset(ANIMATION)
tracks = {track["boneName"]: track for track in source["tracks"]}

try:
    print("TEST_APPLY_RESULT", bpy.ops.object.ff7r_rebirth_apply_animation_game_packages(
        "EXEC_DEFAULT", virtual_path=ANIMATION, start_frame=START_FRAME))
except RuntimeError as exc:
    print("TEST_APPLY_ERROR", str(exc))
    raise SystemExit

action = armature.animation_data.action if armature.animation_data else None
if action is None:
    print("TEST_NO_ACTION")
    raise SystemExit


def action_fcurves(act):
    """Walk an action's curves on either the legacy or the 4.4+ slotted layout."""
    legacy = getattr(act, "fcurves", None)
    if legacy is not None:
        yield from legacy
        return
    for layer in act.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield from bag.fcurves


curves = list(action_fcurves(action))
print("TEST_ACTION", action.name)
print("TEST_SOURCE_FPS", action.get("ff7r_source_fps"), "DURATION", action.get("ff7r_source_duration"))
print("TEST_FCURVES", len(curves), "TRACKS", len(tracks))
print("TEST_FRAME_RANGE", tuple(round(v, 3) for v in action.frame_range))

failures = []

# --- ACL decode: unit rotations, and no discontinuity at a segment seam ---------
quats: dict[str, dict[int, list[float]]] = {}
for curve in curves:
    if not curve.data_path.endswith(".rotation_quaternion"):
        continue
    bone = curve.data_path.split('"')[1]
    channel = quats.setdefault(bone, {})
    for index, point in enumerate(curve.keyframe_points):
        channel.setdefault(index, [0.0, 0.0, 0.0, 0.0])[curve.array_index] = point.co[1]

num_frames = max((len(v) for v in quats.values()), default=0)
print("TEST_KEYED_BONES", len(quats), "FRAMES", num_frames)

worst_norm = 0.0
jumps = [0.0] * num_frames
for frames in quats.values():
    previous = None
    for index in range(len(frames)):
        q = Quaternion(frames[index])
        worst_norm = max(worst_norm, abs(q.magnitude - 1.0))
        if previous is not None:
            jumps[index] = max(jumps[index], 2 * math.degrees(math.acos(min(1.0, abs(previous.dot(q))))))
        previous = q

print(f"TEST_QUAT_NORM_ERR {worst_norm:.3e}")
if worst_norm >= 1e-4:
    failures.append("rotations are not unit length")

if num_frames > 2:
    median = sorted(jumps[1:])[len(jumps) // 2]
    ranked = sorted(range(1, num_frames), key=lambda f: -jumps[f])[:5]
    print(f"TEST_JUMP median={median:.2f}deg max={max(jumps[1:]):.2f}deg")
    print("TEST_JUMP_WORST_FRAMES", [(f, round(jumps[f], 2)) for f in ranked])
    for seam in SEAMS:
        if seam < num_frames:
            ratio = jumps[seam] / max(median, 1e-9)
            print(f"TEST_SEAM f{seam} {jumps[seam]:.2f}deg ratio={ratio:.1f}x")
            if ratio > 3.0:
                failures.append(f"segment seam at frame {seam} is discontinuous")

# --- Axis conversion: compare the pose against the UE track data ---------------
D = animations.DISPLAY_CORRECTION


def ue_local(track, frame):
    x, y, z, w = track["rotations"][frame]
    q = Quaternion((w, -x, y, -z))
    q.normalize()
    loc = Vector(track["translations"][frame]) * 0.01
    loc.y = -loc.y
    return Matrix.LocRotScale(loc, q, Vector(track["scales"][frame]))


def posed(name):
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.pose.bones[name].matrix.copy()


orphans = [n for n in tracks
           if armature.data.bones[n].parent is not None
           and armature.data.bones[n].parent.name not in tracks]
print("TEST_TRACKS_WITH_UNKEYED_PARENT", len(orphans), orphans[:5])

for frame in (0, 1, 10, num_frames // 2, num_frames - 1):
    bpy.context.scene.frame_set(START_FRAME + frame)
    worst_rot, worst_loc, worst_scale, worst_name = 0.0, 0.0, 0.0, ""
    for name, track in tracks.items():
        bone = armature.data.bones[name]
        if bone.parent is None or bone.parent.name not in tracks:
            continue
        got = posed(bone.parent.name).inverted() @ posed(name)
        want = D.inverted() @ ue_local(track, frame) @ D
        angle = math.degrees(abs(got.to_quaternion().rotation_difference(want.to_quaternion()).angle))
        angle = min(angle, 360.0 - angle)
        if angle > worst_rot:
            worst_rot, worst_name = angle, name
        worst_loc = max(worst_loc, (got.translation - want.translation).length * 100.0)
        # D is a signed permutation, so a UE scale stays diagonal through the
        # conjugation and the components must survive it exactly.
        worst_scale = max(worst_scale, max(
            abs(a - b) for a, b in zip(got.to_scale(), want.to_scale())))
    print(f"TEST_POSE_f{frame} worst_rot={worst_rot:.4f}deg ({worst_name}) "
          f"worst_loc={worst_loc:.5f}cm worst_scale={worst_scale:.3e}")
    if worst_rot > 0.05 or worst_loc > 0.01 or worst_scale > 1e-4:
        failures.append(f"pose at frame {frame} does not match the source track")

for name in (n for n in tracks if armature.data.bones[n].parent is None):
    track = tracks[name]
    worst = 0.0
    bpy.context.scene.frame_set(START_FRAME)
    previous = posed(name).translation.copy()
    for frame in range(1, len(track["translations"])):
        bpy.context.scene.frame_set(START_FRAME + frame)
        current = posed(name).translation.copy()
        want = (Vector(track["translations"][frame]) - Vector(track["translations"][frame - 1])).length
        worst = max(worst, abs((current - previous).length * 100.0 - want))
        previous = current
    print(f"TEST_ROOT_STEP {name} worst_error={worst:.6f}cm")
    if worst > 0.001:
        failures.append(f"root bone {name} does not travel with the source track")

# --- The guard the operator warns on --------------------------------------------
print("TEST_RECORDED_FORWARD", armature.get(skel.FACE_FORWARD_PROPERTY),
      "->", skel.armature_face_forward(armature))
worst_rest = 0.0
for name, track in tracks.items():
    bind = Matrix.LocRotScale(
        animations._ue_location(track["bindTranslation"]),
        animations._quaternion(track["bindRotation"]),
        Vector(track["bindScale"]))
    worst_rest = max(worst_rest, animations._rest_convention_error(
        armature.data.bones[name], bind, Matrix.Identity(4)))
print(f"TEST_REST_CONVENTION_ERR {worst_rest:.6f} (tolerance {animations.REST_CONVENTION_TOLERANCE})")
if worst_rest > animations.REST_CONVENTION_TOLERANCE:
    failures.append("rest pose does not match the clip's bind pose")

# --- The root's absolute orientation: does the character stand up? --------------
# The rest pose is known good, so the animated body must be upright too. A wrong
# root frame shows here as a ~90 degree constant tilt that no other check sees.
UPRIGHT_CLIPS = (
    ANIMATION,
    "End/Content/Cut/Game/8800-FOREE/EV_FOREE_4000/Motion/EV_FOREE_4000_PC0000_00_C0190.uasset",
    "End/Content/Cut/Game/8200-JUNOE/EV_JUNOE_4500/Motion/EV_JUNOE_4500_PC0000_00_C0228.uasset",
)
MAX_TILT_DEGREES = 25.0


def body_up():
    ev = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return (ev.pose.bones["C_Head_a"].matrix.translation
            - ev.pose.bones["C_Hip_a"].matrix.translation).normalized()


SAMPLE_BONES = ("Trans", "C_Hip_a", "C_Head_a", "L_Hand_a")
posed_by_forward: dict[str, dict[tuple[str, str, int], Matrix]] = {}

for forward in (skel.FACE_FORWARD_UNREAL, skel.FACE_FORWARD_BLENDER):
    bpy.ops.import_scene.ff7r_rebirth_skeleton_game_packages(
        "EXEC_DEFAULT", virtual_path=SKELETON, armature_name=f"Rig_{forward}",
        face_y_forward=forward)
    armature = bpy.data.objects[f"Rig_{forward}"]
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    if skel.armature_face_forward(armature) != forward:
        failures.append(f"armature did not record the {forward} forward axis")
    samples = posed_by_forward.setdefault(forward, {})
    for clip in UPRIGHT_CLIPS:
        bpy.ops.object.ff7r_rebirth_apply_animation_game_packages(
            "EXEC_DEFAULT", virtual_path=clip, start_frame=START_FRAME)
        tilts = []
        for frame in (0, 10, 25):
            bpy.context.scene.frame_set(START_FRAME + frame)
            tilts.append(math.degrees(math.acos(max(-1.0, min(1.0, body_up().z)))))
            ev = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for bone in SAMPLE_BONES:
                samples[(clip, bone, frame)] = ev.pose.bones[bone].matrix.copy()
        print(f"TEST_UPRIGHT {forward:8s} {clip.rsplit('/', 1)[-1][:44]:46s} "
              f"tilt_from_Z={[round(t, 1) for t in tilts]}")
        if max(tilts) > MAX_TILT_DEGREES:
            failures.append(f"{forward}: {clip.rsplit('/', 1)[-1]} is not upright")
    armature.select_set(False)

# Tilt is measured about Z, so it cannot tell the two forward axes apart. The
# forward choice is a -90 degree turn about Z baked into every root bone's rest
# matrix, so the whole animated result must differ by exactly that and nothing else.
worst_forward = 0.0
for key, unreal in posed_by_forward[skel.FACE_FORWARD_UNREAL].items():
    expected = skel.Y_FORWARD_ROTATION @ unreal
    actual = posed_by_forward[skel.FACE_FORWARD_BLENDER][key]
    worst_forward = max(worst_forward, max(
        abs(a - b) for ra, rb in zip(expected, actual) for a, b in zip(ra, rb)))
print(f"TEST_FORWARD_ROTATION_ERR {worst_forward:.3e} over "
      f"{len(posed_by_forward[skel.FACE_FORWARD_UNREAL])} samples")
if worst_forward > 1e-4:
    failures.append("-Y forward result is not the +X forward result rotated -90 about Z")

print("TEST_FAILURES", failures)
print("TEST_VERDICT", "PASS" if not failures else "FAIL")
