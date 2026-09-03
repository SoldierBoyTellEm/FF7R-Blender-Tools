import bpy
from pathlib import Path

path = str(Path(__file__).resolve().parent / "PC0000_00_BodyA_C.dds")
image = bpy.data.images.load(path, check_existing=False)
print(f"DDS_LOAD_OK name={image.name} size={image.size[0]}x{image.size[1]} packed={image.packed_file is not None}")
