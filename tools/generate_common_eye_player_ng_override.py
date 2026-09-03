"""Bake the bundled high-fidelity Common_Eye_Player_NG override.

Run with Blender in background mode. Blender's DDS loader reconstructs the BC5
blue channel before this script applies two 3x3 binomial passes (the equivalent
of a 5x5 Gaussian) and writes a lossless half-float EXR.
"""

from pathlib import Path

import bpy
import numpy as np


SOURCE = Path(
    r"O:\Blender\Rebirth DDS\Character\Common\Eye\Texture\Common_Eye_Player_NG.dds"
)
OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "ff7r_rebirth_tools"
    / "assets"
    / "Common_Eye_Player_NG_filtered.png"
)


def binomial_pass(pixels: np.ndarray) -> np.ndarray:
    padded = np.pad(pixels, ((1, 1), (1, 1), (0, 0)), mode="edge")
    return (
        padded[:-2, :-2] + 2.0 * padded[:-2, 1:-1] + padded[:-2, 2:]
        + 2.0 * padded[1:-1, :-2] + 4.0 * padded[1:-1, 1:-1]
        + 2.0 * padded[1:-1, 2:]
        + padded[2:, :-2] + 2.0 * padded[2:, 1:-1] + padded[2:, 2:]
    ) * (1.0 / 16.0)


source = bpy.data.images.load(str(SOURCE), check_existing=False)
source.colorspace_settings.name = "Non-Color"
width, height = (int(value) for value in source.size)
raw = np.empty(width * height * 4, dtype=np.float32)
source.pixels.foreach_get(raw)
filtered = binomial_pass(binomial_pass(raw.reshape(height, width, 4)))

result = bpy.data.images.new(
    "Common_Eye_Player_NG_filtered",
    width=width,
    height=height,
    alpha=True,
    float_buffer=True,
)
result.colorspace_settings.name = "Non-Color"
result.pixels.foreach_set(filtered.reshape(-1))
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
result.filepath_raw = str(OUTPUT)
result.file_format = "PNG"

scene = bpy.context.scene
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.render.image_settings.color_depth = "16"
result.save()
print(f"Wrote {OUTPUT} ({width}x{height}, lossless 16-bit PNG)")
