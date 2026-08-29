"""Shared Blender render-setting helpers for FF7R imports."""

from __future__ import annotations

import bpy


MIN_CYCLES_TRANSPARENT_BOUNCES = 24


def ensure_cycles_transparent_bounces(
    scene: bpy.types.Scene | None = None,
    minimum: int = MIN_CYCLES_TRANSPARENT_BOUNCES,
) -> bool:
    """Raise Cycles transparent bounces when the current scene is below minimum."""
    scene = scene or bpy.context.scene
    cycles = getattr(scene, "cycles", None)
    if cycles is None or not hasattr(cycles, "transparent_max_bounces"):
        return False

    current = cycles.transparent_max_bounces
    if current >= minimum:
        return False

    cycles.transparent_max_bounces = minimum
    return True
