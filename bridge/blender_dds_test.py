import bpy

path = r"C:\Users\Ghouls\Proton Drive\GhoulCulture\My files\TheBeastintheTable\Blender\FF7R\Rebirth scripts\ff7r_kdi\bridge\PC0000_00_BodyA_C.dds"
image = bpy.data.images.load(path, check_existing=False)
print(f"DDS_LOAD_OK name={image.name} size={image.size[0]}x{image.size[1]} packed={image.packed_file is not None}")
