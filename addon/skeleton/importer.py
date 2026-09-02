"""Build a Blender armature from package-decoded Skeleton/SkeletalMesh data.

Bone translation/rotation values are UE-space (centimeters, left-handed quaternions),
parent-relative, matching ``FTransform``. See ``ue_bone_transform_to_blender`` below
for the coordinate conversion and why it differs from the one in
``json/map_import.py``.
"""

from __future__ import annotations

import json
import math
from typing import Any

import bpy
from mathutils import Matrix, Quaternion, Vector

from ..json.map_import import location_from_relative, rotation_from_relative

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

# KDI helper bones and "_Spo" pose-space helpers exist to be moved independently
# by KineDriver's generated drivers -- that is their entire purpose. Blender's
# "connected" mode locks a bone's head to its parent's tail, which would fight
# any driver trying to translate it. These must never be connected, regardless
# of the connect_bones option or how close they measure to the threshold.
NEVER_CONNECT_SUFFIXES: tuple[str, ...] = ("Kdi", "Spo")

# How close a bone's head must sit to its parent's (cosmetic, computed) tail before
# "connected" mode is allowed to snap them together. This is deliberately tight: a
# bone whose length was set to reach a single dominant aligned child (see the tail
# loop) lands its tail almost exactly on that child's head, so real chain joins are
# well inside this threshold; anything wider risks visibly relocating a head that
# was verified against the game data.
CONNECT_DISTANCE_THRESHOLD = 0.0001

# Naive IK setup: a plain IK constraint (no target, no pole) on each limb's end
# bone, plus a hinge lock on the elbow/knee so the solver can't bend it
# sideways. This is deliberately bare-bones -- the user still has to add and
# aim their own target objects; it just saves the constraint/limit busywork.
IK_END_BONES: tuple[str, ...] = ("R_Foot_a", "L_Foot_a", "R_Hand_a", "L_Hand_a")
IK_CHAIN_COUNT = 3
IK_HINGE_BONES: tuple[str, ...] = ("L_Foreleg_a", "R_Foreleg_a", "L_Forearm_a", "R_Forearm_a")


def apply_naive_ik(armature_obj: bpy.types.Object) -> tuple[int, int]:
    """Add a chain-length-3 IK constraint to each present limb end bone, and
    lock Y/Z rotation to 0 on each present elbow/knee. Missing bones are
    skipped silently. Returns (end_bones_configured, hinge_bones_configured).
    """
    pose_bones = armature_obj.pose.bones

    end_bones_configured = 0
    for bone_name in IK_END_BONES:
        pose_bone = pose_bones.get(bone_name)
        if pose_bone is None:
            continue
        constraint = pose_bone.constraints.new(type="IK")
        constraint.chain_count = IK_CHAIN_COUNT
        end_bones_configured += 1

    hinge_bones_configured = 0
    for bone_name in IK_HINGE_BONES:
        pose_bone = pose_bones.get(bone_name)
        if pose_bone is None:
            continue
        pose_bone.use_ik_limit_y = True
        pose_bone.ik_min_y = 0.0
        pose_bone.ik_max_y = 0.0
        pose_bone.use_ik_limit_z = True
        pose_bone.ik_min_z = 0.0
        pose_bone.ik_max_z = 0.0
        hinge_bones_configured += 1

    return end_bones_configured, hinge_bones_configured


def max_length_for_bone(bone_name: str) -> float | None:
    """Return the display-length cap for this bone, or None if it is unclamped."""
    lowered = bone_name.casefold()
    for suffix, limit in BONE_LENGTH_CLAMPS:
        if lowered.endswith(suffix.casefold()):
            return limit
    return None


def allows_connect(bone_name: str) -> bool:
    """Whether this bone may ever be switched into Blender's "connected" mode."""
    lowered = bone_name.casefold()
    return not any(lowered.endswith(suffix.casefold()) for suffix in NEVER_CONNECT_SUFFIXES)


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

    This deliberately differs from ``json/map_import.py``'s
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


def build_armature_from_bones(
        context: Any,
        name: str,
        bones: list[dict[str, Any]],
        sockets: list[dict[str, Any]] | None = None,
        scale_factor: float = 0.01,
        connect_bones: bool = False,
        create_socket_empties: bool = True,
        create_socket_bones: bool = False,
        setup_naive_ik: bool = False,
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
    KDI/"_Spo" bones are always excluded from this regardless of distance -- see
    ``allows_connect`` -- since KineDriver needs to translate them independently
    of their parent.

    ``create_socket_empties`` materializes each socket as a bone-parented Empty.
    ``create_socket_bones`` instead adds the sockets as non-deforming bones in a
    hidden ``Sockets`` bone collection.  Socket bones are preferable for package
    UMAP imports because their transform is part of the armature, not a separate
    scene object.  The raw socket data is stored on the armature either way.

    ``setup_naive_ik`` additionally calls ``apply_naive_ik`` -- see there for
    what it does.
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
        armature_space = _build_edit_bones(armature_data, bones, scale_factor, connect_bones)
        socket_bone_names = (
            _build_socket_edit_bones(
                armature_data, sockets, bones, armature_space, scale_factor
            )
            if sockets and create_socket_bones
            else {}
        )
    except Exception:
        # Never leave a half-built armature behind for the user to clean up.
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(armature_obj)
        bpy.data.armatures.remove(armature_data)
        raise
    bpy.ops.object.mode_set(mode="OBJECT")

    if sockets:
        armature_obj["ff7r_sockets"] = json.dumps(sockets, ensure_ascii=False)
        if socket_bone_names:
            _finalize_socket_bones(armature_obj, socket_bone_names)
        _tag_socket_bones(armature_obj, sockets, socket_bone_names)
        if create_socket_empties and not socket_bone_names:
            _create_socket_empties(
                armature_obj, sockets, bones, armature_space, scale_factor
            )

    if setup_naive_ik:
        apply_naive_ik(armature_obj)

    return armature_obj


def _socket_local_matrix(socket: dict[str, Any], scale_factor: float) -> Matrix:
    """Convert one socket's bone-relative UE offset into a Blender-space matrix.

    Handles both export shapes: the bridge emits ``translation``/``rotation`` with
    the rotation already converted to a quaternion by CUE4Parse's own
    ``FRotator.Quaternion()``, while a raw FModel export carries
    ``relativeLocation``/``relativeRotation`` as a Pitch/Yaw/Roll Rotator in
    degrees. The Rotator path reuses ``json/map_import``'s
    ``rotation_from_relative``, which was verified against the quaternion path by
    full rotation-matrix comparison rather than by decomposed Euler angles.
    """
    if "translation" in socket or "rotation" in socket:
        return ue_bone_transform_to_blender(
            tuple(socket.get("translation") or (0.0, 0.0, 0.0)),
            tuple(socket.get("rotation") or (0.0, 0.0, 0.0, 1.0)),
            scale_factor,
        )

    location = location_from_relative(socket.get("relativeLocation") or {}, scale_factor)
    rotation = rotation_from_relative(socket.get("relativeRotation") or {})
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()


def _create_socket_empties(
        armature_obj: bpy.types.Object,
        sockets: list[dict[str, Any]],
        bones: list[dict[str, Any]],
        armature_space: list[Matrix],
        scale_factor: float,
) -> int:
    """Create one bone-parented Empty per socket. Must be called in Object mode.

    The socket's offset is composed against ``armature_space`` -- the raw
    UE-converted bind transform -- and never against the edit bone's *display*
    matrix. That distinction matters: this importer aims bones down UE's local +X
    and adds a 90 degree roll, so a bone's displayed local axes are a permutation
    of the UE axes the socket offset is expressed in. Composing in UE-converted
    space and only then rebasing onto the finished bone (via ``rest_matrix``
    below, read back from Blender) keeps this correct without depending on what
    that permutation currently is.
    """
    bone_index_by_name = {bone["name"]: index for index, bone in enumerate(bones)}
    collection = armature_obj.users_collection[0] if armature_obj.users_collection else None
    created = 0

    for socket in sockets:
        socket_name = socket.get("name")
        bone_name = socket.get("boneName")
        if not socket_name or not bone_name:
            continue
        bone_index = bone_index_by_name.get(bone_name)
        bone = armature_obj.data.bones.get(bone_name)
        if bone_index is None or bone is None:
            print(f"[FF7R Skeleton] Socket '{socket_name}': bone '{bone_name}' not found; skipped.")
            continue

        socket_matrix = armature_space[bone_index] @ _socket_local_matrix(socket, scale_factor)

        empty = bpy.data.objects.new(f"{armature_obj.name}_{socket_name}", None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = 0.02
        empty["SocketName"] = socket_name
        empty["ff7r_socket_bone"] = bone_name
        if collection is not None:
            collection.objects.link(empty)

        # Blender parents to a bone's TAIL, so the effective parent frame is the
        # bone's rest matrix shifted down its own +Y by the bone's length. Undo
        # exactly that shift so the Empty lands on the socket's true position;
        # leaving matrix_parent_inverse at identity keeps the resulting local
        # transform visible (and editable) in the N panel.
        parent_frame = bone.matrix_local @ Matrix.Translation((0.0, bone.length, 0.0))
        empty.parent = armature_obj
        empty.parent_type = "BONE"
        empty.parent_bone = bone_name
        empty.matrix_parent_inverse = Matrix.Identity(4)
        empty.matrix_basis = parent_frame.inverted() @ socket_matrix
        created += 1

    return created


def _build_socket_edit_bones(
        armature_data: bpy.types.Armature,
        sockets: list[dict[str, Any]],
        bones: list[dict[str, Any]],
        armature_space: list[Matrix],
        scale_factor: float,
) -> dict[str, str]:
    """Add one non-deforming edit bone at each socket transform.

    The rest transforms are composed in exactly the same pre-display coordinate
    frame as the regular skeleton.  The bone is then aimed and rolled like an
    imported UE bone, so using it as a Blender attachment target has the same
    local axes as the socket's game transform.
    """
    bone_index_by_name = {bone["name"]: index for index, bone in enumerate(bones)}
    edit_bones = armature_data.edit_bones
    socket_bone_names: dict[str, str] = {}
    for socket in sockets:
        socket_name = socket.get("name")
        parent_name = socket.get("boneName")
        parent_index = bone_index_by_name.get(parent_name)
        parent_bone = edit_bones.get(parent_name) if parent_name else None
        if not socket_name or parent_index is None or parent_bone is None:
            print(f"[FF7R Skeleton] Socket '{socket_name}': bone '{parent_name}' not found; skipped.")
            continue

        socket_matrix = armature_space[parent_index] @ _socket_local_matrix(socket, scale_factor)
        socket_bone = edit_bones.new(socket_name)
        socket_bone.parent = parent_bone
        socket_bone.use_connect = False
        basis = socket_matrix.to_3x3()
        head = socket_matrix.translation
        aim = (basis @ Vector((1.0, 0.0, 0.0))).normalized()
        socket_bone.head = head
        socket_bone.tail = head + aim * DEFAULT_BONE_LENGTH
        socket_bone.align_roll(basis @ Vector((0.0, 0.0, 1.0)))
        socket_bone.roll += math.pi * 0.5
        socket_bone_names[socket_name] = socket_bone.name
    return socket_bone_names


def _finalize_socket_bones(
        armature_obj: bpy.types.Object,
        socket_bone_names: dict[str, str],
) -> None:
    """Move socket bones into a hidden collection and ensure they never deform."""
    armature_data = armature_obj.data
    socket_collection = armature_data.collections.get("Sockets")
    if socket_collection is None:
        socket_collection = armature_data.collections.new("Sockets")
    for socket_bone_name in socket_bone_names.values():
        bone = armature_data.bones.get(socket_bone_name)
        if bone is None:
            continue
        bone.use_deform = False
        for collection in list(bone.collections):
            collection.unassign(bone)
        socket_collection.assign(bone)
    socket_collection.is_visible = False


def _tag_socket_bones(
        armature_obj: bpy.types.Object,
        sockets: list[dict[str, Any]],
        socket_bone_names: dict[str, str] | None = None,
) -> None:
    """Tag each socket target pose bone with a SocketName custom property.

    ``json/map_import.py``'s ``find_bone_by_socket_name`` already looks for
    this property when resolving a UMAP actor's AttachSocketName against a
    skeletal actor -- it predates this importer and had nothing populating it
    until now, so package-sourced skeletons never resolved a socket attach.
    """
    pose_bones = armature_obj.pose.bones
    for socket in sockets:
        socket_name = socket.get("name")
        bone_name = (socket_bone_names or {}).get(socket_name) or socket.get("boneName")
        if not bone_name or not socket_name:
            continue
        pose_bone = pose_bones.get(bone_name)
        if pose_bone is not None:
            pose_bone["SocketName"] = socket_name


def _build_edit_bones(
        armature_data: Any,
        bones: list[dict[str, Any]],
        scale_factor: float,
        connect_bones: bool,
) -> list[Matrix]:
    """Populate an armature's edit bones. Must be called in Edit mode.

    Returns each bone's armature-space bind matrix in UE-converted axes (before
    the display-frame aim/roll applied to the edit bones), which socket placement
    needs in order to compose bone-relative UE offsets correctly.
    """
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

        if connect_bones and edit_bone.parent is not None and allows_connect(bones[index]["name"]):
            # Preserve the imported child head: move only the parent's tail before
            # enabling connected mode. Assigning use_connect first would snap the
            # child head onto the parent tail and lose its 1:1 game position.
            if (head - edit_bone.parent.tail).length <= CONNECT_DISTANCE_THRESHOLD:
                edit_bone.parent.tail = head
                edit_bone.use_connect = True

    return armature_space
