"""Shared reporting for FF7R operators.

Blender's status-bar reports are intentionally short-lived; mirror them to the
system console so import diagnostics, warnings, and success summaries live in
one searchable log as well.
"""

from __future__ import annotations

import json

import bpy


def _addon_package_name() -> str:
    return (__package__ or "").split(".", 1)[0]


def _last_settings_preferences(context):
    addon = context.preferences.addons.get(_addon_package_name())
    return addon.preferences if addon else None


class FF7R_LoggedOperator(bpy.types.Operator):
    """Operator base which sends every Blender report to stdout too, and lets
    an importer remember its dialog settings across Blender sessions.
    """

    # Property names an importer subclass wants remembered as "last used" --
    # deliberately excludes transient per-asset state like a typed search
    # path, search results, or a file browser's filepath. Persisted as JSON
    # on the add-on preferences, keyed by bl_idname, so unrelated operators
    # never collide.
    _persisted_props: tuple[str, ...] = ()

    def _load_last_import_settings(self, context) -> None:
        """Apply this operator's remembered settings, and mark this run as
        interactive so `_save_last_import_settings` is allowed to update them.

        Call once from `invoke()`, never from `execute()` alone -- an operator
        invoked headlessly by another operator's `execute()` (e.g. the
        SkeletalMesh importer driving the Skeleton importer with its own
        caller-supplied/context-derived arguments) must not feed those
        arguments back into what the user chose the last time they used the
        dialog directly.
        """
        self._interactive_invoke = True
        prefs = _last_settings_preferences(context)
        if prefs is None or not self._persisted_props:
            return
        try:
            all_settings = json.loads(prefs.last_import_settings_json or "{}")
        except (ValueError, TypeError):
            return
        stored = all_settings.get(self.bl_idname)
        if not isinstance(stored, dict):
            return
        for prop_name in self._persisted_props:
            if prop_name not in stored:
                continue
            try:
                setattr(self, prop_name, stored[prop_name])
            except (TypeError, ValueError):
                pass  # a removed enum value, a type change across versions, etc.

    def _save_last_import_settings(self, context) -> None:
        """Remember this run's settings, if it was reached via `invoke()`."""
        if not getattr(self, "_interactive_invoke", False):
            return
        prefs = _last_settings_preferences(context)
        if prefs is None or not self._persisted_props:
            return
        try:
            all_settings = json.loads(prefs.last_import_settings_json or "{}")
        except (ValueError, TypeError):
            all_settings = {}
        all_settings[self.bl_idname] = {
            prop_name: getattr(self, prop_name) for prop_name in self._persisted_props
        }
        prefs.last_import_settings_json = json.dumps(all_settings)

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
