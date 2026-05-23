bl_info = {
    "name": "FF7 Rebirth map importer",
    "author": "GargoyleTech",
    "version": (2, 0, 3),
    "blender": (4, 0, 0),
    "location": "File > Import > FF7R Rebirth",
    "description": "Imports FF7R Rebirth cutscene JSON, UMAP JSON, and Massive Environment .umap files",
    "category": "Import-Export",
}

import importlib
import os
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import AddonPreferences, Menu, Operator

from . import asset_linking, cutscene_import, lights, map_import, particles, timeline_actions, worlds
from .mec import importer as mec_importer
from .mec import material as mec_material
from .mec import parser as mec_parser

for _module in (
    asset_linking,
    lights,
    particles,
    worlds,
    timeline_actions,
    cutscene_import,
    mec_material,
    mec_parser,
    mec_importer,
    map_import,
):
    importlib.reload(_module)

FF7R_REBIRTH_OT_import_mec_umap = mec_importer.FF7R_REBIRTH_OT_import_mec_umap
FF7R_REBIRTH_FH_import_mec_umap = mec_importer.FF7R_REBIRTH_FH_import_mec_umap

FileHandler = getattr(bpy.types, "FileHandler", None)


def _addon_prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def _resolve_asset_library_selection(selection: str, manual_path: str) -> str:
    if selection == asset_linking.ASSET_LIBRARY_MANUAL:
        return asset_linking.manual_asset_library_selection(manual_path)
    return selection or asset_linking.ASSET_LIBRARY_ALL


def _validate_manual_asset_library_path(selection: str, manual_path: str) -> str | None:
    if selection != asset_linking.ASSET_LIBRARY_MANUAL:
        return None

    manual_path = manual_path or ""
    if not manual_path.strip():
        return "Choose a custom prop library folder, or set Prop Library to None."

    absolute_path = bpy.path.abspath(manual_path)
    if not os.path.isdir(absolute_path):
        return f"Custom prop library folder does not exist: {absolute_path}"

    return None


def _cutscene_prefix_from_selection(filepath: str) -> str:
    """Resolve a folder or selected cutscene JSON file into the shared file prefix."""
    path = Path(bpy.path.abspath(filepath))
    if path.suffix.lower() == ".json":
        stem = path.with_suffix("")
        for suffix in ("_Camera", "_Character", "_Light"):
            if stem.name.endswith(suffix):
                return str(stem.with_name(stem.name[:-len(suffix)]))
        return str(stem)

    folder = path
    if not folder.name and folder.parent != folder:
        folder = folder.parent
    return str(folder / folder.name)


class FF7R_ImportPreferences(AddonPreferences):
    """Persistent settings shared by all FF7R importers."""

    bl_idname = __name__

    asset_library_selection: EnumProperty(
        name="Prop Library",
        description="Prop library source to scan for linked collections, objects, and actions",
        items=asset_linking.asset_library_items,
    )
    manual_asset_library_path: StringProperty(
        name="Custom Prop Library Path",
        description="Folder to scan recursively for .blend prop assets when Prop Library is set to Custom Folder",
        subtype="DIR_PATH",
        default="",
    )
    game_root: StringProperty(
        name="Game Content Root",
        description="Default exported Unreal /Game directory for recursive JSON and .umap imports. Subfolders such as 'level'",
        subtype="DIR_PATH",
        default="",
    )
    recursive_import: BoolProperty(
        name="Recursive Import",
        description="Import streaming levels referenced by EndStreamingVolume entries",
        default=True,
    )
    allow_external_recursive_json: BoolProperty(
        name="Allow Recursive JSON Outside Current Folder",
        description="Allow recursive JSON imports to follow referenced maps outside the selected JSON file's folder",
        default=False,
    )
    import_massive_environment_umaps: BoolProperty(
        name="Import Referenced Massive Environment UMAPs",
        description="Import .umap files referenced by MassiveEnvironmentComponent entries during UMAP JSON import",
        default=True,
    )
    offset_mec_opposite_faces: BoolProperty(
        name="Offset MEC Opposite Faces",
        description=(
            "Directly offset overlapping opposite-facing Massive Environment faces "
            "by 0.0005 during import; no modifiers or material changes"
        ),
        default=False,
    )
    game_texture_root: StringProperty(
        name="Texture Content Root",
        description="Local folder corresponding to Unreal /Game texture paths",
        default="",
        subtype="DIR_PATH",
    )
    texture_extension: StringProperty(
        name="Texture Extension",
        description="File extension used when resolving /Game texture paths",
        default="dds",
    )
    texture_match_by_filename: BoolProperty(
        name="Match Textures by Filename Only",
        description=(
            "Ignore /Game/ path hierarchy when loading textures. "
            "On import, builds an in-memory index of every texture file under "
            "Texture Content Root and matches by filename alone. "
            "Useful when your local texture folder layout does not mirror the game's /Game/ paths"
        ),
        default=False,
    )

    def draw(self, _context):
        layout = self.layout

        asset_box = layout.box()
        asset_box.label(text="JSON Prop Library")
        asset_box.prop(self, "asset_library_selection")
        if self.asset_library_selection == asset_linking.ASSET_LIBRARY_MANUAL:
            asset_box.prop(self, "manual_asset_library_path")

        json_box = layout.box()
        json_box.label(text="JSON Imports")
        json_box.prop(self, "game_root")
        json_box.prop(self, "recursive_import")
        external_row = json_box.row()
        external_row.enabled = self.recursive_import
        external_row.prop(self, "allow_external_recursive_json")
        json_box.prop(self, "import_massive_environment_umaps")
        mec_offset_row = json_box.row()
        mec_offset_row.enabled = self.import_massive_environment_umaps
        mec_offset_row.prop(self, "offset_mec_opposite_faces")

        texture_box = layout.box()
        texture_box.label(text="Massive Environment Textures")
        texture_box.prop(self, "game_texture_root")
        row = texture_box.row()
        row.label(text="Extension:")
        row.prop(self, "texture_extension", text="")
        texture_box.prop(self, "texture_match_by_filename")


class FF7R_REBIRTH_OT_import_cutscene_json(Operator):
    """Import an FF7R cutscene JSON folder."""

    bl_idname = "import_scene.ff7r_rebirth_cutscene_json"
    bl_label = "FF7R Cutscene JSON"
    bl_description = "Import FF7R cutscene camera, character, and light JSON files"
    bl_options = {"UNDO"}

    filepath: StringProperty(subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    import_lights: BoolProperty(
        name="Import Lights",
        default=True,
    )
    import_cameras: BoolProperty(
        name="Import Cameras",
        default=True,
    )
    clear_existing_cameras: BoolProperty(
        name="Clear Existing Cameras",
        default=False,
    )
    import_characters: BoolProperty(
        name="Import Characters",
        default=True,
    )
    camera_prefix: StringProperty(
        name="Camera Prefix",
        default="",
    )
    asset_library_selection: EnumProperty(
        name="Prop Library",
        description="Prop library source to scan for linked collections, objects, and actions",
        items=asset_linking.asset_library_items,
    )
    manual_asset_library_path: StringProperty(
        name="Custom Prop Library Path",
        description="Folder to scan recursively for .blend prop assets when Prop Library is set to Custom Folder",
        subtype="DIR_PATH",
        default="",
    )

    def execute(self, _context):
        manual_path_error = _validate_manual_asset_library_path(
            self.asset_library_selection,
            self.manual_asset_library_path,
        )
        if manual_path_error:
            self.report({"ERROR"}, manual_path_error)
            return {"CANCELLED"}

        asset_selection = _resolve_asset_library_selection(
            self.asset_library_selection,
            self.manual_asset_library_path,
        )
        file_prefix = _cutscene_prefix_from_selection(self.filepath)

        cutscene_import.import_ue_cutscene(
            file_prefix=file_prefix,
            import_lights=self.import_lights,
            import_cameras=self.import_cameras,
            clear_existing_cameras=self.clear_existing_cameras,
            import_characters=self.import_characters,
            camera_prefix=self.camera_prefix,
            asset_library_selection=asset_selection,
        )

        self.report({"INFO"}, f"Imported cutscene JSON: {Path(file_prefix).name}")
        return {"FINISHED"}

    def invoke(self, context, _event):
        prefs = _addon_prefs()
        if prefs is not None:
            self.asset_library_selection = prefs.asset_library_selection
            self.manual_asset_library_path = prefs.manual_asset_library_path
        if self.filepath:
            return context.window_manager.invoke_props_dialog(self)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "import_lights")
        layout.prop(self, "import_cameras")
        layout.prop(self, "clear_existing_cameras")
        layout.prop(self, "import_characters")
        layout.prop(self, "camera_prefix")
        layout.separator(factor=0.7)
        layout.prop(self, "asset_library_selection")
        if self.asset_library_selection == asset_linking.ASSET_LIBRARY_MANUAL:
            layout.prop(self, "manual_asset_library_path")


class FF7R_REBIRTH_OT_import_umap_json(Operator):
    """Import FF7R UMAP JSON files."""

    bl_idname = "import_scene.ff7r_rebirth_umap_json"
    bl_label = "FF7R UMAP JSON"
    bl_description = "Import FF7R UMAP JSON actors, lights, linked assets, and streaming levels"
    bl_options = {"UNDO"}

    filename_ext = ".json"

    filepath: StringProperty(subtype="FILE_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    exposure: FloatProperty(
        name="Exposure (stops)",
        description="Light intensity multiplier: intensity * 2^exposure",
        default=0.0,
        min=-16.0,
        max=16.0,
    )
    attenuation_radius_mult: FloatProperty(
        name="Attenuation Radius Multiplier",
        description="Multiplier applied to imported Unreal light attenuation radii",
        default=1.0,
        min=0.0,
        soft_min=0.0,
        soft_max=10.0,
    )
    game_root: StringProperty(
        name="Game Content Root",
        description="Path to the exported Unreal /Game directory",
        subtype="DIR_PATH",
        default="",
    )
    recursive_import: BoolProperty(
        name="Recursive Import",
        description="Import streaming levels referenced by EndStreamingVolume entries",
        default=True,
    )
    allow_external_recursive_json: BoolProperty(
        name="Allow Recursive JSON Outside Current Folder",
        description="Allow recursive JSON imports to follow referenced maps outside the selected JSON file's folder",
        default=False,
    )
    import_massive_environment_umaps: BoolProperty(
        name="Import Massive Environment UMAPs",
        description="Import .umap files referenced by MassiveEnvironmentComponent entries",
        default=True,
    )
    offset_mec_opposite_faces: BoolProperty(
        name="Offset MEC Opposite Faces",
        description=(
            "Directly offset overlapping opposite-facing Massive Environment faces "
            "by 0.0005 during .umap import; no modifiers or material changes"
        ),
        default=False,
    )
    asset_library_selection: EnumProperty(
        name="Prop Library",
        description="Prop library source to scan for linked collections and object assets",
        items=asset_linking.asset_library_items,
    )
    manual_asset_library_path: StringProperty(
        name="Custom Prop Library Path",
        description="Folder to scan recursively for .blend prop assets when Prop Library is set to Custom Folder",
        subtype="DIR_PATH",
        default="",
    )

    def execute(self, _context):
        manual_path_error = _validate_manual_asset_library_path(
            self.asset_library_selection,
            self.manual_asset_library_path,
        )
        if manual_path_error:
            self.report({"ERROR"}, manual_path_error)
            return {"CANCELLED"}

        asset_selection = _resolve_asset_library_selection(
            self.asset_library_selection,
            self.manual_asset_library_path,
        )
        map_import.ASSET_LIBRARY_SELECTION = asset_selection

        if self.files:
            directory = os.path.dirname(self.filepath)
            import_paths = [os.path.join(directory, f.name) for f in self.files]
        else:
            import_paths = [self.filepath]

        exposure_mult = 2.0 ** self.exposure
        game_root = bpy.path.abspath(self.game_root) if self.game_root else ""
        visited_paths: set[str] = set()
        imported_umap_paths: set[str] = set()
        imported_world_sky_paths: set[str] = set()
        texture_index_cache: dict[tuple[str, str], dict[str, str]] = {}
        total_created = 0
        all_missing_assets: set[str] = set()
        errors: list[tuple[str, str]] = []

        for path in import_paths:
            try:
                created, missing = map_import.import_json_file(
                    filepath=path,
                    exposure_mult=exposure_mult,
                    attenuation_radius_mult=self.attenuation_radius_mult,
                    game_root=game_root,
                    visited_paths=visited_paths,
                    recursive_import=self.recursive_import,
                    allow_external_recursive_json=self.allow_external_recursive_json,
                    import_massive_environment_umaps=(
                        self.import_massive_environment_umaps
                        and map_import.is_umap_addon_available()
                    ),
                    offset_mec_opposite_faces=self.offset_mec_opposite_faces,
                    imported_umap_paths=imported_umap_paths,
                    imported_world_sky_paths=imported_world_sky_paths,
                    texture_index_cache=texture_index_cache,
                )
                total_created += created
                all_missing_assets |= missing
            except Exception as exc:
                print(f"[FF7R Rebirth Import]   ERROR processing {os.path.basename(path)!r}: {exc}")
                errors.append((path, str(exc)))

        if all_missing_assets:
            print("[FF7R Rebirth Import] Missing collection or object assets for these names:")
            for name in sorted(all_missing_assets):
                print("  -", name)

        for path, msg in errors:
            self.report({"WARNING"}, f"{os.path.basename(path)}: {msg}")

        if not total_created and errors:
            return {"CANCELLED"}

        if total_created:
            map_import.collapse_outliner_collections()

        self.report({"INFO"}, f"Created {total_created} item(s) from {len(import_paths)} UMAP JSON file(s)")
        return {"FINISHED"}

    def invoke(self, context, _event):
        prefs = _addon_prefs()
        if prefs is not None:
            if not self.game_root:
                self.game_root = prefs.game_root
            self.recursive_import = prefs.recursive_import
            self.allow_external_recursive_json = prefs.allow_external_recursive_json
            self.import_massive_environment_umaps = prefs.import_massive_environment_umaps
            self.offset_mec_opposite_faces = prefs.offset_mec_opposite_faces
            self.asset_library_selection = prefs.asset_library_selection
            self.manual_asset_library_path = prefs.manual_asset_library_path
        if self.filepath:
            return context.window_manager.invoke_props_dialog(self)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "asset_library_selection")
        if self.asset_library_selection == asset_linking.ASSET_LIBRARY_MANUAL:
            layout.prop(self, "manual_asset_library_path")
        layout.separator(factor=0.7)
        layout.prop(self, "game_root")
        layout.prop(self, "recursive_import")
        external_row = layout.row()
        external_row.enabled = self.recursive_import
        external_row.prop(self, "allow_external_recursive_json")
        if map_import.is_umap_addon_available():
            layout.prop(self, "import_massive_environment_umaps")
            mec_offset_row = layout.row()
            mec_offset_row.enabled = self.import_massive_environment_umaps
            mec_offset_row.prop(self, "offset_mec_opposite_faces")
        layout.separator(factor=0.7)
        layout.prop(self, "exposure")
        layout.prop(self, "attenuation_radius_mult")


class TOPBAR_MT_file_import_ff7r_rebirth(Menu):
    bl_idname = "TOPBAR_MT_file_import_ff7r_rebirth"
    bl_label = "FF7R Rebirth"

    def draw(self, _context):
        layout = self.layout
        op = layout.operator(
            FF7R_REBIRTH_OT_import_cutscene_json.bl_idname,
            text="Cutscene JSON Folder",
        )
        op.filepath = ""
        op = layout.operator(
            FF7R_REBIRTH_OT_import_umap_json.bl_idname,
            text="UMAP JSON",
        )
        op.filepath = ""
        op = layout.operator(
            FF7R_REBIRTH_OT_import_mec_umap.bl_idname,
            text="Massive Environment UMAP",
        )
        op.filepath = ""


def menu_func_import(self, _context):
    self.layout.menu(TOPBAR_MT_file_import_ff7r_rebirth.bl_idname)


if FileHandler is not None:
    class FF7R_REBIRTH_FH_import_cutscene_json(FileHandler):
        bl_idname = "FF7R_REBIRTH_FH_import_cutscene_json"
        bl_label = "FF7R Cutscene JSON"
        bl_import_operator = FF7R_REBIRTH_OT_import_cutscene_json.bl_idname
        bl_file_extensions = ".json"

        @classmethod
        def poll_drop(cls, context):
            return context.region and context.region.type == "WINDOW"


    class FF7R_REBIRTH_FH_import_umap_json(FileHandler):
        bl_idname = "FF7R_REBIRTH_FH_import_umap_json"
        bl_label = "FF7R UMAP JSON"
        bl_import_operator = FF7R_REBIRTH_OT_import_umap_json.bl_idname
        bl_file_extensions = ".json"

        @classmethod
        def poll_drop(cls, context):
            return context.region and context.region.type == "WINDOW"
else:
    FF7R_REBIRTH_FH_import_cutscene_json = None
    FF7R_REBIRTH_FH_import_umap_json = None


classes = tuple(cls for cls in (
    FF7R_ImportPreferences,
    FF7R_REBIRTH_OT_import_cutscene_json,
    FF7R_REBIRTH_OT_import_umap_json,
    FF7R_REBIRTH_OT_import_mec_umap,
    TOPBAR_MT_file_import_ff7r_rebirth,
    FF7R_REBIRTH_FH_import_cutscene_json,
    FF7R_REBIRTH_FH_import_umap_json,
    FF7R_REBIRTH_FH_import_mec_umap,
) if cls is not None)


def register():
    mec_material.load_hash_table()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
