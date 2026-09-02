"""
FF7 Rebirth  RMI_Surface  ->  per-variant Blender material

Companion to ff7r_rmi_surface.py (the "master" ubershader builder).  This
script takes ONE RMI_Surface_<variant> name, looks up which of the 126 known
switches that variant's compiled MaterialInstance actually enables (exact,
current-build ground truth -- see ..\\RMI_SURFACE_VARIANTS.md), and trims a
material down to that variant:

  * every switch NOT in the variant's set is turned off, its driver is
    removed, and its custom property is DELETED (PLAN: "remove the custom
    properties outside of that code path")
  * every node that exists only to serve a switch outside the variant's set
    is MUTED (PLAN: "mute the nodes not in that code path") -- as a visual
    cue for a manual cull pass, not a guarantee every muted node is
    provably dead; this does not need to be perfect out of the box
  * "Shading Model" is set to match the variant's model-defining flag
    (Eye_ / Hair_ / Skin_ / Subsurface_ / Fabric_ / Unlit_ / else Standard)

By default this trims the ACTIVE OBJECT'S ACTIVE MATERIAL SLOT IN PLACE --
e.g. your own working copy of the full shader, however you've named it
(a real case: a duplicate renamed to "PC0000_00_TopsA"). Set TARGET_MATERIAL
below to a material name instead if you'd rather not rely on the current
selection. Set MAKE_COPY_FROM_MASTER = True if you instead want a brand-new
"RMI_Surface_<variant>" copy freshly duplicated off FF7R_RMI_Surface_Master
-- see the warning on that flag below before turning it on.

IMPORTANT -- why importing ff7r_rmi_surface.py used to break OTHER materials
------------------------------------------------------------------------------
"A node group SHARES its node tree between instances" is called out in
ff7r_rmi_surface.py's own module docstring: FF7R Eye, FF7R Hair Strand,
FF7R Coverage, UE Unpack Normal (RG), and every other util_*/grp_* group are
ONE node-group datablock referenced by a ShaderNodeGroup instance in every
material that uses that feature -- the master, and any hand-made variant
copy of it (PC0000_00_TopsA included).

ff7r_rmi_surface.py used to call build() unconditionally the moment it was
imported. build() calls every util_*()/grp_*() function, and each of those
calls _rebuild_group() on its shared group -- which does
`g.interface.remove(it)` for every existing input/output socket and then
recreates them from scratch. That doesn't just touch
FF7R_RMI_Surface_Master: it destroys and replaces the socket identities on
groups that EVERY OTHER material referencing them is still linked to, which
breaks those links wherever that group is used -- including inside a material
this script never even looked up by name. That is what was happening to
PC0000_00_TopsA.001.

The fix is upstream: ff7r_rmi_surface.py now guards its build()+report call
behind `if __name__ == "__main__":`, so importing it (as this script does,
via importlib, to reuse its tables) only defines functions/constants and
does NOT rebuild anything. This script only calls master_mod.build() at all
when MAKE_COPY_FROM_MASTER is True, and even then that only touches
FF7R_RMI_Surface_Master's own node tree plus the shared groups' interfaces --
which is unavoidable if you want a guaranteed-fresh master to copy from, but
is why that mode is opt-in rather than the default.

node.mute itself was never the problem -- an earlier revision of this file
mistakenly swapped it for a cosmetic recolor thinking mute was deleting
links; it wasn't, the group-interface rebuild above was. Muting is back:
Frame/Reroute nodes silently can't be muted and are skipped; everything else
gets marked with the same "M" the Shader Editor already uses for "disabled",
which is more legible for a manual culling pass than a recolor.

What "mute" actually does to a Mix node, and the one rule that follows
------------------------------------------------------------------------------
Blender's mute is a BYPASS, not an off switch: it replaces the node with an
internal link from ONE input to the matching output.  Which input it picks is
not "the first one" -- it is the first *linked* input of a compatible type.
Measured on 5.0.1 and 5.2.0 LTS, for every ShaderNodeMix data_type
(FLOAT / VECTOR / RGBA):

    A linked                -> A passes through          <- what we want
    A unlinked, B linked    -> **B** passes through      <- the trap
    neither linked          -> A's default value

That single table explains both halves of this file's muting policy:

  * USE_<role> holds the neutral constant in A (unlinked) and the texture in
    B (linked).  Muting one does not disable the texture, it FORCES the
    texture on -- exactly backwards.  So texture bypasses are never muted;
    their SW_ Value node is zeroed instead (see _retire_property).

  * A LADDER RUNG is the opposite shape: A carries the value accumulated so
    far down the chain and B carries one feature's contribution, both linked.
    Muting a rung is precisely "skip this rung", which is correct exactly
    when B can never be selected for this variant.  That is what
    _mute_ladder_mix() does, and why it hard-refuses any Mix whose A is not
    linked rather than trusting the caller.

Three ladders dominate the graph: _menu() builds SM_BaseColor / SM_Normal /
SM_Roughness as a 7-rung compare+Mix chain each (Menu Switch is unusable on
every Blender tested -- see _detect_menu_switch() in the master, this is not
a legacy path), and a variant resolves to exactly ONE shading model, so six
of the seven rungs in each are unreachable.  BloodOverride and EmissionSum
are the same shape with a hand-mapped gate.  Note that the "selector == i"
Math comparators are deliberately NOT muted: a muted Math node passes input 0
straight through, which would put the raw selector value on the next rung's
Factor and select B -- the exact opposite of the intent.

Node <-> switch mapping used to decide what to mute
-----------------------------------------------------
Everything here is DERIVED from ff7r_rmi_surface.py's own tables (imported
directly, not copy-pasted, so the two files cannot drift):

  * SWITCH_CONTROLS      -> the "SW_<name>" Value node for each checkbox
  * SWITCH_FOR_ROLE       -> the "TEX_<role>" / "USE_<role>" node pair for
                             each texture slot
  * ROLE_UV               -> which of the 4 UV nodes a role actually reads
  * FRAME_EyePath / FRAME_HairPath -> every node parented to those frames is
                             switched as a block on Eye_ / Hair_, since those
                             are wholesale alternate shading models selected
                             once via the SM_* menus, not per-node features

A handful of named feature-group instances (Coverage, SegmentLayers,
LayerBlend, TransitionNormal, Film, DetailLayer, Blood, Emissive, and the
small math nodes that only feed them) are not switch-derived automatically --
they are hand-mapped in FEATURE_NODE_REQUIRES below by reading exactly which
switch drives each one's "Enable" input in build(). Likewise ENUM_REQUIRES /
FLOAT_REQUIRES hand-map the non-boolean custom properties (Eye Cornea Slope,
Hair Min VdotT, Subsurface Scale, Film Thickness, ...) to the switch that
makes them meaningful. These hand-maps are the one place this script is
making a documentation judgement call rather than reading a fact straight out
of the master script; each is commented with which line in build() it is
based on.

Run:   blender --background --factory-startup --python ff7r_rmi_surface_variant.py
   or  paste into Blender's text editor and hit Run (set MASTER_SCRIPT_PATH
       below first -- an unsaved text block has no reliable __file__, even
       after drag-and-drop -- it resolves relative to the .blend, not to
       wherever the file actually lives on disk).

Edit VARIANT (and TARGET_MATERIAL, if you're not relying on the active
selection) below and re-run for each variant you want trimmed.
"""

import bpy
import difflib
import importlib.util
import json
import os
import re

# ---------------------------------------------------------------------------
# Edit these for your run.
# ---------------------------------------------------------------------------
VARIANT = "RMI_Surface_Standard_Coverage_Wide_Detail"

# None = trim the active object's active material slot, in place. Set this to
# a material name (e.g. "PC0000_00_TopsA") to target it explicitly instead of
# relying on the current selection.
TARGET_MATERIAL = None

# False (default): trim TARGET_MATERIAL / the active material in place.
# True: ignore TARGET_MATERIAL and instead (re)build FF7R_RMI_Surface_Master
# fresh and duplicate IT into a new "RMI_Surface_<variant>" material. This
# calls master_mod.build(), which touches every shared "FF7R "/"UE " node
# group's interface -- safe for materials that only ever read from those
# groups' CURRENT interface, but see the module docstring: if you have other
# hand-edited materials in this .blend using those same shared groups, their
# links can be affected too. Leave this False unless you specifically want a
# clean copy of the master rather than to trim what you already have open.
MAKE_COPY_FROM_MASTER = False

# Blender's Text Editor does not give a dropped-in/loaded text block a
# reliable __file__ pointing at the real file on disk -- even after
# drag-and-drop it resolves relative to the .blend, not the script's actual
# folder. So this is hardcoded rather than auto-detected.
# Blank in the add-on's bundled copy: ff7r_rmi_surface.py sits next to this
# file in rmi_sources/, and the add-on loads this module from a real file path
# via importlib, so __file__ is trustworthy here and _script_dir() resolves the
# bundled master. The research checkout's copy keeps the hardcoded path for the
# drag-into-Text-Editor workflow described above.
MASTER_SCRIPT_PATH = ""


def _script_dir():
    # MASTER_SCRIPT_PATH wins even when __file__ exists: Blender's Text
    # Editor resolves __file__ relative to the .blend for a text block, not
    # to wherever the file actually lives on disk -- true even after
    # drag-and-drop, so __file__ cannot be trusted here at all.
    if MASTER_SCRIPT_PATH:
        return os.path.dirname(os.path.abspath(MASTER_SCRIPT_PATH))
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass
    raise RuntimeError(
        "Cannot locate this script's own directory (no __file__ -- likely "
        "running from an unsaved Blender Text Editor buffer). Set "
        "MASTER_SCRIPT_PATH at the top of ff7r_rmi_surface_variant.py to the "
        "full path of ff7r_rmi_surface.py and re-run.")


def _load_master_module(script_dir):
    """Import ff7r_rmi_surface.py for its tables only. As of the
    `if __name__ == "__main__":` guard at the bottom of that file, this does
    NOT call build() and therefore does not touch FF7R_RMI_Surface_Master or
    any shared node group -- see the module docstring above."""
    path = MASTER_SCRIPT_PATH or os.path.join(script_dir, "ff7r_rmi_surface.py")
    if not os.path.isfile(path):
        raise RuntimeError("ff7r_rmi_surface.py not found at %r" % path)
    spec = importlib.util.spec_from_file_location("ff7r_rmi_surface", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_ground_truth(script_dir):
    path = os.path.join(script_dir, "..", "scripts", "renderer_ground_truth.json")
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise RuntimeError(
            "Ground-truth switch table not found at %r. This is the exact, "
            "current-build StaticSwitchParameters dump for all 104 "
            "RMI_Surface_* MaterialInstanceConstants -- see "
            "RMI_SURFACE_VARIANTS.md's opening section for where it comes "
            "from." % path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_variant(name, table):
    full = name if name.startswith("RMI_Surface_") else "RMI_Surface_" + name
    if full in table:
        return full, set(table[full])
    close = difflib.get_close_matches(full, table.keys(), n=5)
    raise KeyError("Unknown RMI_Surface variant %r.%s"
                   % (full, ("\n  Closest matches:\n    " +
                             "\n    ".join(close)) if close else ""))


def shading_model_for(switches):
    """Mirrors ENUM_PROPS's "Shading Model" description in ff7r_rmi_surface.py:
    0 Standard | 1 Skin | 2 Subsurface Profile | 3 Hair | 4 Cloth | 5 Eye |
    6 Unlit. Priority order matches how the 104 variants are actually
    partitioned -- Eye_/Hair_/Skin_/Subsurface_/Fabric_/Unlit_ never combine
    with each other in the ground-truth table, so order only matters for the
    handful of variants (e.g. Ghost/Phantom, which also carry Standard_) that
    layer an unlit-family flag on top."""
    if "Eye_" in switches:
        return 5
    if "Hair_" in switches:
        return 3
    if "Skin_" in switches:
        return 1
    if "Subsurface_" in switches:
        return 2
    if "Fabric_" in switches:
        return 4
    if "Unlit_" in switches or "Ghost_" in switches or "Hologram_" in switches \
            or "Phantom_" in switches:
        return 6
    return 0


# ---------------------------------------------------------------------------
# Hand-mapped requirements -- see the module docstring for how these were
# derived from build().  `None` = core / always required, never muted, never
# removed.  A tuple = kept when ANY of those raw switch flags is in the
# variant's set.
# ---------------------------------------------------------------------------

DETAIL_FLAGS = ("Detail_", "DetailColor_", "DetailMetallic_", "DetailNormal_",
                "DetailRoughness_", "DetailOcclusion_", "Isotropy_")
# build(): DetailLayer's "Enable" is the OR-chain over exactly these six
# detail-role flags; Isotropy_ additionally forces RNM through the same
# detail_mode node, so it belongs in the same "detail machinery matters" set.

ENUM_REQUIRES = {
    "Shading Model": None,                                  # core selector
    "Coverage Mode": ("Coverage_",),                         # feeds cov "Mode"
    "Detail Blend": DETAIL_FLAGS,                            # feeds detail_mode
    "Vertex Expression Source": ("PositiveTransitionNormal_", "VertexExpressionBone_"),
    # build(): only consumer is trans "Source", and trans's own Enable is
    # PositiveTransitionNormal_ (cooccurs with VertexExpressionBone_).
}

FLOAT_REQUIRES = {
    "Object Fade": None,          # cov "Object Fade" -- always wired
    "Opacity Clip": None,         # cov "Clip" -- always wired
    "GBufferA.a": None,           # f0 "GBufferA.a" -- always wired
    "Eye Cornea Slope": ("Eye_",), "Eye Eta": ("Eye_",),
    "Eye Depth Const": ("Eye_",), "Eye Depth Falloff": ("Eye_",),
    "Eye Depth Scale": ("Eye_",), "Eye Mask Inner": ("Eye_",),
    "Eye Mask Outer": ("Eye_",), "Eye Limbal Offset": ("Eye_",),
    "Eye Limbal Softness": ("Eye_",), "Eye Limbal Aperture": ("Eye_",),
    "Eye Limbal Amount": ("Eye_",), "Pupil Dilation": ("Eye_",),
    "Hair Min VdotT": ("Hair_",), "Hair Dither Sign": ("Hair_",),
    "Hair Roughness Curvature": ("Hair_",), "Hair Anisotropic": ("Hair_",),
    "Hair Flow Influence": ("Hair_",), "Hair Specular F0": ("Hair_",),
    "Detail Normal Strength": DETAIL_FLAGS,  # feeds DetailLayer "Normal Strength"
    "Subsurface Scale": ("Subsurface_",),   # feeds smodel's SSS radius calc
    "Thickness": ("Subsurface_",),          # same calc, paired with the above
    "Sheen Roughness": ("Fabric_",),        # only meaningful when Cloth's
    "Sheen": ("Fabric_",),                  # Sheen Weight is nonzero (SM 4)
    "Isotropy": ("Isotropy_",),
    "Wetness": ("Wetness_", "WetnessTrickle_"),
    "DetailWetness": ("Wetness_", "WetnessTrickle_"),
    "Visibility": None,                     # global object-level fade concept
    "FilmStructure": ("Film_",),            # feeds film "Structure"
    "FilmThickness": ("Film_",),            # feeds film "Thickness"
    "ExtraColor": ("ExtraColor_",),
    "OxygenSaturation": ("Blood_",),        # feeds blood "Oxygen Saturation"
    "DetailMetallic": ("DetailColor_", "DetailMetallic_", "LayeredColor_"),
}

# node name (as created in build()) -> switches that keep it unmuted.
FEATURE_NODE_REQUIRES = {
    "Coverage": ("Coverage_",),
    "SegmentLayers": ("Segmented_",),
    "LayerBlend": ("Layered_",),
    "TransitionNormal": ("PositiveTransitionNormal_", "VertexExpressionBone_"),
    "N_Transition": ("PositiveTransitionNormal_", "VertexExpressionBone_"),
    "Film": ("Film_",),
    "DetailLayer": DETAIL_FLAGS,
    "N_Detail": DETAIL_FLAGS,
    "RNMWhenIsotropy": DETAIL_FLAGS,
    "EffectiveDetailBlend": DETAIL_FLAGS,
    "Blood": ("Blood_", "OxygenSaturation_"),
    "Emissive": ("Emissive_", "ExtraEmissive_", "ExternalEmissive_"),
}

FRAME_REQUIRES = {
    "FRAME_EyePath": ("Eye_",),
    "FRAME_HairPath": ("Hair_",),
}


def _is_active(flags, switches):
    return flags is None or any(f in switches for f in flags)


def _mute(node, muted):
    """Mute only source nodes that are provably unused.

    Blender's mute implementation is a bypass, not an "off" switch.  In
    particular, muting a Value/Math/Mix/group node that feeds a live graph can
    substitute a socket default and turn a downstream Mix factor on.  Variant
    switches are already made safe by their zero-valued Value nodes, so retain
    those processing nodes and only hide unused texture/UV sources.
    """
    if node is None:
        return False
    if node.bl_idname not in {"ShaderNodeTexImage", "ShaderNodeUVMap"}:
        return False
    try:
        node.mute = muted
        return bool(muted)
    except Exception:
        return False


# Mix nodes wired as a ladder rung in the MATERIAL tree: A is the value
# flowing down the chain, B is one feature's contribution, and the Factor
# chooses between them.  `None` = never muted.  A tuple = the rung stays live
# when ANY of those raw switch flags is in the variant's set.
LADDER_MIX_REQUIRES = {
    # build(): _link(colsel -> "A"), _link(blood "Base Color" -> "B"),
    #          _link(P["Blood_"] -> "Factor").  Blood_ and OxygenSaturation_
    #          are one co-occurrence class in the ground truth, so either flag
    #          keeps it.
    "BloodOverride": ("Blood_", "OxygenSaturation_"),
    # build(): _link(emis "Emission" -> "A"), _link(eye "Emission" -> "B"),
    #          Factor pinned to 1.0 with blend_type ADD.  B is the eye group's
    #          unshadowed iris glow, so it is only reachable on the eye path.
    #          IrisEmissive_ never occurs without Eye_ in the 104 ground-truth
    #          variants; it is listed anyway so a future variant that split
    #          them would keep the rung rather than silently lose the glow.
    "EmissionSum": ("Eye_", "IrisEmissive_"),
}

# ShaderNodeMix carries one A/B pair per data type and hides the rest, so
# node.inputs["A"] is ambiguous -- resolve by socket identifier instead.
_MIX_AB = {
    "FLOAT": ("A_Float", "B_Float"),
    "VECTOR": ("A_Vector", "B_Vector"),
    "RGBA": ("A_Color", "B_Color"),
    "ROTATION": ("A_Rotation", "B_Rotation"),
}


def _mix_ab(node):
    """The A and B sockets that are live for a Mix node's current data_type."""
    ids = _MIX_AB.get(getattr(node, "data_type", ""))
    if ids is None:
        return None, None
    by_id = {s.identifier: s for s in node.inputs}
    return by_id.get(ids[0]), by_id.get(ids[1])


def _mute_ladder_mix(node, why):
    """Mute one ladder rung -- but only where muting means "pass A through".

    Refuses anything that is not a Mix with BOTH A and B linked.  With A
    unlinked, Blender's bypass hands the output to B instead (see the module
    docstring's table), which would turn the dead branch ON; with B unlinked
    there is no branch to skip in the first place."""
    if node is None or node.bl_idname != "ShaderNodeMix":
        return False
    a, b = _mix_ab(node)
    if a is None or b is None or not a.is_linked or not b.is_linked:
        return False
    try:
        node.mute = True
    except Exception:
        return False
    node.label = "%s  [bypassed: %s]" % (node.label or node.name, why)
    return True


def _menu_ladders(tree):
    """Group the nodes of each _menu() compare+Mix ladder by its menu name.

    _menu() emits "<menu>_selector", "<menu>_val<i>", "<menu>_eq<i>" and
    "<menu>_mix<i>" -- derived from the master's own naming rather than a
    hardcoded list of the three SM_* menus, so a menu added later is picked
    up without editing this file."""
    ladders = {}
    for node in tree.nodes:
        match = re.match(r"^(.*)_(val|mix|eq)(\d+)$", node.name)
        if match is None:
            continue
        ladders.setdefault(match.group(1), {}).setdefault(
            match.group(2), {})[int(match.group(3))] = node
    return {name: parts for name, parts in ladders.items()
            if "mix" in parts and "val" in parts}


def _selector_property(tree, menu_name):
    """Which material custom property drives this ladder's selector Value node.

    Read off the driver the master put there rather than assumed, so a menu
    keyed on something other than "Shading Model" is not mis-trimmed."""
    node = tree.nodes.get(menu_name + "_selector")
    anim = getattr(tree, "animation_data", None)
    if node is None or anim is None:
        return None
    path = 'nodes["%s"].outputs[0].default_value' % node.name
    for curve in anim.drivers:
        if curve.data_path != path:
            continue
        for var in curve.driver.variables:
            match = re.match(r'^\["(.+)"\]$', var.targets[0].data_path or "")
            if match:
                return match.group(1)
    return None


def _retire_property(mat, node, prop_name, stats):
    """Zero a retired control without muting the Value node that feeds it.

    A muted Value node can bypass its zero output and make a linked Mix factor
    evaluate as one.  Keeping the node live at zero is both the correct shader
    result and a reusable rule for every unavailable switch/enum/float.
    """
    if node is not None:
        try:
            node.outputs[0].driver_remove("default_value")
        except Exception:
            pass
        try:
            node.outputs[0].default_value = 0.0
        except Exception:
            pass
        if getattr(node, "bl_idname", "") == "ShaderNodeValue":
            node.label = (node.label or prop_name) + "  [disabled: 0]"
    if prop_name in mat:
        del mat[prop_name]
        stats["removed"].append(prop_name)


def get_target_material(target_name):
    if target_name:
        mat = bpy.data.materials.get(target_name)
        if mat is None:
            raise RuntimeError("TARGET_MATERIAL %r not found." % target_name)
        return mat
    obj = bpy.context.object
    if obj is None or obj.active_material is None:
        raise RuntimeError(
            "No TARGET_MATERIAL set and no active object/material slot to "
            "trim -- select the object (and material slot, in the "
            "Properties panel) you want to trim first, or set "
            "TARGET_MATERIAL at the top of this script.")
    return obj.active_material


def get_or_copy_master(master_mat_name, variant_full_name):
    master = bpy.data.materials.get(master_mat_name)
    if master is None:
        raise RuntimeError(
            "Master material %r not found even after (re)running "
            "ff7r_rmi_surface.py -- something upstream failed." % master_mat_name)
    existing = bpy.data.materials.get(variant_full_name)
    if existing is not None:
        bpy.data.materials.remove(existing)   # always start from a clean copy
    variant_mat = master.copy()
    variant_mat.name = variant_full_name
    return variant_mat


def apply_variant(mat, master_mod, variant_full_name, switches):
    t = mat.node_tree
    stats = {"removed": [], "muted": 0, "kept_switches": 0, "ladder": 0}

    # 1. switch checkboxes ---------------------------------------------------
    for control, members, _default in master_mod.SWITCH_CONTROLS:
        node = t.nodes.get("SW_" + members[0])
        active = any(m in switches for m in members)
        if active:
            if control in mat:
                mat[control] = True
            _mute(node, False)
            stats["kept_switches"] += 1
        else:
            _retire_property(mat, node, control, stats)

    # 2. enum selectors -------------------------------------------------
    for name, _d, _mn, _mx, _desc in master_mod.ENUM_PROPS:
        req = ENUM_REQUIRES.get(name)
        node = t.nodes.get("E_" + name.replace(" ", "_"))
        if not _is_active(req, switches):
            _retire_property(mat, node, name, stats)
        elif name == "Shading Model" and name in mat:
            mat[name] = shading_model_for(switches)

    # Blender 5.2's Constant Menu cannot have a driver, but Python can set its
    # value.  It is the single source feeding every top-level routing switch
    # and the FF7R Shading Model group, so one dropdown remains in sync.
    model = shading_model_for(switches)
    model_name = master_mod.SHADING_MODELS[model]
    model_constant = t.nodes.get("E_Shading_Model")
    if model_constant is not None and model_constant.bl_idname == "FunctionNodeInputMenu":
        try:
            model_constant.value = model_name
        except (AttributeError, TypeError, ValueError):
            pass
    else:
        # Compatibility with materials generated by the brief intermediate
        # implementation that stored an independent default on each menu.
        for menu_name in ("SM_BaseColor", "SM_Normal", "SM_Roughness"):
            menu = t.nodes.get(menu_name)
            if menu is None or menu.bl_idname != master_mod.MENU_SWITCH_IDNAME:
                continue
            try:
                menu.inputs[0].default_value = model_name
            except (AttributeError, TypeError, ValueError):
                pass

    # 3. float parameters -------------------------------------------------
    for name, _d, _mn, _mx, _desc in master_mod.FLOAT_PROPS:
        req = FLOAT_REQUIRES.get(name)
        node = t.nodes.get("P_" + name.replace(" ", "_"))
        if not _is_active(req, switches):
            _retire_property(mat, node, name, stats)

    # 4. texture roles: TEX_<role> / USE_<role> pairs ------------------------
    for role, sw in master_mod.SWITCH_FOR_ROLE.items():
        active = sw in switches
        # The texture image may be hidden when unused, but USE_<role> is the
        # live neutral/texture Mix. Muting it would bypass its neutral A input.
        for prefix in ("TEX_",):
            node = t.nodes.get(prefix + role)
            if node is not None:
                if _mute(node, not active):
                    stats["muted"] += 1

    # 5. named feature-group instances + their helper math nodes -------------
    for name, req in FEATURE_NODE_REQUIRES.items():
        node = t.nodes.get(name)
        if node is not None:
            active = _is_active(req, switches)
            if _mute(node, not active):
                stats["muted"] += 1
    for node in list(t.nodes):
        if node.name.startswith("DetailEnable"):
            active = _is_active(DETAIL_FLAGS, switches)
            if _mute(node, not active):
                stats["muted"] += 1

    # 6. whole alternate-shading-model frames (Eye / Hair) -------------------
    for node in list(t.nodes):
        parent = node.parent
        if parent is not None and parent.name in FRAME_REQUIRES:
            active = _is_active(FRAME_REQUIRES[parent.name], switches)
            if _mute(node, not active):
                stats["muted"] += 1

    # 7. UV coordinate sets: keep only the ones this variant actually reads --
    used_uv = set()
    for role, sw in master_mod.SWITCH_FOR_ROLE.items():
        if sw in switches:
            used_uv.add(master_mod.ROLE_UV.get(role, 0))
    for i in range(4):
        node = t.nodes.get("UV%d" % i)
        if node is not None:
            active = ("Coordinate%d_" % i in switches) or (i in used_uv)
            if _mute(node, not active):
                stats["muted"] += 1

    # 8. ladder rungs whose B input is unreachable for this variant ---------
    #    Two shapes, one rule (see _mute_ladder_mix and the module docstring):
    #    a feature-override Mix whose gate switch this variant does not carry,
    #    and the per-shading-model selector ladders, where a variant resolves
    #    to exactly ONE model so six of every seven rungs are dead.
    for name, req in LADDER_MIX_REQUIRES.items():
        if _is_active(req, switches):
            continue
        why = "no " + "/".join(req)
        if _mute_ladder_mix(t.nodes.get(name), why):
            stats["ladder"] += 1

    for menu, rungs in _menu_ladders(t).items():
        prop = _selector_property(t, menu)
        if prop == "Shading Model":
            reachable = model
        elif prop is None or prop not in mat:
            # The selector's property was retired in step 2, so its Value node
            # now reads a hard 0 and only option 0 survives.
            reachable = 0
        else:
            # Still a live, artist-changeable selector -- every rung is
            # reachable, so none of them is safe to bypass.
            continue
        for index, node in sorted(rungs.get("mix", {}).items()):
            if index == reachable:
                continue
            if _mute_ladder_mix(node, "selector is fixed at %d" % reachable):
                stats["ladder"] += 1
        # "<menu>_eq<i>" is NEVER muted: a muted Math node passes input 0
        # through, putting the raw selector on the next rung's Factor.
        # "<menu>_val<i>" is left alone too -- its B is unlinked, so it is a
        # value holder rather than a rung, and _mute_ladder_mix refuses it.

    mat["_RMI_Variant"] = variant_full_name
    mat["_RMI_Variant_Switches"] = sorted(switches)
    return stats


def _out_of_scope_hit(item, switches):
    """OUT_OF_SCOPE entries are either an exact flag ("Deform_"), a family
    wildcard ("Gradient*_" meaning "any switch starting with Gradient"), or a
    plain descriptive string ("SM1 default") that isn't a flag at all."""
    if item.endswith("*_"):
        prefix = item[:-2]
        return sorted(s for s in switches if s.startswith(prefix))
    if item.endswith("_") and item in switches:
        return [item]
    return []


def report(mat, variant_full_name, switches, stats, master_mod):
    print("=" * 72)
    print("Trimmed %r for variant %r" % (mat.name, variant_full_name))
    print("  %d switches on, %d properties removed, %d nodes muted, "
          "%d ladder rung(s) bypassed"
          % (stats["kept_switches"], len(stats["removed"]), stats["muted"],
             stats["ladder"]))
    print("  Shading Model -> %d" % shading_model_for(switches))
    inert = {}
    for why, items in master_mod.OUT_OF_SCOPE.items():
        hit = []
        for item in items:
            hit.extend(_out_of_scope_hit(item, switches))
        if hit:
            inert[why] = sorted(set(hit))
    if inert:
        print("  This variant's own switches include flags not wired in this "
              "Blender port (harmless -- they were already inert on the "
              "master material too):")
        for why, items in inert.items():
            print("     %s: %s" % (why, ", ".join(items)))


def build_variant(variant_name):
    script_dir = _script_dir()
    master_mod = _load_master_module(script_dir)
    table = _load_ground_truth(script_dir)
    full_name, switches = resolve_variant(variant_name, table)

    if MAKE_COPY_FROM_MASTER:
        master_mod.build()
        mat = get_or_copy_master(master_mod.MAT_NAME, full_name)
    else:
        mat = get_target_material(TARGET_MATERIAL)

    stats = apply_variant(mat, master_mod, full_name, switches)
    report(mat, full_name, switches, stats, master_mod)
    return mat


if __name__ == "__main__":
    build_variant(VARIANT)
