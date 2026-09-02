bl_info = {
    "name": "FF7R Rebirth Tools",
    "author": "GargoyleTech, GhoulCulture",
    "version": (0, 5, 0),
    "blender": (4, 0, 0),
    "location": "File > Import > FF7R Rebirth; Object > Retrilogy tools",
    "description": "FF7R package/static-mesh, UMAP, KineDriver, and cleanup tools",
    "category": "Import-Export",
}

import importlib
import os
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import AddonPreferences, Menu, Operator

from . import animations, game_packages, render_settings, rmi_surface, z_fighting
from .reporting import FF7R_LoggedOperator
from .kdi import audit as kdi_audit
from .kdi import drivers as kdi_drivers
from .kdi import nodetree as kdi_nodetree
from .json import asset_linking, cutscene_import, lights, particles, timeline_actions, worlds
from .mec import importer as mec_importer
from .mec import material as mec_material
from .mec import parser as mec_parser
from .skeleton import importer as skeleton_importer

for _module in (
    animations,
    asset_linking,
    lights,
    particles,
    render_settings,
    worlds,
    timeline_actions,
    cutscene_import,
    kdi_audit,
    kdi_drivers,
    kdi_nodetree,
    z_fighting,
    mec_material,
    mec_parser,
    mec_importer,
    skeleton_importer,
    game_packages,
    rmi_surface,
):
    importlib.reload(_module)

FF7R_REBIRTH_OT_import_mec_umap = mec_importer.FF7R_REBIRTH_OT_import_mec_umap
FF7R_REBIRTH_FH_import_mec_umap = mec_importer.FF7R_REBIRTH_FH_import_mec_umap
FF7R_REBIRTH_OT_import_mec_game_packages = game_packages.FF7R_REBIRTH_OT_import_mec_game_packages
MESH_OT_find_opposite_faces = z_fighting.MESH_OT_find_opposite_faces

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
    rebirth_install_root: StringProperty(
        name="Rebirth Install Folder",
        description="FINAL FANTASY VII REBIRTH installation containing End/Content/Paks",
        subtype="DIR_PATH",
        default="",
    )
    rebirth_usmap_path: StringProperty(
        name="Rebirth Mapping File",
        description="UE 4.26 .usmap used by CUE4Parse to deserialize Rebirth packages",
        subtype="FILE_PATH",
        default="",
    )
    rebirth_oodle_dll: StringProperty(
        name="Oodle DLL",
        description="Compatible oo2core DLL used to decompress package content (found in a Final Fantasy VII Remake Intergrade install, e.g. Engine/Binaries/ThirdParty/Oodle/Win64/oo2core_7_win64.dll)",
        subtype="FILE_PATH",
        default="",
    )
    map_scale_factor: FloatProperty(
        name="Map Scale",
        description="Uniform coordinate scale applied to package map actors and Massive Environment geometry",
        default=0.01,
        min=0.0001,
        max=100.0,
        soft_min=0.001,
        soft_max=10.0,
    )
    offset_mec_opposite_faces: BoolProperty(
        name="Offset MEC Opposite Faces",
        description=(
            "Directly offset overlapping opposite-facing Massive Environment faces "
            "by 0.0005 at 0.01 scale during import"
        ),
        default=True,
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
        asset_box.label(text="Linked Prop Library")
        asset_box.prop(self, "asset_library_selection")
        if self.asset_library_selection == asset_linking.ASSET_LIBRARY_MANUAL:
            asset_box.prop(self, "manual_asset_library_path")

        texture_box = layout.box()
        texture_box.label(text="Massive Environment Textures")
        texture_box.prop(self, "game_texture_root")
        row = texture_box.row()
        row.label(text="Extension:")
        row.prop(self, "texture_extension", text="")
        texture_box.prop(self, "texture_match_by_filename")

        package_box = layout.box()
        package_box.label(text="Direct Rebirth Package Imports")
        package_box.prop(self, "rebirth_install_root")
        package_box.prop(self, "rebirth_usmap_path")
        package_box.prop(self, "rebirth_oodle_dll")
        package_box.prop(self, "map_scale_factor")
        package_box.prop(self, "offset_mec_opposite_faces")


class FF7R_REBIRTH_OT_import_cutscene_json(FF7R_LoggedOperator):
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
        render_settings.ensure_cycles_transparent_bounces()

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
            FF7R_REBIRTH_OT_import_mec_umap.bl_idname,
            text="Massive Environment UMAP (Loose Files)",
        )
        op.filepath = ""
        layout.operator(
            FF7R_REBIRTH_OT_import_mec_game_packages.bl_idname,
            text="UMAP (Rebirth Packages)",
        )
        layout.operator(
            game_packages.FF7R_REBIRTH_OT_import_static_mesh_game_packages.bl_idname,
            text="Static Mesh (Rebirth Packages)",
        )
        layout.operator(
            game_packages.FF7R_REBIRTH_OT_import_skeletal_mesh_game_packages.bl_idname,
            text="Skeletal Mesh (Rebirth Packages)",
        )
        layout.separator()
        layout.operator(
            kdi_drivers.KDI_OT_step2_scalar_drivers.bl_idname,
            text="KineDriver JSON",
        )
        layout.operator(
            game_packages.FF7R_REBIRTH_OT_import_kdi_game_packages.bl_idname,
            text="KineDriver JSON (Rebirth Packages)",
        )
        layout.separator()
        layout.operator(
            game_packages.FF7R_REBIRTH_OT_import_skeleton_game_packages.bl_idname,
            text="Skeleton (Rebirth Packages)",
        )


def menu_func_import(self, _context):
    self.layout.menu(TOPBAR_MT_file_import_ff7r_rebirth.bl_idname)


def _search_rmi_variants(_self, _context, edit_text):
    """Searchable RMI variant names for the operator redo panel."""
    try:
        _master, _variant, ground_truth = rmi_surface._modules()
    except Exception:
        return []
    needle = (edit_text or "").casefold()
    return [name for name in sorted(ground_truth, key=str.casefold)
            if needle in name.casefold()]


class OBJECT_OT_add_rmi_surface_material(FF7R_LoggedOperator):
    """Put a fresh RMI_Surface master or trimmed variant in the active slot."""

    bl_idname = "object.ff7r_add_rmi_surface_material"
    bl_label = "Add FF7R RMI Surface Material"
    bl_description = (
        "Create a new FF7R RMI Surface material in the active material slot; "
        "leave Variant blank for the full master"
    )
    bl_options = {'REGISTER', 'UNDO'}

    variant: StringProperty(
        name="Variant",
        description=(
            "Optional RMI_Surface variant. Search the known variant list, or "
            "leave blank to use the complete master surface"
        ),
        default="",
        search=_search_rmi_variants,
        search_options={'SUGGESTION', 'SORT'},
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and hasattr(getattr(obj, "data", None), "materials")

    def draw(self, _context):
        self.layout.prop(self, "variant")

    def execute(self, context):
        obj = context.active_object
        try:
            master, variant_helper, ground_truth = rmi_surface._modules()
            master_material = rmi_surface._ensure_master(master)
            material = master_material.copy()

            requested = self.variant.strip()
            if requested:
                variant_name = requested if requested.startswith("RMI_Surface_") else (
                    "RMI_Surface_" + requested
                )
                switches = ground_truth.get(variant_name)
                if switches is None:
                    bpy.data.materials.remove(material)
                    self.report({'ERROR'}, "Choose a variant from the search list.")
                    return {'CANCELLED'}
                material.name = variant_name
                variant_helper.apply_variant(material, master, variant_name, set(switches))
                material["ff7r_rmi_variant"] = variant_name
            else:
                material.name = "FF7R RMI Surface"
                material["ff7r_rmi_variant"] = ""

            slots = obj.data.materials
            if slots:
                slot_index = min(max(0, obj.active_material_index), len(slots) - 1)
                slots[slot_index] = material
                obj.active_material_index = slot_index
            else:
                slots.append(material)
                obj.active_material_index = 0
            self.report({'INFO'}, "Added " + material.name)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, "Could not create RMI surface: " + str(exc))
            return {'CANCELLED'}


class OBJECT_MT_ff7r_rebirth_tools(Menu):
    """The deliberately small replacement for the former FF7R Actions menu."""

    bl_idname = "OBJECT_MT_ff7r_rebirth_tools"
    bl_label = "Retrilogy tools"

    def draw(self, _context):
        layout = self.layout
        layout.operator(OBJECT_OT_add_rmi_surface_material.bl_idname)
        layout.separator()
        layout.operator(MESH_OT_find_opposite_faces.bl_idname)
        layout.separator()
        layout.operator(animations.FF7R_REBIRTH_OT_apply_animation_game_packages.bl_idname)
        layout.separator()
        layout.operator(kdi_nodetree.FF7R_KDI_OT_visualize.bl_idname)
        layout.operator(kdi_drivers.KDI_OT_remove_scalar_drivers.bl_idname)


def menu_func_object(self, _context):
    self.layout.menu(OBJECT_MT_ff7r_rebirth_tools.bl_idname)


class ARMATURE_MT_ff7r_rebirth_tools(Menu):
    """Edit-mode helpers for the active armature bone."""

    bl_idname = "ARMATURE_MT_ff7r_rebirth_tools"
    bl_label = "Retrilogy tools"

    def draw(self, _context):
        self.layout.operator(
            ARMATURE_OT_connect_parent_tail_to_bone.bl_idname,
        )


class ARMATURE_OT_connect_parent_tail_to_bone(FF7R_LoggedOperator):
    bl_idname = "armature.connect_parent_tail_to_bone"
    bl_label = "Move Parent Tail Here and Connect"
    bl_description = (
        "Move the active bone's parent's tail to this bone's head, then connect "
        "the active bone without moving its head"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature = context.object
        bone = context.active_bone
        return (
            armature is not None
            and armature.type == "ARMATURE"
            and armature.mode == "EDIT"
            and bone is not None
            and bone.parent is not None
        )

    def execute(self, context):
        bone = context.active_bone
        parent = bone.parent
        parent.tail = bone.head
        bone.use_connect = True
        self.report({"INFO"}, f"Connected '{bone.name}' to '{parent.name}' without moving its head")
        return {"FINISHED"}


def menu_func_edit_armature(self, _context):
    self.layout.menu(ARMATURE_MT_ff7r_rebirth_tools.bl_idname)


if FileHandler is not None:
    class FF7R_REBIRTH_FH_import_cutscene_json(FileHandler):
        bl_idname = "FF7R_REBIRTH_FH_import_cutscene_json"
        bl_label = "FF7R Cutscene JSON"
        bl_import_operator = FF7R_REBIRTH_OT_import_cutscene_json.bl_idname
        bl_file_extensions = ".json"

        @classmethod
        def poll_drop(cls, context):
            return context.region and context.region.type == "WINDOW"

else:
    FF7R_REBIRTH_FH_import_cutscene_json = None


classes = tuple(cls for cls in (
    FF7R_ImportPreferences,
    FF7R_REBIRTH_OT_import_cutscene_json,
    *animations.CLASSES,
    FF7R_REBIRTH_OT_import_mec_umap,
    *game_packages.CLASSES,
    *kdi_nodetree.CLASSES,
    TOPBAR_MT_file_import_ff7r_rebirth,
    FF7R_REBIRTH_FH_import_cutscene_json,
    FF7R_REBIRTH_FH_import_mec_umap,
    MESH_OT_find_opposite_faces,
    OBJECT_OT_add_rmi_surface_material,
    OBJECT_MT_ff7r_rebirth_tools,
    ARMATURE_OT_connect_parent_tail_to_bone,
    ARMATURE_MT_ff7r_rebirth_tools,
) if cls is not None)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    game_packages.register_runtime_properties()
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_object.append(menu_func_object)
    bpy.types.VIEW3D_MT_edit_armature.append(menu_func_edit_armature)
    kdi_drivers.register()


def unregister():
    kdi_drivers.unregister()
    bpy.types.VIEW3D_MT_edit_armature.remove(menu_func_edit_armature)
    bpy.types.VIEW3D_MT_object.remove(menu_func_object)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    game_packages.unregister_runtime_properties()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
