"""Search and apply Rebirth animation sequences to a selected package armature."""

from __future__ import annotations

import traceback
from typing import Iterable

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import game_packages
from .reporting import FF7R_LoggedOperator, report


def _selected_armature_skeleton_path(armature: bpy.types.Object) -> str | None:
    """Get the source Skeleton path recorded by a package armature or bound mesh."""
    direct_path = armature.get(game_packages.SKELETON_ASSET_PATH_PROPERTY)
    if isinstance(direct_path, str) and direct_path.strip():
        return direct_path

    # Older package imports did not tag their armature.  A bound Rebirth mesh
    # still carries the linked Skeleton path, which lets those existing scenes
    # use the picker without reimporting their character.
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH':
            continue
        if not any(
            modifier.type == 'ARMATURE' and modifier.object == armature
            for modifier in obj.modifiers
        ):
            continue
        linked_path = obj.get("ff7r_linked_skeleton_path")
        if isinstance(linked_path, str) and linked_path.strip():
            return linked_path
    return None


def _search_matching_animations(_self, _context, edit_text):
    return game_packages._search_virtual_animations(_self, _context, edit_text)


def _vector(value: Iterable[float], fallback: tuple[float, float, float]) -> Vector:
    parts = tuple(float(part) for part in value)
    return Vector(parts[:3] if len(parts) >= 3 else fallback)


def _quaternion(value: Iterable[float]) -> Quaternion:
    parts = tuple(float(part) for part in value)
    if len(parts) < 4:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    # CUE4Parse serializes FQuat as UE X/Y/Z/W.  This is the same mirror
    # conversion used for the imported Skeleton's bind transforms.
    result = Quaternion((parts[3], -parts[0], parts[1], -parts[2]))
    result.normalize()
    return result


def _rest_local_matrix(bone: bpy.types.Bone) -> Matrix:
    if bone.parent is None:
        return bone.matrix_local.copy()
    return bone.parent.matrix_local.inverted_safe() @ bone.matrix_local


def _blender_basis_values(
        bone: bpy.types.Bone,
        translation: Iterable[float],
        rotation: Iterable[float],
        scale: Iterable[float],
        bind_translation: Iterable[float],
        bind_rotation: Iterable[float],
        bind_scale: Iterable[float],
) -> tuple[Vector, Quaternion, Vector]:
    """Convert a UE local track transform into this add-on's bone display basis.

    Package armatures have a deliberate per-bone roll correction so Blender
    draws its +Y bone axis along Unreal's +X.  Carry that correction from the
    UE bind pose into every animated local transform before deriving
    ``matrix_basis``.  This makes animation keys agree with the existing rig
    instead of treating its display orientation as source data.
    """
    location = _vector(translation, (0.0, 0.0, 0.0)) * 0.01
    location.y = -location.y
    source_matrix = Matrix.LocRotScale(
        location,
        _quaternion(rotation),
        _vector(scale, (1.0, 1.0, 1.0)),
    )
    rest_local = _rest_local_matrix(bone)
    source_bind_location = _vector(bind_translation, (0.0, 0.0, 0.0)) * 0.01
    source_bind_location.y = -source_bind_location.y
    ue_bind = Matrix.LocRotScale(
        source_bind_location,
        _quaternion(bind_rotation),
        _vector(bind_scale, (1.0, 1.0, 1.0)),
    )
    correction = ue_bind.inverted_safe() @ rest_local
    basis = rest_local.inverted_safe() @ (source_matrix @ correction)
    return basis.decompose()


def _insert_keys(
        action: bpy.types.Action,
        bone_name: str,
        data_path: str,
        values: list[tuple[float, ...]],
        source_frames: list[float],
        frame_scale: float,
        start_frame: float,
) -> int:
    if not values:
        return 0
    dimensions = len(values[0])
    if not source_frames:
        source_frames = [0.0] * len(values)
    count = min(len(values), len(source_frames))
    for axis in range(dimensions):
        curve = action.fcurves.new(data_path=data_path, index=axis, action_group=bone_name)
        curve.keyframe_points.add(count)
        for index in range(count):
            point = curve.keyframe_points[index]
            point.co = (start_frame + source_frames[index] * frame_scale, values[index][axis])
            point.interpolation = 'LINEAR'
        curve.update()
    return count * dimensions


def _key_times(values: list, frames: list[float], num_frames: int) -> list[float]:
    if not values:
        return []
    if len(frames) == len(values):
        return [float(frame) for frame in frames]
    if len(values) == 1:
        return [0.0]
    last_frame = max(1, num_frames - 1)
    return [index * last_frame / (len(values) - 1) for index in range(len(values))]


def _sample_vector(values: list, frames: list[float], frame: float, default: Vector) -> Vector:
    if not values:
        return default.copy()
    keys = [_vector(value, tuple(default)) for value in values]
    if len(keys) == 1 or frame <= frames[0]:
        return keys[0]
    if frame >= frames[-1]:
        return keys[-1]
    for index in range(1, len(keys)):
        if frame <= frames[index]:
            span = frames[index] - frames[index - 1]
            fraction = 0.0 if span <= 0 else (frame - frames[index - 1]) / span
            return keys[index - 1].lerp(keys[index], fraction)
    return keys[-1]


def _sample_ue_quaternion(
        values: list,
        frames: list[float],
        frame: float,
        default: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    def as_ue_quaternion(value) -> Quaternion:
        parts = tuple(float(part) for part in value)
        if len(parts) < 4:
            parts = default
        return Quaternion((parts[3], parts[0], parts[1], parts[2]))

    if not values:
        return default
    keys = [as_ue_quaternion(value) for value in values]
    if len(keys) == 1 or frame <= frames[0]:
        result = keys[0]
        return (result.x, result.y, result.z, result.w)
    if frame >= frames[-1]:
        result = keys[-1]
        return (result.x, result.y, result.z, result.w)
    for index in range(1, len(keys)):
        if frame <= frames[index]:
            span = frames[index] - frames[index - 1]
            fraction = 0.0 if span <= 0 else (frame - frames[index - 1]) / span
            result = keys[index - 1].slerp(keys[index], fraction)
            return (result.x, result.y, result.z, result.w)
    result = keys[-1]
    return (result.x, result.y, result.z, result.w)


class FF7R_REBIRTH_OT_apply_animation_game_packages(FF7R_LoggedOperator):
    bl_idname = "object.ff7r_rebirth_apply_animation_game_packages"
    bl_label = "Apply Rebirth Animation"
    bl_description = "Find AnimSequence assets made for the selected package armature and apply one as a Blender Action"
    bl_options = {'REGISTER', 'UNDO'}

    virtual_path: bpy.props.StringProperty(
        name="Animation",
        description="Only AnimSequence assets linked to this armature's Rebirth Skeleton are listed",
        search=_search_matching_animations,
        search_options={'SUGGESTION'},
    )
    start_frame: bpy.props.IntProperty(
        name="Start Frame",
        description="Blender frame for the beginning of the action",
        default=1,
    )
    replace_current_action: bpy.props.BoolProperty(
        name="Set as Active Action",
        description="Assign the imported action to the selected armature immediately",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def invoke(self, context, _event):
        armature = context.active_object
        skeleton_path = _selected_armature_skeleton_path(armature)
        if not skeleton_path:
            report(self,
                {'ERROR'},
                "The selected armature has no Rebirth Skeleton source. Import it from packages, or bind a package SkeletalMesh first.",
            )
            return {'CANCELLED'}
        prefs = game_packages._preferences(context)
        if prefs is None:
            report(self, {'ERROR'}, "FF7R Rebirth add-on preferences are unavailable.")
            return {'CANCELLED'}
        try:
            game_packages.refresh_animation_index(
                bpy.path.abspath(prefs.rebirth_install_root),
                bpy.path.abspath(prefs.rebirth_oodle_dll),
                bpy.path.abspath(prefs.rebirth_usmap_path),
                skeleton_path,
            )
        except Exception as exc:
            print(f"[FF7R animation ERROR] Search failed for skeleton '{skeleton_path}':")
            traceback.print_exc()
            report(self, {'ERROR'}, f"Animation search failed: {exc}")
            return {'CANCELLED'}
        if not game_packages._VIRTUAL_ANIMATIONS:
            report(self, {'WARNING'}, "No AnimSequence assets were linked to this armature's Rebirth Skeleton.")
            return {'CANCELLED'}
        self.start_frame = context.scene.frame_current
        return context.window_manager.invoke_props_dialog(self, width=900)

    def draw(self, _context):
        layout = self.layout
        layout.label(text=f"{len(game_packages._VIRTUAL_ANIMATIONS):,} compatible AnimSequence asset(s) found", icon='ARMATURE_DATA')
        layout.prop(self, "virtual_path", icon='VIEWZOOM')
        layout.prop(self, "start_frame")
        layout.prop(self, "replace_current_action")
        layout.label(text="Keys use the scene frame rate and preserve the package rig's bone-roll convention.", icon='INFO')

    def execute(self, context):
        armature = context.active_object
        prefs = game_packages._preferences(context)
        if prefs is None or armature is None:
            report(self, {'ERROR'}, "FF7R Rebirth add-on preferences are unavailable.")
            return {'CANCELLED'}
        skeleton_path = _selected_armature_skeleton_path(armature)
        virtual_path = self.virtual_path.strip().replace("\\", "/").lstrip("/")
        if not skeleton_path:
            report(self, {'ERROR'}, "The selected armature has no Rebirth Skeleton source.")
            return {'CANCELLED'}
        try:
            compatible = game_packages.refresh_animation_index(
                bpy.path.abspath(prefs.rebirth_install_root),
                bpy.path.abspath(prefs.rebirth_oodle_dll),
                bpy.path.abspath(prefs.rebirth_usmap_path),
                skeleton_path,
            )
            if virtual_path not in compatible:
                raise ValueError("Choose an animation from the compatible package search results.")
            with game_packages.PackageAssetSession(
                bpy.path.abspath(prefs.rebirth_install_root),
                bpy.path.abspath(prefs.rebirth_oodle_dll),
                bpy.path.abspath(prefs.rebirth_usmap_path),
            ) as session:
                animation = session.animation_asset(virtual_path)
        except Exception as exc:
            print(f"[FF7R animation ERROR] Import failed for '{virtual_path}':")
            traceback.print_exc()
            report(self, {'ERROR'}, f"Animation import failed: {exc}")
            return {'CANCELLED'}

        source_fps = float(animation.get("framesPerSecond") or 0.0)
        if source_fps <= 0.0:
            source_fps = 30.0
        scene_fps = context.scene.render.fps / context.scene.render.fps_base
        frame_scale = scene_fps / source_fps
        action_name = f"{armature.name} | {animation.get('name') or virtual_path.rsplit('/', 1)[-1]}"
        action = bpy.data.actions.new(action_name)
        action["ff7r_animation_virtual_path"] = virtual_path
        action["ff7r_skeleton_asset_path"] = skeleton_path
        action["ff7r_source_fps"] = source_fps
        action["ff7r_source_duration"] = float(animation.get("duration") or 0.0)

        keyed_bones = 0
        skipped_bones = 0
        key_count = 0
        for track in animation.get("tracks") or []:
            bone_name = track.get("boneName")
            bone = armature.data.bones.get(bone_name) if bone_name else None
            if bone is None:
                skipped_bones += 1
                continue
            source_translations = list(track.get("translations") or [])
            source_rotations = list(track.get("rotations") or [])
            source_scales = list(track.get("scales") or [])
            num_frames = int(animation.get("numFrames") or 1)
            translation_frames = _key_times(
                source_translations, list(track.get("translationFrames") or []), num_frames
            )
            rotation_frames = _key_times(
                source_rotations, list(track.get("rotationFrames") or []), num_frames
            )
            scale_frames = _key_times(
                source_scales, list(track.get("scaleFrames") or []), num_frames
            )
            source_frames = sorted(set(translation_frames + rotation_frames + scale_frames))
            if not source_frames:
                continue
            bind_translation = _vector(track.get("bindTranslation") or (), (0.0, 0.0, 0.0))
            bind_rotation = tuple(track.get("bindRotation") or (0.0, 0.0, 0.0, 1.0))
            bind_scale = _vector(track.get("bindScale") or (), (1.0, 1.0, 1.0))
            bases = [
                _blender_basis_values(
                    bone,
                    _sample_vector(source_translations, translation_frames, frame, bind_translation),
                    _sample_ue_quaternion(source_rotations, rotation_frames, frame, bind_rotation),
                    _sample_vector(source_scales, scale_frames, frame, bind_scale),
                    bind_translation,
                    bind_rotation,
                    bind_scale,
                )
                for frame in source_frames
            ]
            translations = [tuple(basis[0]) for basis in bases]
            rotations = [tuple(basis[1]) for basis in bases]
            scales = [tuple(basis[2]) for basis in bases]
            path = f'pose.bones["{bone.name}"]'
            key_count += _insert_keys(action, bone.name, path + ".location", translations,
                                      source_frames, frame_scale, self.start_frame)
            key_count += _insert_keys(action, bone.name, path + ".rotation_quaternion", rotations,
                                      source_frames, frame_scale, self.start_frame)
            key_count += _insert_keys(action, bone.name, path + ".scale", scales,
                                      source_frames, frame_scale, self.start_frame)
            keyed_bones += 1

        if not action.fcurves:
            bpy.data.actions.remove(action)
            report(self, {'ERROR'}, "No animation tracks matched bones on the selected armature.")
            return {'CANCELLED'}
        action.frame_start = self.start_frame
        action.frame_end = self.start_frame + max(0.0, float(animation.get("duration") or 0.0) * scene_fps)
        if self.replace_current_action:
            armature.animation_data_create().action = action
        report(self,
            {'INFO'},
            f"Imported '{animation.get('name') or action.name}' to '{armature.name}': "
            f"{keyed_bones} bone(s), {key_count:,} channel key(s)"
            + (f"; skipped {skipped_bones} unavailable bone(s)" if skipped_bones else ""),
        )
        return {'FINISHED'}


CLASSES = (FF7R_REBIRTH_OT_apply_animation_game_packages,)
