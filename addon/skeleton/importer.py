"""Build a Blender armature from a game Skeleton/SkeletalMesh bone hierarchy.

Accepts two JSON shapes:

- The compact shape produced by the FF7RGameAssetBridge "skeleton" action:
  ``{"bones": [{"name", "parentIndex", "translation", "rotation", "scale"}, ...],
  "sockets": [...], "sourceType": "USkeleton"|"USkeletalMesh"}``.
- A raw FModel export of a ``Skeleton`` asset (a JSON array whose ``"Skeleton"``-typed
  entry carries the native ``ReferenceSkeleton.FinalRefBoneInfo``/``FinalRefBonePose``
  fields, with sibling ``SkeletalMeshSocket`` entries elsewhere in the same array) —
  this lets any FModel-exported ``*_Skeleton.json`` file be imported directly.

Bone translation/rotation values are UE-space (centimeters, left-handed quaternions),
parent-relative, matching ``FTransform``. See ``ue_bone_transform_to_blender`` below
for the coordinate conversion and why it differs from the one in
``ff7r_json/map_import.py``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

MIN_BONE_LENGTH = 0.0005
DEGENERATE_LENGTH = 1.0e-5
DEFAULT_BONE_LENGTH = 0.05
LEAF_LENGTH_RATIO = 0.5

# A bone's real successor in the skeleton chain sits exactly along its +X (measured
# at 0.00 degrees throughout Cloud's skeleton); accessory children -- cloth, armour,
# prop holders -- are all >= 3 degrees off. A small tolerance therefore separates
# "the next bone in the chain" from "something bolted onto this bone".
ALIGNED_CHILD_DEGREES = 5.0
ALIGNED_CHILD_COSINE = math.cos(math.radians(ALIGNED_CHILD_DEGREES))

# Per-bone maximum display length, chosen by name suffix. These bones are purely
# functional rigging machinery the user is not meant to grab, so they are kept
# short to cut visual clutter when the whole armature is visible. Matched
# case-insensitively against the bone name, longest suffix first.
BONE_LENGTH_CLAMPS: tuple[tuple[str, float], ...] = (
    ("Kdi", 0.02),
)

# How close a bone's head must sit to its parent's (cosmetic, computed) tail before
# "connected" mode is allowed to snap them together. This is deliberately tight: a
# bone whose length was set to reach a single dominant aligned child (see the tail
# loop) lands its tail almost exactly on that child's head, so real chain joins are
# well inside this threshold; anything wider risks visibly relocating a head that
# was verified against the game data.
CONNECT_DISTANCE_THRESHOLD = 0.0001


def max_length_for_bone(bone_name: str) -> float | None:
    """Return the display-length cap for this bone, or None if it is unclamped."""
    lowered = bone_name.casefold()
    for suffix, limit in BONE_LENGTH_CLAMPS:
        if lowered.endswith(suffix.casefold()):
            return limit
    return None


def ue_bone_transform_to_blender(
        translation: tuple[float, float, float],
        rotation: tuple[float, float, float, float],
        scale_factor: float,
) -> Matrix:
    """Convert one parent-relative UE bone FTransform to a Blender matrix.

    UE (left-handed, Z-up) -> Blender (right-handed, Z-up) is the mirror
    M = diag(1, -1, 1). A transform is carried across by the similarity M T M^-1,
    which negates Y on a translation, and takes a quaternion (X, Y, Z, W) to
    (W, -X, Y, -Z) -- note it is X and Z that flip, not Y and Z.

    This deliberately differs from ``ff7r_json/map_import.py``'s
    ``rotation_from_quaternion``, which negates Y and Z. The two forms agree
    exactly whenever X = Y = 0, i.e. for yaw-only rotations about Z -- which is
    what placed level actors overwhelmingly use, so the difference never
    surfaced there. Bone bind poses carry arbitrary 3D rotations, where only the
    form below is correct: it reproduces umodel's reference hierarchy for
    PC0000_00 to within 0.05mm across all 557 bones, whereas negating Y and Z
    builds the skeleton upside down.
    """
    tx, ty, tz = translation
    rx, ry, rz, rw = rotation
    location = Vector((tx * scale_factor, -ty * scale_factor, tz * scale_factor))
    orientation = Quaternion((rw, -rx, ry, -rz))
    orientation.normalize()
    return Matrix.Translation(location) @ orientation.to_matrix().to_4x4()


def _bones_from_bridge(data: dict[str, Any]) -> list[dict[str, Any]]:
    bones = []
    for bone in data.get("bones") or []:
        translation = bone.get("translation") or (0.0, 0.0, 0.0)
        rotation = bone.get("rotation") or (0.0, 0.0, 0.0, 1.0)
        bones.append({
            "name": bone["name"],
            "parent_index": int(bone.get("parentIndex", -1)),
            "translation": tuple(translation),
            "rotation": tuple(rotation),
        })
    return bones


def _bones_from_fmodel_skeleton(skeleton_entry: dict[str, Any]) -> list[dict[str, Any]]:
    reference_skeleton = skeleton_entry.get("ReferenceSkeleton") or {}
    bone_infos = reference_skeleton.get("FinalRefBoneInfo") or []
    bone_poses = reference_skeleton.get("FinalRefBonePose") or []
    bones = []
    for index, info in enumerate(bone_infos):
        pose = bone_poses[index] if index < len(bone_poses) else {}
        translation = pose.get("Translation") or {}
        rotation = pose.get("Rotation") or {}
        bones.append({
            "name": info.get("Name") or f"Bone_{index}",
            "parent_index": int(info.get("ParentIndex", -1)),
            "translation": (
                float(translation.get("X", 0.0)),
                float(translation.get("Y", 0.0)),
                float(translation.get("Z", 0.0)),
            ),
            "rotation": (
                float(rotation.get("X", 0.0)),
                float(rotation.get("Y", 0.0)),
                float(rotation.get("Z", 0.0)),
                float(rotation.get("W", 1.0)),
            ),
        })
    return bones


def _sockets_from_fmodel_root(root: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sockets = []
    for entry in root:
        if entry.get("Type") != "SkeletalMeshSocket":
            continue
        props = entry.get("Properties") or {}
        sockets.append({
            "name": props.get("SocketName", entry.get("Name")),
            "boneName": props.get("BoneName"),
            "relativeLocation": props.get("RelativeLocation"),
            "relativeRotation": props.get("RelativeRotation"),
            "relativeScale3D": props.get("RelativeScale3D"),
        })
    return sockets


def read_skeleton_export(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return (bones, sockets, source_label) for either JSON shape described above."""

    data = json.loads(path.read_text(encoding="utf-8-sig"))

    if isinstance(data, dict) and "bones" in data:
        source_label = data.get("sourceType") or "bridge"
        return _bones_from_bridge(data), list(data.get("sockets") or []), source_label

    if isinstance(data, list):
        skeleton_entry = next((entry for entry in data if entry.get("Type") == "Skeleton"), None)
        if skeleton_entry is None:
            raise ValueError('No "Skeleton" export was found in this FModel JSON file.')
        bones = _bones_from_fmodel_skeleton(skeleton_entry)
        sockets = _sockets_from_fmodel_root(data)
        return bones, sockets, "FModel"

    raise ValueError("Unrecognized skeleton JSON shape.")


def build_armature_from_bones(
        context: Any,
        name: str,
        bones: list[dict[str, Any]],
        sockets: list[dict[str, Any]] | None = None,
        scale_factor: float = 0.01,
        connect_bones: bool = False,
) -> bpy.types.Object:
    """Create a new armature object with one edit-bone per entry in `bones`.

    Each bone's head is placed by composing its parent-relative UE bind-pose
    transform down the hierarchy (bones must list parents before children,
    which FinalRefBoneInfo/the bridge always guarantee).

    The game data carries no notion of bone length, so tails are derived: each
    bone is aimed down its UE local +X (see the axis note in the tail loop
    below), reaching its nearest child, and leaf bones take a fraction of their
    parent's length. That correction rotates only each bone's own display frame,
    never the chain used to place children, so head positions are unaffected.

    ``connect_bones`` opts a bone into Blender's "connected" mode (no visible gap
    to its parent) whenever its head already sits within ``CONNECT_DISTANCE_THRESHOLD``
    of the parent's tail. The parent's tail is moved to the imported head first, so
    enabling connected mode never changes the child's 1:1 game-space position.
    """

    if not bones:
        raise ValueError("No bones to import.")

    armature_data = bpy.data.armatures.new(f"{name}_Skeleton")
    armature_obj = bpy.data.objects.new(name, armature_data)
    context.collection.objects.link(armature_obj)
    for obj in context.selected_objects:
        obj.select_set(False)
    context.view_layer.objects.active = armature_obj
    armature_obj.select_set(True)

    bpy.ops.object.mode_set(mode="EDIT")
    try:
        _build_edit_bones(armature_data, bones, scale_factor, connect_bones)
    except Exception:
        # Never leave a half-built armature behind for the user to clean up.
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(armature_obj)
        bpy.data.armatures.remove(armature_data)
        raise
    bpy.ops.object.mode_set(mode="OBJECT")

    if sockets:
        armature_obj["ff7r_sockets"] = json.dumps(sockets, ensure_ascii=False)

    return armature_obj


def _build_edit_bones(
        armature_data: Any,
        bones: list[dict[str, Any]],
        scale_factor: float,
        connect_bones: bool,
) -> None:
    """Populate an armature's edit bones. Must be called in Edit mode."""
    edit_bones = armature_data.edit_bones
    created = []
    for bone in bones:
        edit_bone = edit_bones.new(bone["name"])
        edit_bone.head = (0.0, 0.0, 0.0)
        edit_bone.tail = (0.0, DEFAULT_BONE_LENGTH, 0.0)
        created.append(edit_bone)

    armature_space: list[Matrix] = [Matrix.Identity(4)] * len(bones)
    children_of: dict[int, list[int]] = {}
    for index, bone in enumerate(bones):
        local_matrix = ue_bone_transform_to_blender(
            bone["translation"], bone["rotation"], scale_factor
        )
        parent_index = bone["parent_index"]
        if parent_index is not None and 0 <= parent_index < index:
            armature_space[index] = armature_space[parent_index] @ local_matrix
            created[index].parent = created[parent_index]
            children_of.setdefault(parent_index, []).append(index)
        else:
            armature_space[index] = local_matrix
        created[index].matrix = armature_space[index]

    bone_length: dict[int, float] = {}
    for index, edit_bone in enumerate(created):
        basis = armature_space[index].to_3x3()
        head = armature_space[index].translation
        aim = (basis @ Vector((1.0, 0.0, 0.0))).normalized()

        # Reach the far end of this bone's own chain. Children are split by whether
        # they continue the chain (aligned with +X) or merely hang off it; taking the
        # longest aligned child skips past intermediate helpers like the deltoid and
        # femoris "_Spo" bones, which would otherwise cut an upper arm or thigh short.
        aligned: list[float] = []
        offset: list[float] = []
        for child in children_of.get(index, ()):
            delta = armature_space[child].translation - head
            distance = delta.length
            if distance < DEGENERATE_LENGTH:
                # Child sits exactly on this bone's head; it implies no length or
                # direction at all. Very common for helper/attachment bones.
                continue
            if delta.normalized().dot(aim) >= ALIGNED_CHILD_COSINE:
                aligned.append(distance)
            else:
                offset.append(distance)

        if aligned:
            length = max(aligned)
        elif offset:
            # Nothing continues the chain, so this bone only carries accessories.
            # The nearest keeps it from overshooting whatever it does hold.
            length = min(offset)
        else:
            # A leaf, or every child is coincident. Echo a fraction of the parent so
            # chain tips taper instead of collapsing into invisible stubs.
            parent_index = bones[index]["parent_index"]
            length = bone_length.get(parent_index, DEFAULT_BONE_LENGTH) * LEAF_LENGTH_RATIO
        cap = max_length_for_bone(bones[index]["name"])
        if cap is not None:
            length = min(length, cap)
        length = max(length, MIN_BONE_LENGTH)
        # Store the clamped value so children inheriting a leaf length stay in
        # proportion with what is actually drawn.
        bone_length[index] = length

        # Blender defines a bone's local +Y as head->tail, but UE skeletal bones
        # run down their local +X (children are offset along the parent's +X).
        # First aim Blender's +Y down UE's X.  The additional +90 degree roll is
        # deliberate: it swaps the displayed local X/Z frame into the convention
        # used by the existing loose-JSON KDI workflow, while leaving every head,
        # tail direction, hierarchy relation, and bind-pose location untouched.
        edit_bone.tail = head + aim * length
        edit_bone.align_roll(basis @ Vector((0.0, 0.0, 1.0)))
        edit_bone.roll += math.pi * 0.5

        if connect_bones and edit_bone.parent is not None:
            # Preserve the imported child head: move only the parent's tail before
            # enabling connected mode. Assigning use_connect first would snap the
            # child head onto the parent tail and lose its 1:1 game position.
            if (head - edit_bone.parent.tail).length <= CONNECT_DISTANCE_THRESHOLD:
                edit_bone.parent.tail = head
                edit_bone.use_connect = True


class FF7R_OT_import_skeleton_json(bpy.types.Operator, ImportHelper):
    """Build an armature from a Skeleton/SkeletalMesh JSON export"""

    bl_idname = "import_scene.ff7r_rebirth_skeleton_json"
    bl_label = "Import FF7R Skeleton JSON"
    bl_description = (
        "Build an armature matching a game Skeleton's bone hierarchy and bind pose, "
        "from a bridge skeleton export or a raw FModel Skeleton JSON export"
    )
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    armature_name: StringProperty(name="Armature Name", default="")
    scale_factor: FloatProperty(name="Scale", default=0.01, min=0.0001, max=100.0)
    connect_bones: BoolProperty(
        name="Connect bones close to their parent's tail",
        description=(
            "Move the parent's tail to the imported child head, then enable Blender's "
            "connected-bone display, preserving the child's original head position"
        ),
        default=False,
    )

    def execute(self, context):
        path = Path(self.filepath)
        try:
            bones, sockets, source_label = read_skeleton_export(path)
            armature_obj = build_armature_from_bones(
                context,
                self.armature_name.strip() or path.stem,
                bones,
                sockets=sockets,
                scale_factor=self.scale_factor,
                connect_bones=self.connect_bones,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Skeleton import failed: {exc}")
            return {"CANCELLED"}
        message = f"Imported {len(bones)} bone(s) from {source_label} as '{armature_obj.name}'"
        if sockets:
            message += f"; {len(sockets)} socket(s) stored in custom property \"ff7r_sockets\""
        self.report({"INFO"}, message)
        print(message)
        return {"FINISHED"}
