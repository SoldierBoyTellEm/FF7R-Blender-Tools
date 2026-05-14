"""
operator.py
Addon preferences and the file-import operator.

Classes
-------
UMAP_AddonPreferences   — Edit > Preferences > Add-ons
IMPORT_UMAP_OT_massive  — File > Import > FF7Rebirth MEC (.umap)
"""

import os
import json
import bpy

from .parser   import BinReader, build_import_hash_table, build_objects_from_component
from .material import load_hash_table


# ============================================================
#  Addon preferences
# ============================================================

class UMAP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    game_texture_root: bpy.props.StringProperty(
        name="Texture Content Root",
        description=(
            "Local folder that corresponds to /Game/ in UE asset paths.\n"
            "Example: if your textures live at\n"
            "  C:\\Rebirth DDS\\Environment\\Interior\\Texture\\T_foo.dds\n"
            "set this to C:\\Rebirth DDS — the importer strips /Game/ and\n"
            "appends the texture extension configured below."
        ),
        default="",
        subtype='DIR_PATH',
    )

    texture_extension: bpy.props.StringProperty(
        name="Texture Extension",
        description=(
            "File extension used when resolving /Game/... texture paths.\n"
            "Common values: dds, png, tga, tiff"
        ),
        default="dds",
    )

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Texture Lookup", icon='TEXTURE')
        box.prop(self, "game_texture_root")
        row = box.row()
        row.label(text="Extension:")
        row.prop(self, "texture_extension", text="")


# ============================================================
#  Import operator
# ============================================================

class IMPORT_UMAP_OT_massive(bpy.types.Operator):
    bl_idname   = "import_umap.massive"
    bl_label    = "FF7Rebirth Massive Environment Component (.umap)"
    bl_description = (
        "Import MassiveEnvironmentComponent0 from Unreal Engine .umap files (multi-select)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    directory:   bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})
    files:       bpy.props.CollectionProperty(name="File List", type=bpy.types.OperatorFileListElement, options={'SKIP_SAVE'})
    filter_glob: bpy.props.StringProperty(default="*.umap", options={'HIDDEN'})

    # Fallback texture settings — used when addon prefs are inaccessible (Blender 4.5 bug)
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
        name="Texture Extension",
        description="File extension for texture files (dds, png, tga …). "
                    "Used only when Addon Preferences cannot be read.",
        default="dds",
    )

    import_originals: bpy.props.BoolProperty(
        name="Import Originals",
        description="Create hidden wireframe 'Original' objects for each mesh type",
        default=False,
    )

    scale_factor: bpy.props.FloatProperty(
        name="Scale",
        description="Uniform scale factor applied to all positions (e.g. 0.01 for cm → m)",
        default=0.01,
        min=0.0001, max=100.0,
        soft_min=0.001, soft_max=10.0,
    )
    
    filepath: bpy.props.StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        if self.files:              # called from drag-and-drop (files already resolved)
            return context.window_manager.invoke_props_dialog(self)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "import_originals")
        layout.prop(self, "scale_factor")

        prefs = context.preferences.addons.get(__package__)
        if prefs:
            box = layout.box()
            box.label(text="Texture Lookup", icon='TEXTURE')
            box.prop(prefs.preferences, "game_texture_root")
            row = box.row()
            row.label(text="Extension:")
            row.prop(prefs.preferences, "texture_extension", text="")
        else:
            box = layout.box()
            box.label(text="Texture Lookup (Preferences unavailable)", icon='INFO')
            box.prop(self, "fallback_tex_root")
            row = box.row()
            row.label(text="Extension:")
            row.prop(self, "fallback_tex_ext", text="")

    def execute(self, context):
        processed = 0
        skipped   = 0

        prefs = context.preferences.addons.get(__package__)
        if prefs:
            tex_root = prefs.preferences.game_texture_root
            tex_ext  = prefs.preferences.texture_extension
        else:
            tex_root = self.fallback_tex_root
            tex_ext  = self.fallback_tex_ext

        if tex_root:
            print(f"Texture root: {tex_root!r}  ext: {tex_ext!r}")
        else:
            print("No texture root set — materials will have empty image nodes.")

        # Reload CSV on every import run so edits take effect without restarting Blender
        load_hash_table()

        for file_elem in self.files:
            umap_path  = os.path.join(self.directory, file_elem.name)
            base_name  = os.path.splitext(file_elem.name)[0]
            ubulk_path = os.path.splitext(umap_path)[0] + ".ubulk"

            if not os.path.isfile(umap_path):
                print(f"Skipping {file_elem.name}: file not found")
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
                name_table_offset   = reader.read_int32()
                reader.read_int32()
                reader.read_int32()
                name_table_size_bytes               = reader.read_int32()
                export_table_offset = reader.read_int32()
                import_table_end               = reader.read_int32()
                export_list_offset         = reader.read_int32()
                header_size        = reader.read_int32()
                bulk_data_size        = reader.read_int32()
                asset_data_offset    = header_size + bulk_data_size

                import_hashes = build_import_hash_table(umap_data, export_table_offset, import_table_end)

                reader.seek(export_table_offset)
                import_entry_count = (import_table_end - export_table_offset) // 8
                for _ in range(1, import_entry_count + 1):
                    reader.read_uint64()

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

                max_idx         = max(export_indices) if export_indices else 0
                export_offsets  = [0] * (max_idx + 1)
                export_sizes    = [0] * (max_idx + 1)
                export_type_idx = [0] * (max_idx + 1)
                cur_off         = asset_data_offset

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

                    _import_names = None
                    _meta_path    = os.path.splitext(umap_path)[0] + ".metadata.json"
                    if os.path.isfile(_meta_path):
                        try:
                            _meta = json.load(open(_meta_path, encoding="utf-8"))
                            def _pe(e):
                                if e is None: return None
                                p = e.get("ObjectPath", "")
                                return (p[:-2] if p.endswith(".0") else p) or e.get("ObjectName") or None
                            _import_names = [_pe(e) for e in _meta.get("ImportMap", [])]
                            print(f"Metadata: {len(_import_names)} import names")
                        except Exception as ex:
                            print(f"Warning: metadata load failed: {ex}")

                    build_objects_from_component(
                        context, base_name, combined, umap_len,
                        export_offsets[exp_idx], reader, name_table, import_hashes,
                        scale_factor     = self.scale_factor,
                        create_originals = self.import_originals,
                        import_names     = _import_names,
                        tex_root         = tex_root,
                        tex_ext          = tex_ext,
                    )
                    found = True
                    break

                if found:
                    processed += 1
                else:
                    print(f"No MassiveEnvironmentComponent0 found in {file_elem.name}, skipping.")
                    skipped += 1

            except Exception as e:
                print(f"Error processing {file_elem.name}: {e}")
                import traceback; traceback.print_exc()
                skipped += 1

        if processed == 0:
            self.report({'WARNING'}, "No valid MassiveEnvironmentComponent0 found in selected files.")
        else:
            msg = f"Imported {processed} file(s)"
            if skipped:
                msg += f" ({skipped} skipped)"
            self.report({'INFO'}, msg)

        return {'FINISHED'}

class UMAP_FH_import(bpy.types.FileHandler):
    bl_idname          = "UMAP_FH_import"
    bl_label           = "FF7Rebirth Massive Environment Component"
    bl_import_operator = "import_umap.massive"
    bl_file_extensions = ".umap"

    @classmethod
    def poll_drop(cls, context):
        return context.area and context.area.type == 'VIEW_3D'