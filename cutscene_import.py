"""FF7 Rebirth UE Sequencer -> Blender importer (cameras, characters, lights)."""

import bpy
import json
import math
import mathutils
from pathlib import Path
from collections import namedtuple
from bpy_extras import anim_utils

from . import asset_linking, lights, timeline_actions


FILE_PREFIX: str = (
    r"O:\Games\Rebirth Tools\FModel\Output\Exports"
    r"\End\Content\Cut\Game\8400-GOLDE\EV_GOLDE_4840\EV_GOLDE_4840"
)

CLEAR_EXISTING_CAMERAS: bool  = False
DEFAULT_SENSOR_WIDTH:   float = 36.0
CAMERA_PREFIX:          str   = ""
CREATE_CUT_MARKERS:     bool  = True
BIND_CUT_CAMERAS:       bool  = True
CAMERA_ACTOR_YAW_DEG:   float = -180.0

IMPORT_CHARACTERS: bool = True
ARMATURE_ACTION_FPS: float = 30.0
# Set to None to use the MovieScene DisplayRate stored in the JSON.
SEQUENCER_SOURCE_FPS: float | None = 24
FACE_SLOT_KEYWORD: str = "Facial"
CHARACTER_OBJECT_MAP: dict[str, str] = {}
ASSIGN_ACTIONS: bool = True
ACTION_PREFIX:  str  = ""
CLEAR_EXISTING_CHARACTER_ANIMATION: bool = True
SEARCH_ASSET_LIBRARIES_FOR_ACTIONS: bool = True

IMPORT_LIGHTS: bool = True
IMPORT_CAMERAS: bool = True
ASSET_LIBRARY_SELECTION: str = asset_linking.ASSET_LIBRARY_ALL
LIGHT_NAME_PREFIX: str = ""


Timing = namedtuple(
    "Timing",
    ["tick_num", "display_rate", "start_frame", "end_frame", "source_display_rate"],
)

INTERP_CONSTANT = 0
INTERP_LINEAR   = 1
INTERP_CUBIC    = 2


_UE_LIGHT_TYPES: dict[str, str] = {
    "SpotLight":            "SPOT",
    "SpotLightComponent":   "SPOT",
    "PointLight":           "POINT",
    "PointLightComponent":  "POINT",
}


def _get_light_type(spawnable: dict) -> str | None:
    """Return the Blender light type for a UE spawnable, or None if not a light."""
    obj_name = spawnable.get("ObjectTemplate", {}).get("ObjectName", "")
    return lights.blender_light_type_from_name(obj_name)


def _srgb_to_linear(v: float) -> float:
    v = max(0.0, min(1.0, v))
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _extract_default_or_curve(curve_data: dict, tick_num: int, display_rate: int,
                               frame_lo: float = -1e18, frame_hi: float = 1e18) -> list:
    """
    Like extract_curve but falls back to a single key from DefaultValue when
    the curve has no keyframes (common for static light properties).
    """
    keys = extract_curve(curve_data, tick_num, display_rate, frame_lo, frame_hi)
    if keys:
        return keys
    if curve_data.get("bHasDefaultValue", False):
        return [{"frame": max(frame_lo, 0.0) if frame_lo > -1e17 else 0.0,
                 "value": curve_data.get("DefaultValue", 0.0),
                 "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0}]
    return []


def _extract_default_or_bool_curve(curve_data: dict, tick_num: int, display_rate: int,
                                   frame_lo: float = -1e18, frame_hi: float = 1e18) -> list:
    """
    Parse a UE BoolCurve into constant keys, or fall back to DefaultValue.
    """
    times = curve_data.get("Times", [])
    values = curve_data.get("Values", [])
    cr = curve_data.get("TickResolution", {})
    curve_tick_num = cr.get("Numerator", tick_num)
    keys = []
    for t_entry, v_entry in zip(times, values):
        frame = ue_tick_to_frame(t_entry["Value"], curve_tick_num, display_rate)
        if not (frame_lo <= frame < frame_hi):
            continue
        value = v_entry.get("Value") if isinstance(v_entry, dict) else v_entry
        keys.append(dict(
            frame=frame,
            value=bool(value),
            interp=INTERP_CONSTANT,
            arrive=0.0,
            leave=0.0,
        ))
    if keys:
        return keys
    if curve_data.get("bHasDefaultValue", False):
        return [{"frame": max(frame_lo, 0.0) if frame_lo > -1e17 else 0.0,
                 "value": bool(curve_data.get("DefaultValue", False)),
                 "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0}]
    return []


def _find_prop_sections(data: list, track_paths: list, prop_names: set,
                        tick_num: int, display_rate: int,
                        track_types: set[str] | None = None) -> list:
    """
    Walk track_paths and return [(track, section, frame_lo, frame_hi)] for
    matching property-bound tracks.
    """
    out = []
    for tp in track_paths:
        track = item_at_path(data, tp)
        if track is None:
            continue
        if track_types is not None and track.get("Type") not in track_types:
            continue
        prop = (track.get("Properties", {})
                     .get("PropertyBinding", {})
                     .get("PropertyName"))
        if prop not in prop_names:
            continue
        refs = track["Properties"].get("Sections", [])
        if not refs:
            continue
        sec = item_at_path(data, refs[0]["ObjectPath"])
        if sec is None:
            continue
        sr    = sec["Properties"].get("SectionRange", {}).get("Value", {})
        lo_t  = sr.get("LowerBound", {}).get("Value", {}).get("Value", 0)
        hi_t  = sr.get("UpperBound", {}).get("Value", {}).get("Value", 0)
        out.append((track, sec,
                    ue_tick_to_frame(lo_t, tick_num, display_rate),
                    ue_tick_to_frame(hi_t, tick_num, display_rate)))
    return out



def ue_tick_to_frame(tick: int, tick_num: int, display_rate: int) -> float:
    return tick * display_rate / tick_num


def _scene_fps(scene) -> float:
    fps = float(scene.render.fps)
    fps_base = float(scene.render.fps_base) if scene.render.fps_base else 1.0
    return fps / fps_base


def _source_to_scene_time_scale(
        display_rate: float | int, source_display_rate: float | int | None) -> float:
    if source_display_rate in (None, 0):
        return 1.0
    return float(display_rate) / float(source_display_rate)


def _resolved_sequencer_source_fps(json_source_display_rate: float | int | None) -> float | int | None:
    if SEQUENCER_SOURCE_FPS in (None, 0):
        return json_source_display_rate
    return float(SEQUENCER_SOURCE_FPS)


def _set_scene_frame(scene, frame: float) -> None:
    frame_int = math.floor(frame)
    subframe = frame - frame_int
    scene.frame_set(frame_int, subframe=subframe)


def _clear_childof_inverse(obj, constraint_name: str) -> None:
    con = obj.constraints.get(constraint_name)
    if con is None:
        return

    con.influence = 1.0
    try:
        with bpy.context.temp_override(
                object=obj,
                active_object=obj,
                constraint=con,
                selected_editable_objects=[obj],
                selected_objects=[obj]):
            bpy.ops.constraint.childof_clear_inverse(
                constraint=constraint_name, owner='OBJECT')
    except Exception:
        con.inverse_matrix.identity()


def ue_loc_to_bl(x: float, y: float, z: float) -> tuple:
    return (x * 0.01, -y * 0.01, z * 0.01)


# Maps Blender camera rest axes to UE camera rest axes.
# Blender rest: forward=-Z, up=+Y  |  UE rest: forward=+X, up=+Z
# Derivation: Rx(+90) @ Ry(-90)
_CAM_FWD_FIX = (
    mathutils.Matrix.Rotation(math.radians( 90.0), 4, "X") @
    mathutils.Matrix.Rotation(math.radians(-90.0), 4, "Y")
)


def ue_rot_to_bl_euler(roll_deg: float, pitch_deg: float, yaw_deg: float
                       ) -> mathutils.Euler:
    """
    UE world-space Euler (Roll/Pitch/Yaw degrees, left-handed ZYX)
    -> Blender XYZ Euler (radians, right-handed).
    """
    yaw   = math.radians(-yaw_deg)
    pitch = math.radians(-pitch_deg)
    roll  = math.radians( roll_deg)
    Rz = mathutils.Matrix.Rotation(yaw,   4, "Z")
    Ry = mathutils.Matrix.Rotation(pitch, 4, "Y")
    Rx = mathutils.Matrix.Rotation(roll,  4, "X")
    return (Rz @ Ry @ Rx @ _CAM_FWD_FIX).to_euler("XYZ")


def combine_ue_transforms(
        atx, aty, atz, arx, ary, arz,
        ctx, cty, ctz, crx, cry, crz,
) -> tuple:
    """Combine UE CameraActor (world) + CameraComponent (local) transforms."""
    def _R(r, p, y):
        return (mathutils.Matrix.Rotation(math.radians(y), 4, "Z") @
                mathutils.Matrix.Rotation(math.radians(p), 4, "Y") @
                mathutils.Matrix.Rotation(math.radians(r), 4, "X"))
    R_a = _R(arx, ary, arz)
    R_c = _R(crx, cry, crz)
    R_w = R_a @ R_c
    pos = mathutils.Vector((atx, aty, atz)) + R_a @ mathutils.Vector((ctx, cty, ctz))
    e   = R_w.to_euler("ZYX")
    return (pos.x, pos.y, pos.z,
            math.degrees(e.x), math.degrees(e.y), math.degrees(e.z))


def _camera_debug_style_transform(
        atx: float, aty: float, atz: float,
        ctx: float, cty: float, ctz: float,
        crx: float, cry: float, crz: float,
) -> tuple[tuple[float, float, float], mathutils.Euler]:
    """
    Temporary camera import mapping.

    Actor rotation is ignored for camera orientation, but the component
    translation is still treated as actor-local and rotated by the fixed actor
    yaw before being added to the actor translation.
    """
    yaw = math.radians(CAMERA_ACTOR_YAW_DEG)
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    off_x = (ctx * cos_yaw) - (cty * sin_yaw)
    off_y = (ctx * sin_yaw) + (cty * cos_yaw)
    off_z = ctz

    world_loc = (atx + off_x, aty + off_y, atz + off_z)
    bl_rot = mathutils.Euler((
        math.radians(90.0 + cry),
        math.radians(crx),
        math.radians(90.0 - crz),
    ), "XYZ")
    return world_loc, bl_rot


def _bl_interp(ue_interp: int) -> str:
    if ue_interp == INTERP_CONSTANT: return "CONSTANT"
    if ue_interp == INTERP_LINEAR:   return "LINEAR"
    return "BEZIER"


def extract_curve(curve_data: dict, tick_num: int, display_rate: int,
                  frame_lo: float = -1e18, frame_hi: float = 1e18) -> list:
    """
    Parse a UE RichCurve dict -> list of {frame, value, interp, arrive, leave}.
    Respects per-curve TickResolution. Keys outside [frame_lo, frame_hi) are dropped.
    """
    times  = curve_data.get("Times",  [])
    values = curve_data.get("Values", [])
    cr = curve_data.get("TickResolution", {})
    curve_tick_num = cr.get("Numerator", tick_num)
    tan_scale = display_rate / curve_tick_num
    keys = []
    for t_entry, v_entry in zip(times, values):
        frame = ue_tick_to_frame(t_entry["Value"], curve_tick_num, display_rate)
        if not (frame_lo <= frame < frame_hi):
            continue
        tan = v_entry.get("Tangent", {})
        keys.append(dict(
            frame  = frame,
            value  = v_entry["Value"],
            interp = v_entry.get("InterpMode", INTERP_CONSTANT),
            arrive = tan.get("ArriveTangent", 0.0) * tan_scale,
            leave  = tan.get("LeaveTangent",  0.0) * tan_scale,
        ))
    return keys


def val_at(keys: list, frame: float, default: float = 0.0) -> float:
    """Step-sample: value of the last keyframe at or before frame."""
    if not keys:
        return default
    v = keys[0]["value"]
    for k in keys:
        if k["frame"] <= frame + 1e-6:
            v = k["value"]
        else:
            break
    return v


def interp_at(keys: list, frame: float) -> int:
    if not keys:
        return INTERP_CONSTANT
    m = keys[0]["interp"]
    for k in keys:
        if k["frame"] <= frame + 1e-6:
            m = k["interp"]
        else:
            break
    return m


def _setup_action(id_block, id_type: str, action_name: str):
    """
    Create a fresh slotted Action for id_block and return (action, slot, strip).
    Replaces any existing animation_data.action on the id_block.
    """
    action = bpy.data.actions.new(name=action_name)
    slot   = action.slots.new(id_type=id_type, name=id_block.name)
    layer  = action.layers.new(name="Layer")
    strip  = layer.strips.new(type="KEYFRAME")
    ad = id_block.animation_data_create()
    ad.action      = action
    ad.action_slot = slot
    return action, slot, strip


def _write_keys(strip, slot, data_path: str, array_index: int, keys: list) -> None:
    for k in keys:
        strip.key_insert(slot, data_path, array_index, k["value"], k["frame"])


def _apply_interp(strip, slot, data_path: str, array_index: int, keys: list) -> None:
    action = strip.id_data
    cb = anim_utils.action_ensure_channelbag_for_slot(action, slot)
    fc = cb.fcurves.find(data_path, index=array_index)
    if fc is None:
        return
    pts = list(fc.keyframe_points)
    for i, kfp in enumerate(pts):
        f     = kfp.co[0]
        imode = interp_at(keys, f)
        kfp.interpolation = _bl_interp(imode)
        if imode == INTERP_CUBIC:
            sk     = min(keys, key=lambda k: abs(k["frame"] - f), default=None)
            prev_f = pts[i - 1].co[0] if i > 0            else f - 1.0
            next_f = pts[i + 1].co[0] if i < len(pts) - 1 else f + 1.0
            dt_l   = (f - prev_f) / 3.0
            dt_r   = (next_f - f)  / 3.0
            kfp.handle_left_type  = "FREE"
            kfp.handle_right_type = "FREE"
            kfp.handle_left  = (f - dt_l, kfp.co[1] - sk["arrive"] * dt_l)
            kfp.handle_right = (f + dt_r, kfp.co[1] + sk["leave"]  * dt_r)
    fc.update()


def _force_interp(strip, slot, data_path: str, array_index: int,
                  interp: str, handle_type: str | None = None) -> None:
    """Override all keyframes on one curve with a specific Blender interpolation."""
    action = strip.id_data
    cb = anim_utils.action_ensure_channelbag_for_slot(action, slot)
    fc = cb.fcurves.find(data_path, index=array_index)
    if fc is None:
        return
    for kfp in fc.keyframe_points:
        kfp.interpolation = interp
        if handle_type and interp == "BEZIER":
            kfp.handle_left_type = handle_type
            kfp.handle_right_type = handle_type
    fc.update()


def _scale_slot_time(strip, slot, scale: float) -> None:
    """Scale all keyframe and handle times for a slotted action strip."""
    if abs(scale - 1.0) < 1e-8:
        return
    action = strip.id_data
    cb = anim_utils.action_ensure_channelbag_for_slot(action, slot)
    for fc in cb.fcurves:
        for kfp in fc.keyframe_points:
            kfp.co[0] *= scale
            kfp.handle_left[0] *= scale
            kfp.handle_right[0] *= scale
        fc.update()


def item_at_path(data: list, obj_path: str):
    """Resolve an ObjectPath string to a data-list item using its .N index suffix."""
    try:
        idx = int(obj_path.rsplit(".", 1)[-1])
        return data[idx]
    except (ValueError, IndexError):
        return None


def get_transform_section(data: list, track_path: str):
    track = item_at_path(data, track_path)
    if track is None or track["Type"] != "MovieScene3DTransformTrack":
        return None
    refs = track["Properties"].get("Sections", [])
    return item_at_path(data, refs[0]["ObjectPath"]) if refs else None


def get_float_section(data: list, track_path: str, prop_name: str,
                      tick_num: int, display_rate: int):
    """
    Resolve a FloatTrack -> (section, frame_lo, frame_hi).
    Returns (None, -1e18, 1e18) when not found or property name doesn't match.
    """
    track = item_at_path(data, track_path)
    if track is None or track["Type"] != "MovieSceneFloatTrack":
        return None, -1e18, 1e18
    if track["Properties"].get("PropertyBinding", {}).get("PropertyName") != prop_name:
        return None, -1e18, 1e18
    refs = track["Properties"].get("Sections", [])
    if not refs:
        return None, -1e18, 1e18
    sec = item_at_path(data, refs[0]["ObjectPath"])
    if sec is None:
        return None, -1e18, 1e18
    sr = sec["Properties"].get("SectionRange", {}).get("Value", {})
    lo = sr.get("LowerBound", {}).get("Value", {}).get("Value", 0)
    hi = sr.get("UpperBound", {}).get("Value", {}).get("Value", 0)
    return sec, lo * display_rate / tick_num, hi * display_rate / tick_num


def _load_json(prefix: str, suffix: str):
    """Load {prefix}{suffix}.json if it exists, else return None."""
    path = Path(prefix + suffix + ".json")
    if not path.exists():
        print(f"[UE Import] Skipping (not found): {path.name}")
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _by_type(data: list) -> dict:
    """Index a flat data list by Type string."""
    out: dict = {}
    for item in data:
        out.setdefault(item["Type"], []).append(item)
    return out


def _actor_name_to_cid(actor_name: str, scene_name: str) -> str:
    """
    Strip the scene prefix from a full UE actor name.
    'EV_GOLDE_4840_PC0000_00'  ->  'PC0000_00'
    Falls back to the original string if the prefix isn't present.
    """
    prefix = scene_name + "_"
    return actor_name[len(prefix):] if actor_name.startswith(prefix) else actor_name


def _subpath_to_cid(sub_path: str, scene_name: str) -> str:
    """
    Convert a level SubPathString to a CutsceneID.
    'PersistentLevel.EV_GOLDE_4840_PC0000_00'  ->  'PC0000_00'
    """
    return _actor_name_to_cid(sub_path.split(".")[-1], scene_name)


def _find_obj(cid: str, scene) -> bpy.types.Object | None:
    """Look up a scene object by CutsceneID, going through CHARACTER_OBJECT_MAP first."""
    name = CHARACTER_OBJECT_MAP.get(cid, cid)
    obj  = scene.objects.get(name)
    if obj is None:
        print(f"[UE Import]   WARNING: No object '{name}' for CutsceneID '{cid}'")
    return obj


def _parse_binding_refs(data: list, scene_name: str) -> dict:
    """
    Parse EndCinemaSequence.BindingReferences -> {uuid: cutscene_id}.
    Only returns entries where SubPathString is non-empty (i.e. possessable
    actors that reference an existing level actor).  Spawnable actors (lights,
    some props) have no SubPathString and are handled by _parse_actor_names.
    """
    for item in data:
        if item["Type"] == "EndCinemaSequence":
            out: dict = {}
            for entry in (item.get("Properties", {})
                              .get("BindingReferences", {})
                              .get("BindingIdToReferences", [])):
                refs = entry.get("Value", {}).get("References", [{}])
                sub  = refs[0].get("ExternalObjectPath", {}).get("SubPathString", "")
                if sub:
                    out[entry["Key"]] = _subpath_to_cid(sub, scene_name)
            return out
    return {}


def _parse_actor_names(data: list, scene_name: str) -> dict:
    """
    Build UUID -> CutsceneID from all available name sources, in priority order:

    1. EndCinemaSequence.BindingReferences - possessable actors (characters,
       props) whose SubPathString points to an existing level actor.
    2. MovieScene.Spawnables - actors spawned by the sequencer itself (lights,
       some BG assets).  These have no SubPathString; their name comes from
       the Spawnable.Name field.
    3. MovieScene.Possessables - standard UE possessables that may appear in
       Camera-type files.  Fallback for anything not already covered.

    This is used instead of _parse_binding_refs wherever a file can contain
    a mix of possessable and spawnable bindings (e.g. _Light.json).
    """
    result: dict = {}

    # Source 1: BindingReferences (non-empty SubPathString only)
    for item in data:
        if item["Type"] == "EndCinemaSequence":
            for entry in (item.get("Properties", {})
                              .get("BindingReferences", {})
                              .get("BindingIdToReferences", [])):
                refs = entry.get("Value", {}).get("References", [{}])
                sub  = refs[0].get("ExternalObjectPath", {}).get("SubPathString", "")
                if sub:
                    result[entry["Key"]] = _subpath_to_cid(sub, scene_name)
            break

    # Source 2 & 3: MovieScene Spawnables / Possessables
    for item in data:
        if item["Type"] == "MovieScene":
            props = item.get("Properties", {})
            for key in ("Spawnables", "Possessables"):
                for entry in props.get(key, []):
                    guid = entry.get("Guid", "")
                    name = entry.get("Name", "")
                    if guid and name and guid not in result:
                        result[guid] = _actor_name_to_cid(name, scene_name)
            break

    return result


def _parse_obj_bindings(data: list) -> dict:
    """
    Parse MovieScene.ObjectBindings -> {uuid: [obj_path, ...]}.
    Maps actor binding GUIDs to the track object paths that belong to them.
    """
    for item in data:
        if item["Type"] == "MovieScene":
            return {
                b["ObjectGuid"]: [t["ObjectPath"] for t in b.get("Tracks", [])]
                for b in item.get("Properties", {}).get("ObjectBindings", [])
            }
    return {}


def _set_constant_interp(obj, data_path: str, array_index: int = 0) -> None:
    """
    Set CONSTANT interpolation on an fcurve, compatible with both
    Blender 5.0 slotted actions and legacy actions.
    """
    ad = getattr(obj, "animation_data", None)
    if not ad or not ad.action:
        return
    fc = None
    slot = getattr(ad, "action_slot", None)
    if slot is not None:
        try:
            cb = anim_utils.action_ensure_channelbag_for_slot(ad.action, slot)
            fc = cb.fcurves.find(data_path, index=array_index)
        except Exception:
            pass
    if fc is None:
        try:
            fc = ad.action.fcurves.find(data_path, index=array_index)
        except Exception:
            pass
    if fc:
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT"
        fc.update()


def _clear_animation_state(id_block) -> None:
    """Clear the active action and all NLA tracks from an ID block."""
    ad = getattr(id_block, "animation_data", None)
    if ad is None:
        return
    if hasattr(ad, "action_slot") and getattr(ad, "action", None) is not None:
        try:
            ad.action_slot = None
        except RuntimeError:
            pass
    ad.action = None
    while ad.nla_tracks:
        ad.nla_tracks.remove(ad.nla_tracks[0])


def _ensure_nla_track(ad, name: str):
    """Return an existing NLA track by name or create it."""
    for track in ad.nla_tracks:
        if track.name == name:
            return track
    track = ad.nla_tracks.new()
    track.name = name
    return track


def _trim_previous_strip_for_overlap(nla_track, new_start: float, epsilon: float = 1e-4) -> None:
    """
    Ensure a new strip can start at new_start on a shared track.

    Blender NLA tracks cannot contain overlapping strips, so if the previous
    strip extends into the new strip's range, shorten it. If there is no room
    left at all, remove the older strip entirely.
    """
    if not nla_track.strips:
        return

    prev_strip = nla_track.strips[-1]
    prev_start = float(prev_strip.frame_start)
    prev_end = float(prev_strip.frame_end)
    if prev_end <= new_start + epsilon:
        return

    if new_start <= prev_start + epsilon:
        nla_track.strips.remove(prev_strip)
        return

    prev_strip.frame_end = new_start


def _find_loaded_action_by_name(action_name: str):
    """Return the loaded action, including Blender duplicate/truncation fallbacks."""
    return timeline_actions.find_loaded_action_by_name(action_name)


def _append_action_from_asset_libraries(action_name: str):
    """Append a named action from configured asset-library .blend files."""
    if not SEARCH_ASSET_LIBRARIES_FOR_ACTIONS:
        return None

    return timeline_actions.append_action_from_asset_libraries(
        action_name,
        ASSET_LIBRARY_SELECTION,
    )


def _find_action_by_name(action_name: str):
    """Return a loaded action, or append it from asset libraries on demand."""
    action = _find_loaded_action_by_name(action_name)
    if action is not None:
        return action
    return _append_action_from_asset_libraries(action_name)


def _skeletal_sections_for_binding(
        data: list, track_paths: list, tick_num: int, display_rate: int,
        source_display_rate: float | int | None = None) -> list:
    """
    Collect explicit skeletal animation sections for a single binding.
    Returns records with exact animation names and section timing.
    """
    out: list = []
    section_time_scale = _source_to_scene_time_scale(display_rate, source_display_rate)

    for tp in track_paths:
        track = item_at_path(data, tp)
        if track is None or track["Type"] != "MovieSceneSkeletalAnimationTrack":
            continue

        track_name = track.get("Name", "SkeletalAnimationTrack")
        refs = track.get("Properties", {}).get("AnimationSections", [])
        for sec_ref in refs:
            sec = item_at_path(data, sec_ref["ObjectPath"])
            if sec is None or sec["Type"] != "MovieSceneSkeletalAnimationSection":
                continue

            props  = sec.get("Properties", {})
            params = props.get("Params", {})
            anim_name = params.get("RefOnDisconnected", "")
            if not anim_name:
                continue

            sr   = props.get("SectionRange", {}).get("Value", {})
            lo_t = sr.get("LowerBound", {}).get("Value", {}).get("Value")
            hi_t = sr.get("UpperBound", {}).get("Value", {}).get("Value")
            if lo_t is None:
                continue

            start_fr = ue_tick_to_frame(lo_t, tick_num, display_rate) * section_time_scale
            end_fr = ue_tick_to_frame(
                hi_t if hi_t is not None else lo_t, tick_num, display_rate) * section_time_scale

            out.append({
                "track_name": track_name,
                "slot_name": params.get("SlotName", "DefaultSlot"),
                "anim_name": anim_name,
                "start_frame": start_fr,
                "end_frame": max(start_fr + 1, end_fr),
            })

    return sorted(
        out,
        key=lambda sec: (sec["start_frame"], sec["track_name"], sec["anim_name"]),
    )


def _extract_timing(data: list, target_frame_rate: float) -> Timing:
    """Extract Timing from a MovieScene data list. Per-curve TickResolution overrides the MovieScene value."""
    bt = _by_type(data)
    ms_props     = bt["MovieScene"][0]["Properties"]
    ms_tick_num  = ms_props["TickResolution"]["Numerator"]
    json_source_display_rate = ms_props["DisplayRate"]["Numerator"]
    source_display_rate = _resolved_sequencer_source_fps(json_source_display_rate)

    true_tick_num = ms_tick_num
    for item in data:
        if item["Type"] == "MovieScene3DTransformSection":
            for ch in ("Translation", "Rotation"):
                tr = item["Properties"].get(ch, {}).get("TickResolution", {})
                if tr.get("Numerator"):
                    true_tick_num = tr["Numerator"]
                    break
        if true_tick_num != ms_tick_num:
            break

    if true_tick_num != ms_tick_num:
        print(f"[UE Import] NOTE: TickResolution {ms_tick_num} -> {true_tick_num} "
              f"(overridden by curve TickResolution)")

    if source_display_rate != json_source_display_rate:
        print(f"[UE Import] NOTE: Sequencer source fps override "
              f"{json_source_display_rate} -> {source_display_rate}")

    tick_num = true_tick_num
    pb = ms_props.get("PlaybackRange", {}).get("Value", {})
    start_t = pb.get("LowerBound", {}).get("Value", {}).get("Value", 0)
    end_t   = pb.get("UpperBound", {}).get("Value", {}).get("Value", 0)

    return Timing(
        tick_num     = tick_num,
        display_rate = target_frame_rate,
        start_frame  = ue_tick_to_frame(start_t, tick_num, target_frame_rate),
        end_frame    = ue_tick_to_frame(end_t,   tick_num, target_frame_rate),
        source_display_rate = source_display_rate,
    )


def _build_cut_frames(
        cam_data: list, tick_num: int, display_rate: int,
        source_display_rate: float | int | None = None) -> dict:
    """Return {cut_id: start_frame} from CameraCutSections (cut_id = last segment of camera name, e.g. 'C0010')."""
    if cam_data is None:
        return {}

    bt           = _by_type(cam_data)
    ms_props     = bt.get("MovieScene", [{}])[0].get("Properties", {})
    poss_by_guid = {p["Guid"]: p for p in ms_props.get("Possessables", [])}
    cut_frames: dict = {}
    cut_time_scale = _source_to_scene_time_scale(display_rate, source_display_rate)

    for cut in bt.get("MovieSceneCameraCutSection", []):
        cp       = cut["Properties"]
        cam_guid = cp.get("CameraBindingID", {}).get("Guid", "")
        lo_tick  = (cp.get("SectionRange", {}).get("Value", {})
                      .get("LowerBound", {}).get("Value", {}).get("Value", 0))
        cam_name = poss_by_guid.get(cam_guid, {}).get("Name", "")
        if cam_name:
            cut_id = cam_name.rsplit("_", 1)[-1]   # "EV_GOLDE_4840_CAM_C0010" -> "C0010"
            cut_frames[cut_id] = (
                ue_tick_to_frame(lo_tick, tick_num, display_rate) * cut_time_scale
            )

    return cut_frames


def _import_cameras(data: list, t: Timing, scene) -> None:
    tick_num, display_rate = t.tick_num, t.display_rate
    camera_time_scale = _source_to_scene_time_scale(
        t.display_rate, getattr(t, "source_display_rate", None))
    bt = _by_type(data)

    if CLEAR_EXISTING_CAMERAS:
        removed = timeline_actions.clear_scene_cameras(scene)
        print(f"[UE Import] Cleared {removed} existing camera(s).")

    ms_props     = bt["MovieScene"][0]["Properties"]
    possessables = ms_props.get("Possessables", [])
    NULL_GUID    = "00000000-00000000-00000000-00000000"

    camera_guids = [
        p["Guid"] for p in possessables
        if "CameraActor" in str(p.get("PossessedObjectClass", ""))
        and p.get("ParentGuid", NULL_GUID) == NULL_GUID
    ]
    print(f"[UE Import] Found {len(camera_guids)} camera actor(s).")

    children_of: dict = {}
    for p in possessables:
        par = p.get("ParentGuid", NULL_GUID)
        if par != NULL_GUID:
            children_of.setdefault(par, []).append(p)

    tracks_for: dict = {
        b["ObjectGuid"]: [tr["ObjectPath"] for tr in b.get("Tracks", [])]
        for b in ms_props.get("ObjectBindings", [])
    }

    poss_by_guid = {p["Guid"]: p for p in possessables}

    guid_to_cut: dict = {}
    for cut in bt.get("MovieSceneCameraCutSection", []):
        g = cut["Properties"].get("CameraBindingID", {}).get("Guid", "")
        if g:
            guid_to_cut[g] = cut

    imported_cameras: dict = {}

    for cam_guid in camera_guids:
        cam_name = CAMERA_PREFIX + poss_by_guid[cam_guid]["Name"]
        print(f"[UE Import]   Camera: {cam_name}")

        cam_data = bpy.data.cameras.new(name=cam_name)
        cam_data.lens_unit    = "MILLIMETERS"
        cam_data.sensor_width = DEFAULT_SENSOR_WIDTH
        cam_obj = bpy.data.objects.new(name=cam_name, object_data=cam_data)
        scene.collection.objects.link(cam_obj)
        cam_obj.rotation_mode = "XYZ"
        imported_cameras[cam_guid] = cam_obj

        actor_xform_sec = None
        for tp in tracks_for.get(cam_guid, []):
            s = get_transform_section(data, tp)
            if s:
                actor_xform_sec = s
                break

        comp_xform_sec = None
        for child in children_of.get(cam_guid, []):
            if "CameraComponent" not in str(child.get("PossessedObjectClass", "")):
                continue
            for tp in tracks_for.get(child["Guid"], []):
                s = get_transform_section(data, tp)
                if s:
                    comp_xform_sec = s
                    break
            if comp_xform_sec:
                break

        if actor_xform_sec or comp_xform_sec:
            cut = guid_to_cut.get(cam_guid)
            if cut:
                cp = cut["Properties"]["SectionRange"]["Value"]
                lo = cp["LowerBound"]["Value"]["Value"] * display_rate / tick_num
                hi = cp["UpperBound"]["Value"]["Value"] * display_rate / tick_num
            else:
                lo, hi = -1e18, 1e18

            def _curve(sec, key):
                return (extract_curve(sec["Properties"].get(key, {}),
                                     tick_num, display_rate)
                        if sec else [])

            atx = _curve(actor_xform_sec, "Translation");   aty = _curve(actor_xform_sec, "Translation[1]")
            atz = _curve(actor_xform_sec, "Translation[2]"); arx = _curve(actor_xform_sec, "Rotation")
            ary = _curve(actor_xform_sec, "Rotation[1]");   arz = _curve(actor_xform_sec, "Rotation[2]")
            ctx = _curve(comp_xform_sec,  "Translation");   cty = _curve(comp_xform_sec,  "Translation[1]")
            ctz = _curve(comp_xform_sec,  "Translation[2]"); crx = _curve(comp_xform_sec,  "Rotation")
            cry = _curve(comp_xform_sec,  "Rotation[1]");   crz = _curve(comp_xform_sec,  "Rotation[2]")

            all_frames = sorted(set(
                k["frame"]
                for kl in (atx, aty, atz, arx, ary, arz, ctx, cty, ctz, crx, cry, crz)
                for k in kl
                if lo <= k["frame"] < hi
            ))

            def _def(keys): return keys[0]["value"] if keys else 0.0
            a_tx_d, a_ty_d, a_tz_d = _def(atx), _def(aty), _def(atz)
            a_rx_d, a_ry_d, a_rz_d = _def(arx), _def(ary), _def(arz)

            loc_keys = [[], [], []]
            rot_keys = [[], [], []]

            for frame in all_frames:
                a_tx = val_at(atx, frame, a_tx_d); a_ty = val_at(aty, frame, a_ty_d)
                a_tz = val_at(atz, frame, a_tz_d); a_rx = val_at(arx, frame, a_rx_d)
                a_ry = val_at(ary, frame, a_ry_d); a_rz = val_at(arz, frame, a_rz_d)
                c_tx = val_at(ctx, frame, 0.0);    c_ty = val_at(cty, frame, 0.0)
                c_tz = val_at(ctz, frame, 0.0);    c_rx = val_at(crx, frame, 0.0)
                c_ry = val_at(cry, frame, 0.0);    c_rz = val_at(crz, frame, 0.0)

                if comp_xform_sec:
                    (wx, wy, wz), bl_rot = _camera_debug_style_transform(
                        a_tx, a_ty, a_tz,
                        c_tx, c_ty, c_tz,
                        c_rx, c_ry, c_rz,
                    )
                else:
                    wx, wy, wz = a_tx, a_ty, a_tz
                    bl_rot = mathutils.Euler((0.0, 0.0, math.radians(90.0)), "XYZ")

                bl_x, bl_y, bl_z = ue_loc_to_bl(wx, wy, wz)

                imode_t = interp_at(ctx if ctx else atx, frame)
                imode_r = interp_at(crz if crz else arz, frame)

                for i, v in enumerate([bl_x, bl_y, bl_z]):
                    loc_keys[i].append(dict(frame=frame, value=v,
                                            interp=imode_t, arrive=0.0, leave=0.0))
                for i, v in enumerate([bl_rot.x, bl_rot.y, bl_rot.z]):
                    rot_keys[i].append(dict(frame=frame, value=v,
                                            interp=imode_r, arrive=0.0, leave=0.0))

            action, slot, strip = _setup_action(cam_obj, "OBJECT", cam_name)
            for i in range(3):
                _write_keys(strip, slot, "location",       i, loc_keys[i])
                _write_keys(strip, slot, "rotation_euler", i, rot_keys[i])
            for i in range(3):
                _force_interp(strip, slot, "location",       i, "BEZIER", "AUTO_CLAMPED")
                _force_interp(strip, slot, "rotation_euler", i, "BEZIER", "AUTO_CLAMPED")
            _scale_slot_time(strip, slot, camera_time_scale)

        cam_action = cam_slot = cam_strip = None

        def _ensure_cam_action():
            nonlocal cam_action, cam_slot, cam_strip
            if cam_strip is None:
                cam_action, cam_slot, cam_strip = _setup_action(
                    cam_data, "CAMERA", cam_name + "_cam")

        fl_sec = fov_sec = focus_sec = film_sec = None
        fl_lo, fl_hi     = -1e18, 1e18
        fov_lo, fov_hi   = -1e18, 1e18
        focus_lo, focus_hi = -1e18, 1e18
        film_lo, film_hi = -1e18, 1e18

        for child in children_of.get(cam_guid, []):
            for tp in tracks_for.get(child["Guid"], []):
                if fl_sec    is None: fl_sec,    fl_lo,    fl_hi    = get_float_section(data, tp, "FocalLength",   tick_num, display_rate)
                if fov_sec   is None: fov_sec,   fov_lo,   fov_hi   = get_float_section(data, tp, "FieldOfView",   tick_num, display_rate)
                if focus_sec is None: focus_sec, focus_lo, focus_hi = get_float_section(data, tp, "FocusDistance", tick_num, display_rate)
                if film_sec  is None: film_sec,  film_lo,  film_hi  = get_float_section(data, tp, "FilmWidth",     tick_num, display_rate)

        if fl_sec or fov_sec:
            if fl_sec:
                lens_keys = extract_curve(
                    fl_sec["Properties"]["FloatCurve"], tick_num, display_rate, fl_lo, fl_hi)
            else:
                raw = extract_curve(
                    fov_sec["Properties"]["FloatCurve"], tick_num, display_rate, fov_lo, fov_hi)
                lens_keys = [{**k, "value": DEFAULT_SENSOR_WIDTH /
                              (2.0 * math.tan(math.radians(k["value"]) / 2.0))}
                             for k in raw]
            _ensure_cam_action()
            _write_keys(cam_strip, cam_slot, "lens", 0, lens_keys)
            _apply_interp(cam_strip, cam_slot, "lens", 0, lens_keys)

        if film_sec:
            sw_keys = extract_curve(
                film_sec["Properties"]["FloatCurve"], tick_num, display_rate, film_lo, film_hi)
            _ensure_cam_action()
            _write_keys(cam_strip, cam_slot, "sensor_width", 0, sw_keys)
            _apply_interp(cam_strip, cam_slot, "sensor_width", 0, sw_keys)

        if focus_sec:
            cam_data.dof.use_dof = True
            fd_keys = extract_curve(
                focus_sec["Properties"]["FloatCurve"], tick_num, display_rate, focus_lo, focus_hi)
            fd_m = [{**k, "value": k["value"] * 0.01} for k in fd_keys]
            _ensure_cam_action()
            _write_keys(cam_strip, cam_slot, "dof.focus_distance", 0, fd_m)
            _apply_interp(cam_strip, cam_slot, "dof.focus_distance", 0, fd_m)

        if cam_strip is not None:
            _scale_slot_time(cam_strip, cam_slot, camera_time_scale)

    if CREATE_CUT_MARKERS:
        for cut in bt.get("MovieSceneCameraCutSection", []):
            cp = cut["Properties"]
            g  = cp.get("CameraBindingID", {}).get("Guid", "")
            lo = (cp.get("SectionRange", {}).get("Value", {})
                    .get("LowerBound", {}).get("Value", {}).get("Value", 0))
            f  = round(ue_tick_to_frame(lo, tick_num, display_rate) * camera_time_scale)
            m  = scene.timeline_markers.new(name=f"CAM_{f:04d}", frame=f)
            if BIND_CUT_CAMERAS and g in imported_cameras:
                m.camera = imported_cameras[g]
        print(f"[UE Import] Cut markers added.")

    print(f"[UE Import] Cameras: {len(imported_cameras)} imported.")


# -----------------------------------------------------------------------------
# LIGHT IMPORT
# -----------------------------------------------------------------------------

def _import_lights(data: list, t: Timing, scene, scene_name: str) -> None:
    """Phase 1: create/find spawnable lights and key properties. Phase 2: time-ranged Child Of constraints."""
    tick_num, display_rate = t.tick_num, t.display_rate
    light_time_scale = _source_to_scene_time_scale(
        display_rate, getattr(t, "source_display_rate", None))
    NULL_GUID = "00000000-00000000-00000000-00000000"

    ms_props = next(
        (item.get("Properties", {}) for item in data if item["Type"] == "MovieScene"),
        {},
    )

    spawnables_by_guid: dict = {s["Guid"]: s for s in ms_props.get("Spawnables", [])}

    possessables = ms_props.get("Possessables", [])
    children_of: dict = {}
    for p in possessables:
        par = p.get("ParentGuid", NULL_GUID)
        if par != NULL_GUID:
            children_of.setdefault(par, []).append(p)

    tracks_for: dict = {
        b["ObjectGuid"]: [tr["ObjectPath"] for tr in b.get("Tracks", [])]
        for b in ms_props.get("ObjectBindings", [])
    }

    saved_frame = scene.frame_current
    imported_lights: dict = {}

    for spawn_guid, spawnable in spawnables_by_guid.items():
        light_type = _get_light_type(spawnable)
        if light_type is None:
            continue

        light_name = LIGHT_NAME_PREFIX + spawnable["Name"]

        light_obj = scene.objects.get(light_name)
        if light_obj is None:
            bl_light = bpy.data.lights.new(name=light_name, type=light_type)
            light_obj = bpy.data.objects.new(light_name, bl_light)
            scene.collection.objects.link(light_obj)
            light_obj.rotation_mode = "XYZ"
            print(f"[UE Import]   Light (new):      '{light_name}'")
        else:
            print(f"[UE Import]   Light (existing): '{light_name}'")

        imported_lights[spawn_guid] = light_obj
        bl_light: bpy.types.Light = light_obj.data
        lights.apply_default_light_options(bl_light)

        for tp in tracks_for.get(spawn_guid, []):
            xform = get_transform_section(data, tp)
            if xform is None:
                continue
            props = xform["Properties"]
            tx = props.get("Translation",    {}).get("DefaultValue", 0.0)
            ty = props.get("Translation[1]", {}).get("DefaultValue", 0.0)
            tz = props.get("Translation[2]", {}).get("DefaultValue", 0.0)
            rx = props.get("Rotation",       {}).get("DefaultValue", 0.0)
            ry = props.get("Rotation[1]",    {}).get("DefaultValue", 0.0)
            rz = props.get("Rotation[2]",    {}).get("DefaultValue", 0.0)
            light_obj.location      = ue_loc_to_bl(tx, ty, tz)
            light_obj.rotation_euler = ue_rot_to_bl_euler(rx, ry, rz)
            break

        component_tracks: list = []
        for child in children_of.get(spawn_guid, []):
            cls = str(child.get("PossessedObjectClass", ""))
            if "SpotLightComponent" in cls or "PointLightComponent" in cls:
                component_tracks = tracks_for.get(child["Guid"], [])
                break

        light_action = light_slot = light_strip = None

        def _ensure_light_action():
            nonlocal light_action, light_slot, light_strip
            if light_strip is None:
                light_action, light_slot, light_strip = _setup_action(
                    bl_light, "LIGHT", light_name + "_light")

        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {"Intensity", "Brightness"}, tick_num, display_rate):
            curve = sec["Properties"].get("FloatCurve")
            if not curve:
                continue
            keys = _extract_default_or_curve(curve, tick_num, display_rate, lo, hi)
            if not keys:
                continue
            bl_keys = [{**k, "value": k["value"] * 0.01} for k in keys]
            _ensure_light_action()
            _write_keys(light_strip, light_slot, "energy", 0, bl_keys)
            _apply_interp(light_strip, light_slot, "energy", 0, bl_keys)

        light_color_curves = None
        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {"LightColor"}, tick_num, display_rate,
                track_types={"MovieSceneColorTrack"}):
            props = sec.get("Properties", {})
            light_color_curves = (
                _extract_default_or_curve(props.get("RedCurve",   {}), tick_num, display_rate, lo, hi),
                _extract_default_or_curve(props.get("GreenCurve", {}), tick_num, display_rate, lo, hi),
                _extract_default_or_curve(props.get("BlueCurve",  {}), tick_num, display_rate, lo, hi),
            )
            break

        temperature_curve = None
        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {"Temperature"}, tick_num, display_rate,
                track_types={"MovieSceneFloatTrack"}):
            curve = sec["Properties"].get("FloatCurve")
            if not curve:
                continue
            temperature_curve = _extract_default_or_curve(curve, tick_num, display_rate, lo, hi)
            if temperature_curve:
                break

        if light_color_curves:
            rk, gk, bk = light_color_curves if light_color_curves else ([], [], [])
            all_f = sorted(
                {k["frame"] for ks in (rk, gk, bk) for k in ks}
            )
            if all_f:
                rgb_keys: list = [[], [], []]
                for frame in all_f:
                    imode = interp_at(rk, frame) if rk else INTERP_CONSTANT
                    for i, v in enumerate((
                            val_at(rk, frame, 1.0),
                            val_at(gk, frame, 1.0),
                            val_at(bk, frame, 1.0),
                    )):
                        rgb_keys[i].append({"frame": frame, "value": v,
                                            "interp": imode, "arrive": 0.0, "leave": 0.0})
                _ensure_light_action()
                for i in range(3):
                    _write_keys(light_strip, light_slot, "color", i, rgb_keys[i])
                    _apply_interp(light_strip, light_slot, "color", i, rgb_keys[i])

        if temperature_curve:
            bl_light.use_temperature = True
            _ensure_light_action()
            _write_keys(light_strip, light_slot, "temperature", 0, temperature_curve)
            _apply_interp(light_strip, light_slot, "temperature", 0, temperature_curve)

        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {"SourceRadius"}, tick_num, display_rate,
                track_types={"MovieSceneFloatTrack"}):
            curve = sec["Properties"].get("FloatCurve")
            if not curve:
                continue
            keys = _extract_default_or_curve(curve, tick_num, display_rate, lo, hi)
            if not keys:
                continue
            bl_keys = [{**k, "value": k["value"] * 0.01} for k in keys]
            _ensure_light_action()
            _write_keys(light_strip, light_slot, "shadow_soft_size", 0, bl_keys)
            _apply_interp(light_strip, light_slot, "shadow_soft_size", 0, bl_keys)

        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {"CastShadows"}, tick_num, display_rate,
                track_types={"MovieSceneBoolTrack"}):
            curve = sec["Properties"].get("BoolCurve", {})
            keys = _extract_default_or_bool_curve(curve, tick_num, display_rate, lo, hi)
            if not keys:
                continue
            _ensure_light_action()
            _write_keys(light_strip, light_slot, "use_shadow", 0, keys)
            _apply_interp(light_strip, light_slot, "use_shadow", 0, keys)

        for _track, sec, lo, hi in _find_prop_sections(
                data, component_tracks, {lights.VOLUMETRIC_SCATTERING_PROP}, tick_num, display_rate,
                track_types={"MovieSceneFloatTrack"}):
            curve = sec["Properties"].get("FloatCurve")
            if not curve:
                continue
            keys = _extract_default_or_curve(curve, tick_num, display_rate, lo, hi)
            if not keys:
                continue
            bl_light[lights.VOLUMETRIC_SCATTERING_PROP] = keys[0]["value"]
            _ensure_light_action()
            data_path = f'["{lights.VOLUMETRIC_SCATTERING_PROP}"]'
            _write_keys(light_strip, light_slot, data_path, -1, keys)
            _apply_interp(light_strip, light_slot, data_path, -1, keys)

        if light_type == "SPOT":
            outer_deg = lights.DEFAULT_SPOT_FULL_CONE_ANGLE_DEG
            inner_deg = lights.DEFAULT_SPOT_FULL_CONE_ANGLE_DEG
            bl_light.spot_size = math.radians(outer_deg)
            bl_light.spot_blend = 0.0

            for _track, sec, lo, hi in _find_prop_sections(
                    data, component_tracks,
                    {"OuterFullConeAngle", "InnerFullConeAngle"}, tick_num, display_rate):
                prop = (_track.get("Properties", {})
                              .get("PropertyBinding", {})
                              .get("PropertyName"))
                curve = sec["Properties"].get("FloatCurve")
                if not curve:
                    continue
                keys = _extract_default_or_curve(curve, tick_num, display_rate, lo, hi)
                if not keys:
                    continue

                if prop == "OuterFullConeAngle":
                    outer_deg = keys[0]["value"]
                    bl_keys = [{**k, "value": math.radians(k["value"])} for k in keys]
                    _ensure_light_action()
                    _write_keys(light_strip, light_slot, "spot_size", 0, bl_keys)
                    _apply_interp(light_strip, light_slot, "spot_size", 0, bl_keys)

                elif prop == "InnerFullConeAngle":
                    inner_deg = keys[0]["value"]

            if outer_deg > 0:
                blend = max(0.0, min(1.0, 1.0 - inner_deg / outer_deg))
                bl_light.spot_blend = blend

    for spawn_guid, light_obj in imported_lights.items():
        for tp in tracks_for.get(spawn_guid, []):
            track = item_at_path(data, tp)
            if track is None or track["Type"] != "EndCinemaAttachTrack":
                continue

            for sec_ref in track["Properties"].get("ConstraintSections", []):
                sec = item_at_path(data, sec_ref["ObjectPath"])
                if sec is None or sec["Type"] != "EndCinemaAttachSection":
                    continue

                raw_target = sec["Properties"].get("AttachActorName", "")
                target_cid = _actor_name_to_cid(raw_target, scene_name)
                target_obj = _find_obj(target_cid, scene)
                if target_obj is None:
                    continue

                sr   = sec["Properties"].get("SectionRange", {}).get("Value", {})
                lo_t = sr.get("LowerBound", {}).get("Value", {}).get("Value", 0)
                hi_t = sr.get("UpperBound", {}).get("Value", {}).get("Value", 0)
                lo_f = ue_tick_to_frame(lo_t, tick_num, display_rate) * light_time_scale
                hi_f = ue_tick_to_frame(hi_t, tick_num, display_rate) * light_time_scale

                print(f"[UE Import]     '{light_obj.name}' -> '{target_obj.name}':Trans"
                      f"  [f{lo_f} - f{hi_f}]")

                # Evaluate the target pose at the attach frame before creating
                # the constraint so the Trans bone is up to date.
                _set_scene_frame(scene, lo_f)
                bpy.context.view_layer.update()
                depsgraph    = bpy.context.evaluated_depsgraph_get()
                target_eval  = target_obj.evaluated_get(depsgraph)

                BONE_NAME = "Trans"
                bone = target_eval.pose.bones.get(BONE_NAME)
                if not bone:
                    print(f"[UE Import]     WARNING: bone '{BONE_NAME}' not found "
                          f"in '{target_obj.name}' - falling back to object origin")

                con_name = f"Attach_{target_cid}_{round(lo_f):04d}"
                con = light_obj.constraints.new("CHILD_OF")
                con.name       = con_name
                con.target     = target_obj
                con.subtarget  = BONE_NAME   # follow the Trans bone, not the object root
                con.use_scale_x = con.use_scale_y = con.use_scale_z = False
                con.influence   = 0.0
                _clear_childof_inverse(light_obj, con_name)

                for frame, value in (
                    (lo_f - 1, 0.0),
                    (lo_f,     1.0),
                    (hi_f - 1, 1.0),
                    (hi_f,     0.0),
                ):
                    con.influence = value
                    con.keyframe_insert("influence", frame=frame)

                _set_constant_interp(light_obj,
                                     f'constraints["{con_name}"].influence')

    _set_scene_frame(scene, float(saved_frame))
    print(f"[UE Import] Lights: {len(imported_lights)} light(s) processed.")



def import_ue_cutscene(
    file_prefix: str,
    import_lights: bool | None = None,
    import_cameras: bool | None = None,
    clear_existing_cameras: bool | None = None,
    import_characters: bool | None = None,
    camera_prefix: str | None = None,
    asset_library_selection: str | None = None,
) -> None:
    """Load _Camera, _Character, _Light JSONs from file_prefix and import into the current scene."""
    global IMPORT_LIGHTS, IMPORT_CAMERAS, CLEAR_EXISTING_CAMERAS
    global IMPORT_CHARACTERS, CAMERA_PREFIX, ASSET_LIBRARY_SELECTION

    if import_lights is not None:
        IMPORT_LIGHTS = bool(import_lights)
    if import_cameras is not None:
        IMPORT_CAMERAS = bool(import_cameras)
    if clear_existing_cameras is not None:
        CLEAR_EXISTING_CAMERAS = bool(clear_existing_cameras)
    if import_characters is not None:
        IMPORT_CHARACTERS = bool(import_characters)
    if camera_prefix is not None:
        CAMERA_PREFIX = camera_prefix
    if asset_library_selection is not None:
        ASSET_LIBRARY_SELECTION = asset_library_selection

    scene_name = Path(file_prefix).name   # e.g. "EV_GOLDE_4840"
    scene      = bpy.context.scene

    cam_data   = _load_json(file_prefix, "_Camera")
    char_data  = _load_json(file_prefix, "_Character")
    light_data = _load_json(file_prefix, "_Light")

    timing_src = cam_data or char_data or light_data
    if timing_src is None:
        raise RuntimeError(
            f"[UE Import] No _Camera / _Character / _Light JSON found at:\n  {file_prefix}"
        )

    target_frame_rate = _scene_fps(scene)
    t = _extract_timing(timing_src, target_frame_rate)
    scene_time_scale = _source_to_scene_time_scale(
        t.display_rate, getattr(t, "source_display_rate", None))
    print(f"[UE Import] -- {scene_name} --------------------------------------")
    print(f"[UE Import] source {t.source_display_rate} fps -> scene {t.display_rate:g} fps | "
          f"tick/{t.tick_num} | frames {t.start_frame:.3f}-{t.end_frame:.3f}")

    scene_start = t.start_frame * scene_time_scale
    scene_end = t.end_frame * scene_time_scale
    scene.frame_start = math.floor(scene_start)
    scene.frame_end = math.ceil(scene_end)
    _set_scene_frame(scene, scene_start)

    if cam_data and IMPORT_CAMERAS:
        print("[UE Import] -- Cameras -----------------------------------------")
        _import_cameras(cam_data, t, scene)

    cut_frames = _build_cut_frames(
        cam_data, t.tick_num, t.display_rate, t.source_display_rate)
    if cut_frames:
        print(f"[UE Import] Cut frames: { {k: v for k, v in sorted(cut_frames.items())} }")

    if char_data and IMPORT_CHARACTERS:
        print("[UE Import] -- Characters --------------------------------------")
        _import_characters_explicit_sections(char_data, t, scene, scene_name, cut_frames)

    if light_data and IMPORT_LIGHTS:
        print("[UE Import] -- Lights -------------------------------------------")
        _import_lights(light_data, t, scene, scene_name)

    _set_scene_frame(scene, scene_start)
    print("[UE Import] -- Done ---------------------------------------------")


def _import_characters_explicit_sections(
        data: list, t: Timing, scene, scene_name: str,
        cut_frames: dict | None = None) -> None:
    """
    Import character animation from explicit MovieSceneSkeletalAnimationSection
    entries instead of inferring actions from action-name prefixes.
    """
    tick_num, display_rate = t.tick_num, t.display_rate
    character_time_scale = _source_to_scene_time_scale(
        display_rate, getattr(t, "source_display_rate", None))

    uuid_to_cid = _parse_binding_refs(data, scene_name)
    uuid_to_trks = _parse_obj_bindings(data)

    processed = 0
    for uuid, cid in uuid_to_cid.items():
        obj = _find_obj(cid, scene)
        if obj is None:
            continue

        print(f"[UE Import]   '{cid}' -> '{obj.name}'")
        processed += 1

        if CLEAR_EXISTING_CHARACTER_ANIMATION:
            _clear_animation_state(obj)

        ad = obj.animation_data_create()
        ad.use_nla = True

        if ASSIGN_ACTIONS:
            sections = _skeletal_sections_for_binding(
                data, uuid_to_trks.get(uuid, []), tick_num, display_rate,
                t.source_display_rate)

            if sections:
                body_sections = [s for s in sections
                                 if FACE_SLOT_KEYWORD not in s["slot_name"]]
                face_sections = [s for s in sections
                                 if FACE_SLOT_KEYWORD in s["slot_name"]]

                armature_scale = display_rate / ARMATURE_ACTION_FPS

                track_groups = (
                    ("facial", face_sections),
                    ("body", body_sections),
                )

                for group_name, group_sections in track_groups:
                    if not group_sections:
                        continue

                    nla_track = _ensure_nla_track(ad, f"{cid}_{group_name}_animation")

                    for sec in group_sections:
                        action = _find_action_by_name(sec["anim_name"])
                        if action is None:
                            print(f"[UE Import]     WARNING missing action '{sec['anim_name']}'")
                            continue

                        anim_slot = next(
                            (s for s in action.slots if s.target_id_type == "OBJECT"),
                            action.slots[0] if action.slots else None,
                        )

                        try:
                            action_start, action_end = action.frame_range
                            _trim_previous_strip_for_overlap(nla_track, sec["start_frame"])
                            nla_strip = nla_track.strips.new(
                                action.name, start=math.floor(sec["start_frame"]), action=action)
                            if hasattr(nla_strip, "action_frame_start"):
                                nla_strip.action_frame_start = float(action_start)
                            if hasattr(nla_strip, "action_frame_end"):
                                nla_strip.action_frame_end = float(action_end)
                            nla_strip.frame_start = sec["start_frame"]
                            nla_strip.frame_end   = sec["end_frame"]
                            if hasattr(nla_strip, "scale"):
                                nla_strip.scale = armature_scale
                            if hasattr(nla_strip, "blend_type"):
                                nla_strip.blend_type = "REPLACE"
                            if hasattr(nla_strip, "extrapolation"):
                                nla_strip.extrapolation = "HOLD_FORWARD"
                            if anim_slot and hasattr(nla_strip, "action_slot"):
                                nla_strip.action_slot = anim_slot
                            print(
                                f"[UE Import]     '{action.name}' -> {nla_track.name} "
                                f"f{sec['start_frame']:.1f}-{sec['end_frame']:.1f} "
                                f"scale={armature_scale:.3f}"
                            )
                        except Exception as exc:
                            print(f"[UE Import]     WARNING '{action.name}': {exc}")
            else:
                print(f"[UE Import]     No skeletal sections for '{cid}' - skipped")

        vis_lo, vis_hi = t.start_frame, t.end_frame

        for tp in uuid_to_trks.get(uuid, []):
            track = item_at_path(data, tp)
            if track is None or track["Type"] != "EndCinemaVisibilityTrack":
                continue
            for sec_ref in track["Properties"].get("Sections", []):
                sec = item_at_path(data, sec_ref["ObjectPath"])
                if sec is None or sec["Type"] != "MovieSceneBoolSection":
                    continue
                sr = sec["Properties"].get("SectionRange", {}).get("Value", {})
                lo_t = sr.get("LowerBound", {}).get("Value", {}).get("Value")
                hi_t = sr.get("UpperBound", {}).get("Value", {}).get("Value")
                if lo_t is not None:
                    vis_lo = (
                        ue_tick_to_frame(lo_t, tick_num, display_rate)
                        * character_time_scale
                    )
                if hi_t is not None:
                    vis_hi = (
                        ue_tick_to_frame(hi_t, tick_num, display_rate)
                        * character_time_scale
                    )
                break
            break

        vis_keys = [
            {"frame": vis_lo - 1, "value": 1.0, "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0},
            {"frame": vis_lo, "value": 0.0, "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0},
            {"frame": vis_hi - 1, "value": 0.0, "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0},
            {"frame": vis_hi, "value": 1.0, "interp": INTERP_CONSTANT, "arrive": 0.0, "leave": 0.0},
        ]

        _, vis_slot, vis_strip = _setup_action(obj, "OBJECT", cid + "_vis")
        for path in ("hide_viewport", "hide_render"):
            _write_keys(vis_strip, vis_slot, path, 0, vis_keys)
            _apply_interp(vis_strip, vis_slot, path, 0, vis_keys)

    print(f"[UE Import] Characters: {processed} actor(s) processed.")


if __name__ == "__main__":
    import_ue_cutscene(FILE_PREFIX)
