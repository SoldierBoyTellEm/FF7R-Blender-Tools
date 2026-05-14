"""
FF7Rebirth Massive Environment Importer
=======================================
Blender addon for importing MassiveEnvironmentComponent0 data from
FF7 Rebirth .umap / .ubulk files exported via fmodel or similar tools.

Package layout
--------------
__init__.py          — bl_info, registration
material.py          — hash lookup, texture helpers, node-tree builders
parser.py            — binary reader, UE4 property skipper, scene builder
operator.py          — addon preferences, import operator
texture_hashes.csv   — user-editable hash → UE path mappings (one per line)
"""

bl_info = {
    "name":        "FF7Rebirth Massive Environment Importer",
    "author":      "Converted and extended",
    "version":     (1, 0, 0),
    "blender":     (4, 0, 0),
    "location":    "File > Import > FF7Rebirth MEC (.umap)",
    "description": "Import MassiveEnvironmentComponent0 from .umap/.ubulk",
    "category":    "Import-Export",
}

import os
import bpy

from .operator import UMAP_AddonPreferences, IMPORT_UMAP_OT_massive
from .material import load_hash_table
from .operator import UMAP_AddonPreferences, IMPORT_UMAP_OT_massive, UMAP_FH_import


def _menu_func(self, context):
    self.layout.operator(
        IMPORT_UMAP_OT_massive.bl_idname,
        text="FF7Rebirth Massive Environment Component (.umap)",
    )


def register():
    bpy.utils.register_class(UMAP_AddonPreferences)
    bpy.utils.register_class(IMPORT_UMAP_OT_massive)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func)
    if bpy.app.version >= (4, 1, 0):
        bpy.utils.register_class(UMAP_FH_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func)
    bpy.utils.unregister_class(IMPORT_UMAP_OT_massive)
    bpy.utils.unregister_class(UMAP_AddonPreferences)
    if bpy.app.version >= (4, 1, 0):
        bpy.utils.unregister_class(UMAP_FH_import)
