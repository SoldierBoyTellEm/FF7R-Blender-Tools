"""MassiveEnvironmentComponent .umap importer for the combined FF7R addon."""

import os
import json
import bpy

from .. import render_settings
from .parser import BinReader, build_import_hash_table, build_objects_from_component
from .material import load_hash_table, build_texture_index


TextureIndexCache = dict[tuple[str, str], dict[str, str]]


def _addon_package_name():
    return (__package__ or "").split(".", 1)[0]


def _get_addon_preferences(context):
    addon = context.preferences.addons.get(_addon_package_name())
    return addon.preferences if addon else None


def _get_texture_settings(context, fallback_tex_root="", fallback_tex_ext="dds"):
    prefs = _get_addon_preferences(context)
    if prefs:
        return prefs.game_texture_root, prefs.texture_extension, prefs.texture_match_by_filename
    return fallback_tex_root, fallback_tex_ext, False


def _texture_index_cache_key(tex_root: str, tex_ext: str) -> tuple[str, str]:
    return os.path.normcase(os.path.realpath(tex_root)), tex_ext.lstrip(".").lower()


def _get_texture_index(
        tex_root: str,
        tex_ext: str,
        texture_index_cache: TextureIndexCache | None,
) -> dict[str, str]:
    if texture_index_cache is None:
        return build_texture_index(tex_root, tex_ext)

    key = _texture_index_cache_key(tex_root, tex_ext)
    tex_index = texture_index_cache.get(key)
    if tex_index is None:
        tex_index = build_texture_index(tex_root, tex_ext)
        texture_index_cache[key] = tex_index
    return tex_index


def import_umap_paths(
        context,
        paths,
        *,
        lod_bias=0,
        lod_mode=None,
        lod_quality=1.0,
        lod_level=0,
        import_originals=False,
        offset_opposite_faces=False,
        scale_factor=0.01,
        tex_root=None,
        tex_ext=None,
        tex_match_by_filename=None,
        texture_index_cache: TextureIndexCache | None = None,
):
    """Import one or more .umap files from Python.

    Texture settings default to addon preferences. Passing tex_root or tex_ext
    overrides only that value. When tex_match_by_filename is True (or None and
    the preference is enabled) textures are matched by filename only, ignoring
    the full /Game/ path hierarchy.
    """
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]

    render_settings.ensure_cycles_transparent_bounces(getattr(context, "scene", None))

    pref_tex_root, pref_tex_ext, pref_match_by_filename = _get_texture_settings(context)
    if tex_root is None:
        tex_root = pref_tex_root
    if tex_ext is None:
        tex_ext = pref_tex_ext
    if tex_match_by_filename is None:
        tex_match_by_filename = pref_match_by_filename

    if tex_root:
        print(f"Texture root: {tex_root!r}  ext: {tex_ext!r}  match_by_filename: {tex_match_by_filename}")
    else:
        print("No texture root set - materials will have empty image nodes.")

    tex_index = (
        _get_texture_index(tex_root, tex_ext, texture_index_cache)
        if tex_match_by_filename and tex_root
        else None
    )

    # Reload CSV on every import run so edits take effect without restarting Blender
    load_hash_table()

    processed = 0
    skipped = 0
    lod_bias = max(0, int(lod_bias))
    if lod_mode is None:
        lod_mode = "LEVEL" if lod_bias else "QUALITY"
    lod_quality = max(0.0, min(1.0, float(lod_quality)))
    lod_level = min(0, int(lod_level))

    for path in paths:
        umap_path = os.fspath(path)
        file_name = os.path.basename(umap_path)
        base_name = os.path.splitext(file_name)[0]
        ubulk_path = os.path.splitext(umap_path)[0] + ".ubulk"

        if not os.path.isfile(umap_path):
            print(f"Skipping {file_name}: file not found")
            skipped += 1
            continue

        umap_data  = open(umap_path,  'rb').read()
        ubulk_data = open(ubulk_path, 'rb').read() if os.path.isfile(ubulk_path) else b''
        combined   = umap_data + ubulk_data
        umap_len   = len(umap_data)
        reader     = BinReader(combined)

        try:
            for _ in range(6):
                reader.read_int32()
            name_table_offset = reader.read_int32()
            reader.read_int32()
            reader.read_int32()
            name_table_size_bytes = reader.read_int32()
            export_table_offset = reader.read_int32()
            import_table_end = reader.read_int32()
            export_list_offset = reader.read_int32()
            header_size = reader.read_int32()
            bulk_data_size = reader.read_int32()
            asset_data_offset = header_size + bulk_data_size

            if export_table_offset < 0 or import_table_end < export_table_offset or import_table_end > len(umap_data):
                raise ValueError(
                    f"Invalid import table range: start={export_table_offset} end={import_table_end} "
                    f"umap_size={len(umap_data)}"
                )

            import_hashes = build_import_hash_table(umap_data, export_table_offset, import_table_end)

            name_count = name_table_size_bytes // 8 - 1
            reader.seek(name_table_offset)
            name_table = [reader.read_ue4_string() for _ in range(name_count)]

            reader.seek(export_list_offset)
            reader.read_int32()
            export_count = reader.read_int32()

            export_indices = []
            i = 0
            while i < export_count:
                idx = reader.read_int32()
                typ = reader.read_int32()
                if idx >= export_count:
                    reader.read_int32()
                    reader.read_int32()
                    export_count += 1
                elif typ == 1:
                    export_indices.append(idx)
                i += 1

            max_idx = max(export_indices) if export_indices else 0
            export_offsets = [0] * (max_idx + 1)
            export_sizes = [0] * (max_idx + 1)
            export_type_idx = [0] * (max_idx + 1)
            cur_off = asset_data_offset

            for exp_idx in export_indices:
                reader.seek(import_table_end + 72 * exp_idx)
                export_offsets[exp_idx] = cur_off
                reader.read_int64()
                size = reader.read_int64()
                export_sizes[exp_idx] = size
                cur_off += size
                type_idx = reader.read_int32()
                for _ in range(3):
                    reader.read_int32()
                for _ in range(4):
                    reader.read_uint64()
                reader.read_int32()
                reader.read_int32()
                export_type_idx[exp_idx] = type_idx

            found = False
            for exp_idx in range(max_idx + 1):
                if exp_idx not in export_indices:
                    continue
                if export_type_idx[exp_idx] >= len(name_table):
                    continue
                if name_table[export_type_idx[exp_idx]].lower() != "massiveenvironmentcomponent0":
                    continue

                import_names = None
                meta_path = os.path.splitext(umap_path)[0] + ".metadata.json"
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, encoding="utf-8") as meta_file:
                            meta = json.load(meta_file)

                        def parse_entry(entry):
                            if entry is None:
                                return None
                            path = entry.get("ObjectPath", "")
                            return (path[:-2] if path.endswith(".0") else path) or entry.get("ObjectName") or None

                        import_names = [parse_entry(entry) for entry in meta.get("ImportMap", [])]
                        print(f"Metadata: {len(import_names)} import names")
                    except Exception as ex:
                        print(f"Warning: metadata load failed: {ex}")

                build_objects_from_component(
                    context, base_name, combined, umap_len,
                    export_offsets[exp_idx], reader, name_table, import_hashes,
                    scale_factor=scale_factor,
                    create_originals=import_originals,
                    lod_bias=lod_bias,
                    lod_mode=lod_mode,
                    lod_quality=lod_quality,
                    lod_level=lod_level,
                    import_names=import_names,
                    tex_root=tex_root,
                    tex_ext=tex_ext,
                    tex_index=tex_index,
                    offset_opposite_faces=offset_opposite_faces,
                )
                found = True
                break

            if found:
                processed += 1
            else:
                print(f"No MassiveEnvironmentComponent0 found in {file_name}, skipping.")
                skipped += 1

        except Exception as e:
            print(f"Error processing {file_name}: {e}")
            import traceback; traceback.print_exc()
            skipped += 1

    return processed, skipped


# ============================================================
#  Import operator
# ============================================================

class FF7R_REBIRTH_OT_import_mec_umap(bpy.types.Operator):
    bl_idname = "import_scene.ff7r_rebirth_mec_umap"
    bl_label = "FF7R Massive Environment UMAP (.umap)"
    bl_description = (
        "Import MassiveEnvironmentComponent0 geometry from Unreal Engine .umap files"
    )
    bl_options = {'REGISTER', 'UNDO'}

    directory:   bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})
    files:       bpy.props.CollectionProperty(name="File List", type=bpy.types.OperatorFileListElement, options={'SKIP_SAVE'})
    filter_glob: bpy.props.StringProperty(default="*.umap", options={'HIDDEN'})

    # Fallback texture settings - used when addon prefs are inaccessible (Blender 4.5 bug)
    fallback_tex_root: bpy.props.StringProperty(
        name="Texture Root",
        description=(
            "Local folder corresponding to /Game/ in asset paths.\n"
            "Used only when Addon Preferences cannot be read."
        ),
        default="",
        subtype='DIR_PATH',
    )
    fallback_tex_ext: bpy.props.StringProperty(
        name="Texture extension",
        description="File extension for texture files (dds, png, tga, etc.). "
                    "Used only when Addon Preferences cannot be read.",
        default="dds",
    )

    import_originals: bpy.props.BoolProperty(
        name="Import originals at origin",
        description="Create hidden 'Original' objects at the world origin for each mesh type",
        default=False,
    )

    offset_opposite_faces: bpy.props.BoolProperty(
        name="Offset opposite overlapping faces",
        description=(
            "Directly offset vertices on overlapping opposite-facing faces by 0.0005 at 0.01 scale "
            "to reduce z-fighting; no modifiers or material changes"
        ),
        default=True,
    )

    lod_mode: bpy.props.EnumProperty(
        name="LoD level",
        description="Choose how LoD meshes are selected",
        items=(
            ("QUALITY", "Quality", "Select LoD by normalized quality: 1 is highest detail, 0 is lowest"),
            ("LEVEL", "Level", "Select LoD by explicit level offset from highest detail"),
            ("ALL", "All LoDs", "Import every available LoD level"),
        ),
        default="QUALITY",
    )

    lod_quality: bpy.props.FloatProperty(
        name="Quality",
        description="Normalized LoD quality: 1 is highest detail, 0 is lowest available LoD",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )

    lod_level: bpy.props.IntProperty(
        name="Level",
        description="Explicit LoD level offset: 0 is highest detail, negative values step toward lower detail",
        default=0,
        min=-13,
        max=0,
        soft_min=-13,
        soft_max=0,
    )

    scale_factor: bpy.props.FloatProperty(
        name="Scale",
        description="Uniform scale factor applied to all positions (e.g. 0.01 for cm to m)",
        default=0.01,
        min=0.0001, max=100.0,
        soft_min=0.001, soft_max=10.0,
    )
    
    filepath: bpy.props.StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        prefs = _get_addon_preferences(context)
        if prefs:
            self.offset_opposite_faces = getattr(prefs, "offset_mec_opposite_faces", False)
        if self.files:              # called from drag-and-drop (files already resolved)
            return context.window_manager.invoke_props_dialog(self)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "import_originals")
        layout.prop(self, "offset_opposite_faces")
        layout.label(text="LOD Level")
        layout.prop(self, "lod_mode", expand=True)
        if self.lod_mode == "QUALITY":
            layout.prop(self, "lod_quality", slider=True)
        elif self.lod_mode == "LEVEL":
            layout.prop(self, "lod_level", slider=True)
        else:
            row = layout.row()
            row.enabled = False
            row.prop(self, "lod_quality", text="", slider=True)
        layout.separator(factor=0.7)
        layout.prop(self, "scale_factor")

        prefs = _get_addon_preferences(context)
        if prefs:
            box = layout.box()
            box.label(text="Texture Lookup", icon='TEXTURE')
            box.prop(prefs, "game_texture_root")
            row = box.row()
            row.label(text="Extension:")
            row.prop(prefs, "texture_extension", text="")
        else:
            box = layout.box()
            box.label(text="Texture Lookup (Preferences unavailable)", icon='INFO')
            box.prop(self, "fallback_tex_root")
            row = box.row()
            row.label(text="Extension:")
            row.prop(self, "fallback_tex_ext", text="")

    def execute(self, context):
        paths = [os.path.join(self.directory, file_elem.name) for file_elem in self.files]
        if not paths and self.filepath:
            paths = [self.filepath]

        prefs_available = _get_addon_preferences(context) is not None

        processed, skipped = import_umap_paths(
            context,
            paths,
            lod_mode=self.lod_mode,
            lod_quality=self.lod_quality,
            lod_level=self.lod_level,
            import_originals=self.import_originals,
            offset_opposite_faces=self.offset_opposite_faces,
            scale_factor=self.scale_factor,
            tex_root=None if prefs_available else self.fallback_tex_root,
            tex_ext=None if prefs_available else self.fallback_tex_ext,
        )

        if processed == 0:
            self.report({'WARNING'}, "No valid MassiveEnvironmentComponent0 found in selected files.")
        else:
            msg = f"Imported {processed} file(s)"
            if skipped:
                msg += f" ({skipped} skipped)"
            self.report({'INFO'}, msg)

        return {'FINISHED'}

if hasattr(bpy.types, "FileHandler"):
    class FF7R_REBIRTH_FH_import_mec_umap(bpy.types.FileHandler):
        bl_idname = "FF7R_REBIRTH_FH_import_mec_umap"
        bl_label = "FF7R Massive Environment UMAP"
        bl_import_operator = FF7R_REBIRTH_OT_import_mec_umap.bl_idname
        bl_file_extensions = ".umap"

        @classmethod
        def poll_drop(cls, context):
            return context.area and context.area.type == 'VIEW_3D'
else:
    FF7R_REBIRTH_FH_import_mec_umap = None
