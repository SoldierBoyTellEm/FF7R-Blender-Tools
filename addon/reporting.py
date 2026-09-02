"""Shared reporting for FF7R operators.

Blender's status-bar reports are intentionally short-lived; mirror them to the
system console so import diagnostics, warnings, and success summaries live in
one searchable log as well.
"""

from __future__ import annotations

import bpy


class FF7R_LoggedOperator(bpy.types.Operator):
    """Operator base which sends every Blender report to stdout too."""

    def report(self, type, message):
        levels = "/".join(sorted(str(level) for level in type))
        print(f"[FF7R {levels}] {message}")
        return super().report(type, message)


def report(operator: bpy.types.Operator, levels, message: str) -> None:
    """Emit a Blender report and mirror it to the system console.

    ``Operator.report`` is implemented by Blender's RNA layer and bypasses a
    Python subclass override, so every operator calls this helper explicitly.
    """
    level_text = "/".join(sorted(str(level) for level in levels))
    print(f"[FF7R {level_text}] {message}")
    operator.report(levels, message)
