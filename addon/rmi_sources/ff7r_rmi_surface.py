"""
FF7 Rebirth  RMI_Surface  ->  Blender master node tree

Builds ONE material containing every implemented switch path, organised into
named node groups and frames.  Switches and float parameters are exposed as
CUSTOM PROPERTIES ON THE MATERIAL and driven into the tree, so the whole thing
is controlled from the material's Custom Properties panel rather than by hunting
for Value nodes in the graph.

Spec:  ..\\BLENDER_PORT_PLAN.md   (PLAN x.y refs below are to it)
Notes: ..\\SHADER_FINDINGS.md and ..\\DEFERRED_LIGHTING_HANDOFF.md

Run:   blender --background --factory-startup --python ff7r_rmi_surface.py
   or  paste into Blender's text editor and hit Run.

Provenance
----------
The Eye and Hair paths are ports of the standalone shaders in
..\\hair_and_eye\\ (ff7r_eye_material_v2.py, ff7r_hair_material.py), which were
tuned by hand against captures.  They are kept as SELF-CONTAINED node
groups so that further tweaks to those shading paths stay local to their own
group and do not require restructuring the ubershader:

    FF7R Eye Disc / FF7R Cornea / FF7R Limbal Occlusion
    UE Frame To World (explicit TBN) / UE Limbal Occlusion (directional)
    FF7R Hair Strand / UE Unpack Normal (RG) / UE Tangent To World (mikktspace)

The current lighting-side cross-reference is the older-build RenderDoc dump
`C:\\temp\\hair_and_eye\\rd_graphics_shaders\\
07737_ps70172_Shader_bb252f56_DeferredLightPixelMain__DXBC_DXIL.txt`. Its hair
branch (`_label53`, decompiler lines 2121-2527) proves that GBufferD is a
full-RGB flow direction consumed through dot(flow, L), and that the three hair
lobes are strand-aligned anisotropic GGX rather than Kajiya-Kay. Blender 5.2
shader trees cannot access the light vector L, so this file preserves the
decoded base-pass geometry and uses Principled as an explicit approximation;
the exact downstream work and the shaders already in hand are inventoried in
DEFERRED_LIGHTING_HANDOFF.md.

Three structural facts this script is built around
--------------------------------------------------
1. A node group SHARES its node tree between instances, so an Image Texture
   inside a group would be the same image for every user of it.  Texture slots
   therefore live in the MATERIAL tree, framed by feature; groups take plain
   colour/vector inputs.
2. Menu Switch is Blender 5.2+.  On 5.0/5.1 `_menu()` emits a compare+Mix ladder
   with identical surrounding wiring, so one script runs on both.
3. Custom properties drive node values through AVERAGE drivers with a single
   SINGLE_PROP variable -- no scripted expressions, so this works without
   "Auto Run Python Scripts".
"""

import bpy


def _detect_menu_switch():
    """Find a usable shader-tree Menu Switch bl_idname, and separately whether
    its selector can actually be controlled from outside the node -- these
    turn out to be two different questions with two different answers, found
    by testing on a real Blender 5.2.0 LTS build (`GeometryNodeMenuSwitch`
    placed in a `ShaderNodeTree`, confirmed via a Tree Clipper export of a
    material the user built by hand in the Shader Editor), not guessed:

    1. **Existence / placeability is not a straightforward version check.**
       `hasattr(bpy.types, "ShaderNodeMenuSwitch")` is a literal string check
       (not a fuzzy version comparison), and it is False on 5.2.0 LTS: no
       `ShaderNode`-prefixed class exists.  The real, only class is
       `GeometryNodeMenuSwitch` -- confirmed present since at least 5.0.1 too.
       `nodes.new(idname)` does not enforce tree-type compatibility (that is
       `poll()`'s job, which the direct data-API does not call), so a naive
       "did it instantiate" test is a false positive: on 5.0.1 it happily
       creates a GeometryNode inside a ShaderNodeTree that cannot compile.
       `GeometryNodeMenuSwitch.poll(shader_tree)` reports False even on 5.2.0
       LTS -- yet the Shader Editor's own Add menu lets a user place one
       anyway (per the Tree Clipper dump). So neither "did nodes.new()
       succeed" nor "does poll() agree" is fully trustworthy on its own; both
       are recorded so the build log shows what was actually found rather
       than a version guess.

    2. **Selectability is the fact that actually matters here, and it is
       negative.** The Menu socket's `default_value` -- and the companion
       `FunctionNodeInputMenu.value` -- both raise
       `TypeError: property "default_value" not animatable` from
       `driver_add()`, confirmed directly against 5.2.0 LTS.  Menu selection
       is evidently resolved as a compile-time/specialization choice, not a
       per-shading-point value, so it cannot be driven live from a numeric
       material custom property without a scripted driver (which this file
       deliberately avoids -- see the module docstring on "Auto Run Python
       Scripts").  A plain Float *links* into a Menu socket without Blender
       raising -- but `links.new()` is permissive about socket-type mismatches
       and that success is not evidence the link means anything at shading
       time, so it is not treated as a substitute for the driver test.

    Net effect: **the comparison-ladder fallback in `_menu()` is not a
    compatibility shim for "Menu Switch doesn't exist yet" -- it is the only
    mechanism, on any Blender version tested so far, that satisfies this
    project's actual requirement of live custom-property control without a
    scripted driver.** `HAS_MENU_SWITCH` is therefore gated on both existence
    AND drivability, not existence alone; today that means it is False
    regardless of which candidate name matches."""
    candidates = ["ShaderNodeMenuSwitch", "FunctionNodeMenuSwitch",
                  "NodeMenuSwitch", "GeometryNodeMenuSwitch"]
    scratch = bpy.data.node_groups.new("_ff7r_menu_probe", "ShaderNodeTree")
    try:
        for idn in candidates:
            try:
                node = scratch.nodes.new(idn)
            except RuntimeError:
                continue
            cls = getattr(bpy.types, idn, None)
            polls = cls.poll(scratch) if cls is not None and hasattr(cls, "poll") else None
            drivable = False
            try:
                fc = node.inputs[0].driver_add("default_value")
                drivable = True
                node.inputs[0].driver_remove("default_value")
            except TypeError:
                pass
            except Exception:
                pass
            print("Menu Switch: %r instantiates in a ShaderNodeTree "
                  "(poll=%s, selector drivable=%s)" % (idn, polls, drivable))
            if drivable:
                return idn, True
            if MENU_SWITCH_BEST[0] is None:
                MENU_SWITCH_BEST[0] = idn
        if MENU_SWITCH_BEST[0] is not None:
            print("Menu Switch: %r exists but its selector is not drivable "
                  "from a custom property on this build (Blender %s) -- "
                  "using the comparison-ladder fallback, which is not a "
                  "version shim here, see _detect_menu_switch()'s docstring."
                  % (MENU_SWITCH_BEST[0], bpy.app.version_string))
            return MENU_SWITCH_BEST[0], False
        print("Menu Switch: no candidate instantiates in a ShaderNodeTree on "
              "this build (Blender %s) -- using the fallback ladder."
              % bpy.app.version_string)
        return None, False
    finally:
        bpy.data.node_groups.remove(scratch)


MENU_SWITCH_BEST = [None]   # mutable cell _detect_menu_switch() writes into
MENU_SWITCH_IDNAME, MENU_SWITCH_DRIVABLE = _detect_menu_switch()


def _detect_menu_input():
    """Return the Constant Menu node's idname, or None on builds without one.

    A Menu Switch is only half of the manual path: its selector has to be fed
    by one shared constant-menu node, which is also the single user-facing
    dropdown.  Blender 5.0/5.1 ship the Menu Switch -- `GeometryNodeMenuSwitch`
    even instantiates inside a ShaderNodeTree there -- but not the constant
    menu, so probing for the switch alone made those builds choose a path they
    could not finish: `RuntimeError: Node type FunctionNodeInputMenu undefined`
    from build(), measured on 5.0.1.  Probe for both halves, and let 5.1 and
    earlier take exactly the same comparison ladder 4.x takes."""
    candidates = ["FunctionNodeInputMenu", "ShaderNodeInputMenu",
                  "NodeInputMenu", "FunctionNodeMenuInput"]
    scratch = bpy.data.node_groups.new("_ff7r_menu_input_probe", "ShaderNodeTree")
    try:
        for idn in candidates:
            try:
                scratch.nodes.new(idn)
            except RuntimeError:
                continue
            print("Constant Menu: %r instantiates in a ShaderNodeTree." % idn)
            return idn
        return None
    finally:
        bpy.data.node_groups.remove(scratch)


MENU_INPUT_IDNAME = _detect_menu_input()
# A manual Menu Switch remains useful even though its selector cannot be
# driven: it is clearer in the Shader Editor and Python can set its enum value
# at material creation time.  It needs the constant-menu node to drive it,
# though, so both have to exist before build() may take that path -- see
# _detect_menu_input().  Property-driven selectors still require the comparison
# ladder below until Blender exposes an animatable Menu socket.
HAS_MANUAL_MENU_SWITCH = (MENU_SWITCH_IDNAME is not None and
                          MENU_INPUT_IDNAME is not None)
if MENU_SWITCH_IDNAME is not None and MENU_INPUT_IDNAME is None:
    print("Constant Menu: %r exists but this build (Blender %s) has no "
          "constant-menu node to select with -- using the same comparison "
          "ladder as 4.x." % (MENU_SWITCH_IDNAME, bpy.app.version_string))
HAS_MENU_SWITCH = MENU_SWITCH_IDNAME is not None and MENU_SWITCH_DRIVABLE


def _detect_bundles():
    """True when a bundle in a ShaderNodeTree can carry what this file bundles.

    Existence of the nodes is not the question.  Blender 5.0 ships
    NodeCombineBundle / NodeSeparateBundle and lists BOOLEAN in the socket-type
    enum, yet a bundle inside a ShaderNodeTree refuses to hold one --
    `RuntimeError: Error: Unable to create item with this socket type`,
    measured on 5.0.1, which accepts FLOAT / INT / VECTOR / RGBA and rejects
    BOOLEAN and MENU.  5.2.0 LTS accepts all of them (measured).  The 5.1
    release notes do not say which side of that line 5.1 falls on, so ask the
    running build instead of hard-coding a version: every switch is bundled as
    BOOLEAN and every parameter as FLOAT, so create one of each and let the
    answer decide.  Where it fails, the material keeps the direct property
    links -- exactly what 4.x does, and no feature is lost, only the tidier
    single-wire routing."""
    if not (hasattr(bpy.types, "NodeCombineBundle") and
            hasattr(bpy.types, "NodeSeparateBundle")):
        return False
    scratch = bpy.data.node_groups.new("_ff7r_bundle_probe", "ShaderNodeTree")
    try:
        node = scratch.nodes.new("NodeCombineBundle")
        try:
            node.define_signature = True
        except Exception:
            pass
        for socket_type in ("BOOLEAN", "FLOAT"):
            try:
                node.bundle_items.new(socket_type, "probe_" + socket_type)
            except Exception:
                print("Bundles: this build (Blender %s) cannot put a %s into a "
                      "shader bundle -- material properties stay on direct "
                      "links." % (bpy.app.version_string, socket_type))
                return False
        print("Bundles: shader bundles carry BOOLEAN and FLOAT -- routing the "
              "material properties through one.")
        return True
    except Exception:
        print("Bundles: no usable Combine Bundle on this build (Blender %s) -- "
              "material properties stay on direct links."
              % bpy.app.version_string)
        return False
    finally:
        bpy.data.node_groups.remove(scratch)


HAS_BUNDLES = _detect_bundles()
HAS_CONVENTION = "convention" in bpy.types.ShaderNodeNormalMap.bl_rna.properties


def _detect_thin_wall():
    """Return Principled's thin-wall input name, or None on builds without it.

    Cloth (SM8) is a thin translucent sheet, which Principled can only express
    from Blender 5.2 on.  Probe for the socket rather than testing the version
    number -- same reasoning as HAS_CONVENTION -- so this stays correct if the
    input arrives under a different spelling.  Absence is not an error: it
    means Cloth keeps exactly the parameters it had on 5.1 and earlier."""
    scratch = bpy.data.node_groups.new("_ff7r_thinwall_probe", "ShaderNodeTree")
    try:
        node = scratch.nodes.new("ShaderNodeBsdfPrincipled")
        for nm_ in ("Thin Wall", "Thin wall", "Thin Walled", "Thinwall"):
            if node.inputs.get(nm_) is not None:
                print("Thin Wall: Principled exposes %r -- Cloth gets "
                      "subsurface 1.0 and thin wall." % nm_)
                return nm_
        print("Thin Wall: Principled has no thin-wall input on this build "
              "(Blender %s) -- Cloth left unchanged."
              % bpy.app.version_string)
        return None
    finally:
        bpy.data.node_groups.remove(scratch)


THIN_WALL_SOCKET = _detect_thin_wall()


def _detect_panel_states():
    """Return the Node attribute holding per-panel collapse state, or None.

    Principled draws Subsurface / Specular / Transmission / Coat / Sheen /
    Emission / Thin Film as collapsed sections, and this material drives most
    of them, so they are worth arriving open.  Blender 4.5 and 5.0 keep that
    state in DNA without exposing it to RNA (measured on 4.5.3 and 5.0.1:
    neither `panel_states` nor `panels` exists on a node), so probe for it the
    same way as the other capabilities rather than testing a version: expand
    the sections on builds that allow it, leave them alone on builds that do
    not.  Absence is not an error -- it costs the user one click per panel."""
    scratch = bpy.data.node_groups.new("_ff7r_panel_probe", "ShaderNodeTree")
    try:
        node = scratch.nodes.new("ShaderNodeBsdfPrincipled")
        for attr in ("panel_states", "panels"):
            states = getattr(node, attr, None)
            if not states:
                continue
            try:
                states[0].is_collapsed = False
            except Exception:
                continue
            print("Node panels: %r is writable -- Principled ships with its "
                  "sections expanded." % attr)
            return attr
        print("Node panels: this build (Blender %s) does not expose node panel "
              "state to Python -- Principled keeps Blender's own default."
              % bpy.app.version_string)
        return None
    finally:
        bpy.data.node_groups.remove(scratch)


PANEL_STATE_ATTR = _detect_panel_states()


def _expand_panels(node):
    """Open every collapsed section on `node`, where the build allows it."""
    if PANEL_STATE_ATTR is None:
        return
    for state in getattr(node, PANEL_STATE_ATTR, ()):
        try:
            state.is_collapsed = False
        except Exception:
            log("panel expand failed on %s" % getattr(node, "name", "?"))
            return

MAT_NAME = "FF7R_RMI_Surface_Master"

# ---------------------------------------------------------------------------
# Literals hoisted out of the compiled shaders.  PLAN 0.2: the material JSONs
# carry no per-instance numerics worth reading, so these constants ARE the
# parameters.  Everything here is also exposed as a material custom property.
# ---------------------------------------------------------------------------

# base pass / GBuffer
F0_KNEE = 0.666667                  # GBufferA.a piecewise remap knee
F0_LO, F0_HI = 0.06, 0.36
F0_HI_BIAS = 0.2
F0_CLAMP = 0.16
SPEC_LEVEL_SCALE = 0.08             # Blender: Specular IOR Level = F0 / 0.08
OPACITY_CLIP = 0.3333               # BasePropertyOverrides.OpacityMaskClipValue

# eye  (from ff7r_eye_material_v2.py)
CORNEA_SLOPE = 0.911537             # cornea dome xy scale
ETA = 0.671141                      # 1 / 1.49, the cornea IOR
DEPTH_CONST = 0.373500              # iris depth = max(0, C - F*r^2) * S
DEPTH_FALLOFF = 0.279500
DEPTH_SCALE = 0.407664
MASK_INNER = 0.900000               # sclera mask = smoothstep(IN, OUT, |uvC|)
MASK_OUTER = 1.100000
LIMBAL_OFFSET = 0.780000
LIMBAL_SOFTNESS = 0.550000
LIMBAL_APERTURE = 2.700000
EYE_UV_CENTER = (0.5, 0.5)
EYE_UV_RADIUS = 0.25
_INV = 1.0 / EYE_UV_RADIUS
UV_MAP = ""                         # "" = the object's ACTIVE uv layer

# hair  (from ff7r_hair_material.py -- measured values, not guesses)
MIN_VDOTT = 0.05                    # shader clamps |dot(V, strand)| off zero
HAIR_SPECULAR_F0 = 0.046520         # GBufferA.a 0.684779 through the remap
ROUGHNESS_CURVATURE = 0.18          # specular-AA curvature term, saturated
HAIR_ANISOTROPIC = 1.0
FLOW_INFLUENCE = 0.0                # 0 = capture-exact

# blood
BLOOD_R = (0.0802, 0.8328)          # channel = a + b*s
BLOOD_G = (0.00368, -0.00368)
BLOOD_B = (0.00802, -0.0065)

_LOG = []
_UV_NOTES = []
_OBJ_PROP_NOTES = []


def log(msg):
    _LOG.append(msg)


# ---------------------------------------------------------------- helpers ---

def _new(tree, idname, name, loc, label="", parent=None):
    n = tree.nodes.new(idname)
    n.name = name
    if label:
        n.label = label
    if parent is not None:
        n.parent = parent
    n.location = loc
    return n


def _prop(node, attr, value):
    try:
        setattr(node, attr, value)
    except Exception:
        log("prop failed: %s.%s" % (getattr(node, "name", "?"), attr))


def _in(node, key):
    """Socket by NAME, falling back to index.  Principled's socket ORDER moves
    between Blender versions; its names do not (PLAN 3.3)."""
    try:
        return node.inputs[key]
    except (KeyError, IndexError, TypeError):
        return None


def _sv(node, key, value):
    s = _in(node, key)
    if s is None:
        log("no socket %r on %s" % (key, node.name))
        return
    try:
        dv = s.default_value
        if hasattr(dv, "__len__") and not isinstance(dv, str):
            n = len(dv)
            v = list(value) if hasattr(value, "__len__") else [value] * n
            s.default_value = (v + [1.0] * n)[:n]
        else:
            s.default_value = value
    except Exception:
        log("set failed %s[%s]" % (node.name, key))


def _sov(node, value):
    """ShaderNodeValue keeps its value on outputs[0], not inputs[0]."""
    try:
        node.outputs[0].default_value = value
    except Exception:
        log("value set failed on %s" % getattr(node, "name", "?"))


def _link(tree, a, ao, b, bi):
    try:
        fs = a.outputs[ao]
        ts = b.inputs[bi]
    except (KeyError, IndexError, AttributeError, TypeError):
        log("broken link %s[%s] -> %s[%s]"
            % (getattr(a, "name", "?"), ao, getattr(b, "name", "?"), bi))
        return None
    try:
        return tree.links.new(fs, ts)
    except Exception:
        log("link refused %s -> %s" % (a.name, b.name))
        return None


def _frame(tree, name, label, parent=None):
    f = tree.nodes.new("NodeFrame")
    f.name = name
    f.label = label
    f.shrink = True
    f.label_size = 22
    if parent is not None:
        f.parent = parent
    return f


def _sock(g, name, in_out, stype, default=None, mn=None, mx=None):
    it = g.interface.new_socket(name, in_out=in_out, socket_type=stype)
    if default is not None:
        try:
            it.default_value = default
        except Exception:
            pass
    for attr, val in (("min_value", mn), ("max_value", mx)):
        if val is not None:
            try:
                setattr(it, attr, val)
            except Exception:
                pass
    return it


def _rebuild_group(name, builder):
    g = bpy.data.node_groups.get(name)
    if g is None:
        g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    else:
        g.nodes.clear()
        try:
            for it in list(g.interface.items_tree):
                g.interface.remove(it)
        except Exception:
            pass
    builder(g)
    _hide_unused_system_outputs(g)
    return g


def _grp(tree, gname, node_name, loc, label="", parent=None):
    n = _new(tree, "ShaderNodeGroup", node_name, loc, label or gname, parent)
    n.node_tree = bpy.data.node_groups.get(gname)
    if n.node_tree is None:
        log("missing group %r for %s" % (gname, node_name))
    return n


def _hide_unused_system_outputs(tree):
    """Apply Ctrl+H-style output hiding to high-fanout system nodes.

    Geometry and Light Path otherwise display a large wall of irrelevant
    sockets.  This is intentionally limited to those nodes: ordinary shader
    nodes often use unlinked inputs as authored constants and should stay
    visible for editing.
    """
    for node in tree.nodes:
        if node.bl_idname not in {"ShaderNodeNewGeometry", "ShaderNodeLightPath"}:
            continue
        for socket in node.outputs:
            socket.hide = not socket.is_linked


# ------------------------------------------------ DirectX normal convention --

_MISSING_CONVENTION = []
_CONVENTION_FIXED = []


def _convention(node, value):
    """Normal Map 'convention' is Blender 5.2+.  On 5.0/5.1 the property does
    not exist, so a DIRECTX map would silently lose its green flip.  Record it
    and splice explicit maths in after linking."""
    if HAS_CONVENTION:
        _prop(node, "convention", value)
    elif value == "DIRECTX":
        _MISSING_CONVENTION.append(node)


def _fix_conventions(tree):
    """5.0/5.1 fallback: insert (r, 1-g, b) before each DIRECTX Normal Map --
    exactly what convention='DIRECTX' does natively on 5.2+."""
    for node in list(_MISSING_CONVENTION):
        if node.id_data is not tree:
            continue
        col = _in(node, "Color")
        if col is None or not col.is_linked:
            _MISSING_CONVENTION.remove(node)
            continue
        link = col.links[0]
        src = link.from_socket
        tree.links.remove(link)
        flip = tree.nodes.new("ShaderNodeVectorMath")
        flip.operation = "MULTIPLY_ADD"
        flip.name = node.name + " green flip"
        flip.label = "green flip (DirectX -> OpenGL)"
        flip.parent = node.parent
        flip.location = (node.location[0] - 190, node.location[1] - 120)
        flip.inputs[1].default_value = (1.0, -1.0, 1.0)
        flip.inputs[2].default_value = (0.0, 1.0, 0.0)
        tree.links.new(src, flip.inputs[0])
        tree.links.new(flip.outputs[0], col)
        _CONVENTION_FIXED.append(node.name)
        _MISSING_CONVENTION.remove(node)


# ------------------------------------------------------- custom properties ---

# Defaults used by the original 47-switch implementation. The current-build
# parent materials establish a 126-flag universe. Every flag is exposed below,
# but flags with identical presence vectors across all 104 ground-truth variants
# share ONE checkbox (the checkbox name is the complete "A_ + B_" list). This
# prevents the UI from claiming separability no carrier or decompilation proves.
_SWITCH_DEFAULTS = dict([
    ("Color_", 1.0), ("Normal_", 1.0), ("Roughness_", 1.0), ("Occlusion_", 1.0),
    ("WideOcclusion_", 1.0), ("Metallic_", 0.0), ("Standard_", 1.0),
    ("Coverage_", 0.0), ("Detail_", 0.0), ("DetailNormal_", 0.0),
    ("DetailRoughness_", 0.0), ("DetailColor_", 0.0), ("DetailOcclusion_", 0.0),
    ("Skin_", 0.0), ("Pores_", 0.0), ("Subsurface_", 0.0),
    ("Fabric_", 0.0), ("Diffusion_", 0.0),
    ("Hair_", 0.0), ("CylindricalNormal_", 0.0), ("WideBentNormal_", 0.0),
    ("Eye_", 0.0), ("ScleraNormal_", 0.0), ("GazeNormal_", 0.0),
    ("IrisColor_", 0.0), ("IrisNormal_", 0.0), ("IrisOcclusion_", 0.0),
    ("IrisEmissive_", 0.0), ("EyeMigration_", 0.0),
    ("Segmented_", 0.0), ("SegmentLayer0_", 0.0), ("SegmentLayer1_", 0.0),
    ("Layered_", 0.0), ("ShadingLayer_", 0.0),
    ("Emissive_", 0.0), ("ExtraEmissive_", 0.0), ("ExternalEmissive_", 0.0),
    ("Blood_", 0.0), ("OxygenSaturation_", 0.0),
    ("PositiveTransitionNormal_", 0.0), ("VertexExpressionBone_", 0.0),
    ("Film_", 0.0), ("FilmThickness_", 0.0),
    ("Materia_", 0.0), ("MateriaContext_", 0.0),
    ("Gradient_", 0.0), ("Isotropy_", 0.0),
])

# Exact union of StaticSwitchParameters in the 104 current-build
# Renderer/MaterialInstance/Surface/RMI_Surface_*.json parents.
KNOWN_SWITCHES = [
    "AnimateTime_", "Blood_", "BrokenFlowEmissive0_", "BrokenFlowEmissive1_",
    "BrokenFlow_", "Color_", "Convex_", "Coordinate0_", "Coordinate1_",
    "Coordinate2_", "Coordinate3_", "CoverageIdentifierCoordinate2_",
    "CoverageIdentifierCoordinate3_", "CoverageIdentifier_", "Coverage_",
    "CylindricalNormal_", "Deform_", "DetailColor_", "DetailCoverage_",
    "DetailFoam_", "DetailMetallic_", "DetailNormal_", "DetailOcclusion_",
    "DetailRoughness_", "Detail_", "Diffusion_", "Distribution_", "Emissive_",
    "ExternalEmissive_", "ExtraColor_", "ExtraEmissive_",
    "ExtraNormalizedCoordinate_", "ExtraPixelCoordinate_", "EyeMigration_",
    "Eye_", "Fabric_", "FilmStructure_", "FilmThickness_", "Film_",
    "FlowDetailFoam_", "FlowDetailNormal_", "FlowDetailRoughness_",
    "FlowDirection_", "FlowFoam_", "FlowNormal_", "FlowRoughness_", "Flow_",
    "Foam_", "Froth_", "GazeNormal_", "GenericVector_", "Ghost_",
    "GradientNormal_", "GradientOcclusion_", "GradientPhase_",
    "GradientRoughness_", "Gradient_", "Hair_", "HologramEye_", "Hologram_",
    "IrisColor_", "IrisEmissive_", "IrisNormal_", "IrisOcclusion_",
    "Isotropy_", "LayeredColor_", "Layered_", "MateriaContext_",
    "MateriaIndexed_", "Materia_", "Metallic_", "Muddiness_", "Normal_",
    "Occlusion_", "OceanGeometry_", "OceanMagnitude_", "Ocean_", "Opacity_",
    "OxygenSaturation_", "Phantom_", "PhaseGCrystal_", "PhaseGGlass_",
    "PhaseGWater_", "Pool_", "Pores_", "PositiveTransitionNormal_",
    "ReflectanceCrystal_", "ReflectanceGlass_", "ReflectanceWater_",
    "RenderPassWater_", "RigidBody_", "Roughness_", "ScleraNormal_",
    "SegmentLayer0_", "SegmentLayer1_", "Segmented_", "ShadingLayer_", "Skin_",
    "SkinnedBody_", "SoftBody_", "Standard_", "Subsurface_", "Thick_",
    "Thickness_", "Thin_", "Thinness_", "ToneGradient_",
    "TransmittanceCoefficient_", "Transmittance_", "TransparencyCoefficient_",
    "Transparency_", "Unlit_", "VertexExpressionBone_",
    "VertexExpressionNormal_", "VertexExpressionOcean_",
    "VertexExpressionPosition_", "VertexExpression_", "ViewCoordinate_",
    "WetnessTrickle_", "Wetness_", "WideBentNormal_", "WideFlowDirection_",
    "WideFlowDirectivity_", "WideFlowPhase_", "WideFoamThreshold_",
    "WideOcclusion_",
]

# Exact identical-presence classes across the 104 current-build variants.
# Keep this table synchronized with scripts/renderer_ground_truth.json. A future
# breaker variant should split its class here as soon as it is observed.
COOCCURRENCE_GROUPS = [
    ("Blood_", "OxygenSaturation_"),
    ("BrokenFlowEmissive0_", "BrokenFlowEmissive1_", "BrokenFlow_",
     "WideFlowDirection_"),
    ("DetailColor_", "DetailMetallic_", "LayeredColor_"),
    ("DetailFoam_", "FlowDetailFoam_", "FlowDetailNormal_",
     "FlowDetailRoughness_", "FlowDirection_", "FlowFoam_", "FlowNormal_",
     "FlowRoughness_", "Flow_", "Foam_", "WideFoamThreshold_"),
    ("DetailNormal_", "DetailRoughness_"),
    ("Diffusion_", "Fabric_"),
    ("ExtraNormalizedCoordinate_", "ExtraPixelCoordinate_"),
    ("Eye_", "IrisColor_", "IrisNormal_", "ScleraNormal_"),
    ("FilmStructure_", "FilmThickness_", "Film_"),
    ("Froth_", "Pool_", "RenderPassWater_"),
    ("GradientNormal_", "GradientOcclusion_", "GradientPhase_",
     "GradientRoughness_", "Gradient_"),
    ("Hair_", "WideBentNormal_"),
    ("Layered_", "ShadingLayer_"),
    ("MateriaContext_", "MateriaIndexed_"),
    ("Materia_", "VertexExpressionNormal_"),
    ("Muddiness_", "TransmittanceCoefficient_", "Transmittance_",
     "TransparencyCoefficient_", "Transparency_"),
    ("OceanGeometry_", "OceanMagnitude_", "Ocean_", "VertexExpressionOcean_"),
    ("PhaseGCrystal_", "ReflectanceCrystal_"),
    ("PhaseGGlass_", "Thin_", "Thinness_"),
    ("PhaseGWater_", "ReflectanceWater_"),
    ("Pores_", "Skin_"),
    ("PositiveTransitionNormal_", "VertexExpressionBone_"),
    ("SegmentLayer0_", "SegmentLayer1_", "Segmented_"),
    ("WideFlowDirectivity_", "WideFlowPhase_"),
]

_GROUP_FOR_SWITCH = {member: group for group in COOCCURRENCE_GROUPS
                     for member in group}
SWITCH_CONTROLS = []
for _flag in KNOWN_SWITCHES:
    _members = _GROUP_FOR_SWITCH.get(_flag, (_flag,))
    if _flag != _members[0]:
        continue
    _full_control = " + ".join(_members)
    # Blender ID-property keys are length-limited. Keep the complete list in
    # the UI description and node label when it cannot fit in the checkbox key.
    _control = (_full_control if len(_full_control) <= 63 else
                "%s + ... + %s (%d flags)" %
                (_members[0], _members[-1], len(_members)))
    _default = any(bool(_SWITCH_DEFAULTS.get(m, 0.0)) for m in _members)
    SWITCH_CONTROLS.append((_control, _members, _default))
CONTROL_FOR_SWITCH = {member: control for control, members, _default
                      in SWITCH_CONTROLS for member in members}

# Discrete selectors: integers, not floats -- a "Shading Model" of 2.5 is as
# meaningless as a switch of 0.37.  Drivers read ints fine.
ENUM_PROPS = [
    ("Shading Model", 0, 0, 6,
     "0 Standard | 1 Skin | 2 Subsurface Profile | 3 Hair | 4 Cloth | 5 Eye | 6 Unlit"),
    ("Coverage Mode", 0, 0, 2,
     "0 Alpha (native) | 1 Hard cutout | 2 Dithered (faithful)"),
    ("Detail Blend", 0, 0, 2,
     "0 Slerp (character) | 1 Chained preview | 2 RNM (MEC); Isotropy_ forces RNM"),
    ("Vertex Expression Source", 0, 0, 2,
     "0 Constant | 1 Geometry attribute | 2 Object property"),
]

FLOAT_PROPS = [
    # name,                     default,             min,  max,  description
    ("Object Fade", 1.0, 0.0, 1.0, "Per-object fade/dissolve (UE b1[27].w)"),
    ("Opacity Clip", OPACITY_CLIP, 0.0, 1.0,
     "Alpha-test threshold; authored value is 0.3333"),
    ("GBufferA.a", 0.645420, 0.0, 1.0,
     "Dielectric specular level; eye 0.6454, hair 0.6848, blood 0.4438"),
    # --- eye (ff7r_eye_material_v2.py) ---
    ("Eye Cornea Slope", CORNEA_SLOPE, 0.0, 4.0, "Cornea dome xy scale"),
    ("Eye Eta", ETA, 0.0, 2.0, "1/IOR at the cornea; 0.671141 = IOR 1.49"),
    ("Eye Depth Const", DEPTH_CONST, 0.0, 2.0, "Iris depth constant term"),
    ("Eye Depth Falloff", DEPTH_FALLOFF, 0.0, 2.0, "Iris depth r^2 falloff"),
    ("Eye Depth Scale", DEPTH_SCALE, 0.0, 2.0, "Iris parallax scale"),
    ("Eye Mask Inner", MASK_INNER, 0.0, 2.0, "Limbus smoothstep inner radius"),
    ("Eye Mask Outer", MASK_OUTER, 0.0, 2.0, "Limbus smoothstep outer radius"),
    ("Eye Limbal Offset", LIMBAL_OFFSET, 0.0, 2.0, "Limbal ring slide"),
    ("Eye Limbal Softness", LIMBAL_SOFTNESS, 0.0, 2.0, "Limbal edge hardness"),
    ("Eye Limbal Aperture", LIMBAL_APERTURE, 0.0, 8.0, "Off-axis closing rate"),
    ("Eye Limbal Amount", 1.0, 0.0, 1.0, "Limbal occlusion strength"),
    ("Pupil Dilation", 1.0, 0.05, 4.0, "Iris UV exponent; 1.0 = neutral"),
    # --- hair (ff7r_hair_material.py -- measured) ---
    ("Hair Min VdotT", MIN_VDOTT, 0.001, 0.5,
     "Shader clamps |dot(V, strand)| away from zero"),
    ("Hair Dither Sign", 1.0, 0.0, 1.0,
     "1 = dithered sign (matches capture, hides the seam); 0 = hard sign()"),
    ("Hair Roughness Curvature", ROUGHNESS_CURVATURE, 0.0, 1.0,
     "Specular-AA curvature floor; saturates at 0.18 on hair cards"),
    ("Hair Anisotropic", HAIR_ANISOTROPIC, -1.0, 1.0,
     "NOTE: EEVEE ignores Anisotropic/Tangent entirely (measured)"),
    ("Hair Flow Influence", FLOW_INFLUENCE, 0.0, 1.0,
     "Fold the flow map into the strand axis; 0 = capture-exact"),
    ("Hair Specular F0", HAIR_SPECULAR_F0, 0.0, 0.16,
     "Measured 0.046520 from GBufferA.a = 0.684779"),
    # --- detail layer ---
    ("Detail Normal Strength", 0.5, 0.0, 1.0,
     "DetailNormal intensity; 1.0 = the authored map, 0.5 = the import default"),
    # --- shading model parameters ---
    ("Subsurface Scale", 0.05, 0.0, 1.0, "SSS radius scale in scene units"),
    ("Sheen Roughness", 0.3, 0.0, 1.0, "Cloth/fur sheen roughness"),
    # --- constant roles (PLAN 0.4: never a texture anywhere in the game) ---
    ("Isotropy", 0.0, 0.0, 1.0, "Constant in all 9,485 materials"),
    ("Sheen", 1.0, 0.0, 1.0, "Never a real texture"),
    ("Wetness", 0.0, 0.0, 1.0, "Never a real texture; path is unused content-side"),
    ("DetailWetness", 0.0, 0.0, 1.0, "Never a real texture"),
    ("Visibility", 1.0, 0.0, 1.0, "Never a real texture"),
    ("FilmStructure", 0.4, 0.0, 1.0, "Never a real texture"),
    ("ExtraColor", 0.0, 0.0, 1.0, "Never a real texture"),
    ("OxygenSaturation", 0.078, 0.0, 1.0,
     "Always a solid colour; only 3 assets exist (0.078 / 0.082 / 0.251)"),
    ("DetailMetallic", 0.0, 0.0, 1.0, "Always solid 0"),
    ("Thickness", 0.2, 0.0, 1.0, "Always solid 0.2"),
    ("FilmThickness", 0.5, 0.0, 1.0, "Always solid 0.5"),
]


def _ensure_props(mat):
    """Switches are real booleans, selectors real ints, parameters floats.

    Re-running over a material written by an older version of this script
    rewrites any switch still stored as a float, so the type upgrade is not
    stranded behind the "already present" check."""
    for name, members, default in SWITCH_CONTROLS:
        # Upgrade the old one-property-per-flag layout without losing an enabled
        # member: a grouped control becomes true if ANY legacy member was true.
        legacy = [bool(mat[m]) for m in members if m in mat]
        if name not in mat or not isinstance(mat[name], bool):
            mat[name] = bool(mat[name]) if name in mat else (
                any(legacy) if legacy else bool(default))
        try:
            mat.id_properties_ui(name).update(
                default=bool(default),
                description=("RMI_Surface static switch" if len(members) == 1
                             else "Always co-occurring RMI_Surface flags: " +
                             " + ".join(members)))
        except Exception:
            pass
        for member in members:
            if member != name and member in mat:
                del mat[member]
    for name, default, mn, mx, desc in ENUM_PROPS:
        if name not in mat or isinstance(mat[name], (bool, float)):
            mat[name] = int(mat[name]) if name in mat else int(default)
        try:
            mat.id_properties_ui(name).update(
                min=mn, max=mx, soft_min=mn, soft_max=mx,
                default=int(default), description=desc)
        except Exception:
            pass
    for name, default, mn, mx, desc in FLOAT_PROPS:
        if name not in mat or isinstance(mat[name], (bool, int)):
            mat[name] = float(default)
        try:
            mat.id_properties_ui(name).update(
                min=mn, max=mx, soft_min=mn, soft_max=mx,
                default=float(default), description=desc)
        except Exception:
            pass


def _drive(socket, mat, prop):
    """Bind a float socket to mat["prop"] with an AVERAGE driver over a single
    SINGLE_PROP variable -- a pass-through that needs no scripted expression, so
    it works without 'Auto Run Python Scripts'."""
    if socket is None:
        log("drive: no socket for %r" % prop)
        return None
    try:
        fc = socket.driver_add("default_value")
    except Exception:
        log("drive: driver_add failed for %r" % prop)
        return None
    if isinstance(fc, list):          # vector socket -- not supported here
        for f in fc:
            f.id_data.animation_data.drivers.remove(f)
        return None
    d = fc.driver
    d.type = "AVERAGE"
    v = d.variables.new()
    v.name = "p"
    v.type = "SINGLE_PROP"
    t = v.targets[0]
    t.id_type = "MATERIAL"
    t.id = mat
    t.data_path = '["%s"]' % prop
    return fc


def _bundle_material_properties(tree, property_sources, property_types,
                                props_frame):
    """Route driven material values through one bundle and one separator/frame.

    Blender's Ctrl+H on a node hides unused sockets.  The data-API equivalent
    is ``socket.hide = True``; after rewiring, every Separate Bundle therefore
    exposes only the outputs actually consumed inside its parent frame.

    Shading Model is intentionally absent: in Blender 5.2 it is a Constant Menu,
    not a driven material-property value, and Menu enum definitions do not
    survive independently declared bundle signatures.
    """
    if not HAS_BUNDLES:
        return {}

    # Capture the existing direct property links before the Combine Bundle is
    # added, otherwise its own inputs would be mistaken for destinations.
    routes = []
    for prop_name, source in property_sources.items():
        if prop_name == "Shading Model" or not source.outputs:
            continue
        for link in list(source.outputs[0].links):
            frame = getattr(link.to_node, "parent", None)
            if frame is not None and frame != props_frame:
                routes.append((prop_name, link, frame))

    bundled_names = [name for name in property_sources if name != "Shading Model"]
    # Blender 5.x only: no 4.5 dump position exists for these, so the bundle
    # sits one column right of the float parameters, and each Separate Bundle
    # just above the frame that consumes it.
    combine = _new(tree, "NodeCombineBundle", "MaterialPropertiesBundle",
                   (FLOAT_COLUMN[0] + 400, FLOAT_COLUMN[1]),
                   "All material custom properties", props_frame)
    combine.define_signature = True
    combine_socket_names = {}
    for prop_name in bundled_names:
        item = combine.bundle_items.new(property_types[prop_name], prop_name)
        # Bundle paths sanitise punctuation (`+`/`.` become `_`). Retain the
        # actual socket name returned by Blender instead of assuming the label.
        combine_socket_names[prop_name] = item.name
        _link(tree, property_sources[prop_name], 0, combine, item.name)

    separators = {}
    separator_socket_names = {}
    used = {}
    for prop_name, old_link, frame in routes:
        frame_key = frame.name
        sep = separators.get(frame_key)
        if sep is None:
            key = frame_key.replace("FRAME_", "")
            origin = FRAME_ORIGIN.get(key, (-600, 400))
            sep = _new(tree, "NodeSeparateBundle", "PROPS_" + frame_key,
                       BUNDLE_SEPARATOR_POS.get(
                           key, (origin[0] + FRAME_PAD[0],
                                 origin[1] + BUNDLE_SEPARATOR_DY)),
                       "Material properties used here", frame)
            sep.define_signature = True
            names = {}
            for bundled_name in bundled_names:
                item = sep.bundle_items.new(property_types[bundled_name], bundled_name)
                names[bundled_name] = item.name
            _link(tree, combine, "Bundle", sep, "Bundle")
            separators[frame_key] = sep
            separator_socket_names[frame_key] = names
            used[frame_key] = set()
        destination = old_link.to_socket
        tree.links.remove(old_link)
        try:
            socket_name = separator_socket_names[frame_key][prop_name]
            tree.links.new(sep.outputs[socket_name], destination)
            used[frame_key].add(socket_name)
        except Exception:
            log("bundle link refused: %s -> %s" %
                (prop_name, getattr(destination.node, "name", "?")))

    # Python equivalent of selecting every Separate Bundle and pressing Ctrl+H.
    for frame_key, sep in separators.items():
        visible = used[frame_key]
        for socket in sep.outputs:
            if socket.bl_idname != "NodeSocketVirtual":
                socket.hide = socket.name not in visible
    return separators


# ------------------------------------------------------------- node layout --
# Every position below is an ABSOLUTE canvas coordinate, transcribed from the
# arranged graph in Blender 4.5 -- 4.x draws the widest nodes, so a layout that
# reads cleanly there reads cleanly on 5.x too.  Absolute is what build() can
# use directly: while it runs, every frame is still at the origin, and Blender
# rebases each frame around its children on the first redraw, leaving them at
# FRAME_PAD from the frame's own corner.
FRAME_ORIGIN = {
    "Props": (-2182, 2965), "Coords": (-757, 3434),
    "Base": (-138, 2630), "Coverage": (-141, 270), "Detail": (-137, -450),
    "SkinCloth": (808, -93), "Segment": (813, -757), "Emissive": (-133, -2240),
    "Hair": (797, 2502), "Transition": (814, -2266), "Eye": (802, 2173),
    "Shade": (1767, 1177), "EyePath": (1952, 2484), "HairPath": (1903, 1554),
    "Out": (4375, 2301),
}
FRAME_PAD = (30, -39)       # offset a shrunk frame leaves its first child at

# The OUTPUT frame holds a different node set per build -- three seven-rung
# comparison ladders below 5.2, four compact Menu-Switch-fed mixes on 5.2 --
# so the two are arranged separately, each transcribed from its own version.
OUT_POS = {                 # <5.2 ladder build, arranged in 4.5
    "BloodOverride": (4513, 2261), "Principled BSDF": (5173, 1261),
    "Material Output": (5573, 1261), "EmissionSum": (4545, -342),
    "EmissionStrength": (4628, 43), "MetallicGate": (4405, 115),
    "MetallicModelGate": (4615, 221),
}
OUT_POS_MENU = {            # 5.2 Menu Switch build, arranged in 5.2
    "SM_BaseColor": (4425, 1925), "SM_Normal_Hair": (4559, 1455),
    "SM_Normal": (4758, 1447), "SM_Roughness": (4596, 1671),
    "BloodOverride": (4591, 1925), "Principled BSDF": (5173, 1261),
    "Material Output": (5573, 1261), "EmissionSum": (4649, 559),
    "EmissionStrength": (4819, 416), "MetallicGate": (4558, 1226),
    "MetallicModelGate": (4771, 1231),
}

# Separate Bundle nodes (5.x only) sit above the frame that consumes them;
# the OUTPUT one is hand-placed inside its own busier frame.
BUNDLE_SEPARATOR_DY = 300
BUNDLE_SEPARATOR_POS = {"Out": (4364, 1234)}

# Driven property columns, in the Props frame.
PROP_ROW_PITCH = 150
SWITCH_COLUMNS = ((-2151, 2925), (-1759, 2324))   # x, first y, per column
SWITCH_COLUMN_LEN = 40      # switches per column; a longer list runs the last
                            # column past its neighbour rather than losing rows
ENUM_COLUMN = (-1751, 2925)
FLOAT_COLUMN = (-1351, 2925)

# Coordinate frame, and the texture slots inside each TEXTURES frame.
UV_COLUMN = (-727, 3394)
UV_ROW_PITCH = 200
SLOT_ROW_PITCH = 380
SLOT_USE_DX = 292           # neutral/texture Mix, x offset from its image node


def _out_pos(name):
    """Where `name` goes in the OUTPUT frame, for the build being run."""
    return (OUT_POS_MENU if HAS_MANUAL_MENU_SWITCH else OUT_POS)[name]


# --------------------------------------------------------- menu abstraction --

# Ladder geometry.  A Mix node draws its data-type and blend-mode dropdowns
# whether or not the ladder uses them, so one rung measures roughly 140 x 220 --
# the original 200px row pitch and 200px column pitch were both SMALLER than the
# nodes they were spacing, which is why every ladder overlapped itself as well
# as the next menu down.  Keep MENU_ROW_PITCH above ~230 and the column offsets
# more than ~180 apart or the overlap comes straight back.
MENU_ROW_PITCH = 280
MENU_COL_VAL = 320
MENU_COL_EQ = 620
MENU_COL_MIX = 920
# Where each <5.2 fallback ladder starts; it grows right and down from there.
# The 5.2 Menu Switch build has no ladders at all, and lays its selectors out
# inside the OUTPUT frame instead -- see OUT_POS_MENU.
MENU_ANCHOR = {"SM_BaseColor": (7406, 4290),
               "SM_Normal": (7408, 2068),
               "SM_Roughness": (7411, 6237)}


def _menu(tree, name, loc, items, data_type="FLOAT", label="", parent=None,
          selector_src=None, manual=False):
    """One N-way selector.  Returns (out_node, selector_socket, value_sockets).

    With ``manual=True``, use a real Blender 5.2 Menu Switch whenever one is
    available.  Its default may be set through Python, but cannot have a
    live driver.  Other selectors use a real Menu Switch only once Blender
    makes that socket drivable; until then they retain the property-driven
    comparison ladder."""
    if HAS_MENU_SWITCH or (manual and HAS_MANUAL_MENU_SWITCH):
        n = _new(tree, MENU_SWITCH_IDNAME, name, loc, label or "Menu", parent)
        _prop(n, "data_type", data_type)
        try:
            n.enum_items.clear()
            for it in items:
                n.enum_items.new(it)
            # Replacing the default A/B labels clears Menu's selection.
            # Initialise manual menus to option zero; variant application
            # overwrites this with the imported shading model.
            n.inputs[0].default_value = items[0]
        except Exception:
            log("menu items failed on %s" % name)
        if selector_src is not None:
            selector_node, selector_output = selector_src
            _link(tree, selector_node, selector_output, n, 0)
        return n, n.inputs[0], list(n.inputs)[1:]

    holder = _frame(tree, name + "_frame",
                    (label or "Menu") + "   [<5.2 fallback ladder]", parent)
    caption = "Selector  " + " / ".join("%d=%s" % (i, s)
                                        for i, s in enumerate(items))
    if selector_src is not None:
        sel_node, sel_out = selector_src
        sel_socket = sel_node.outputs[sel_out]
        sel = None
    else:
        sel = _new(tree, "ShaderNodeValue", name + "_selector", loc, caption, holder)
        sel_socket = sel.outputs[0]
    values, prev = [], None
    for i, it in enumerate(items):
        v = _new(tree, "ShaderNodeMix", "%s_val%d" % (name, i),
                 (loc[0] + MENU_COL_VAL, loc[1] - i * MENU_ROW_PITCH),
                 "%d: %s" % (i, it), holder)
        _prop(v, "data_type", data_type)
        # v is used purely as a value HOLDER -- _menu_feed()/_menu_set() only
        # ever write to its "A" input, and every caller (e.g. the per-model
        # loops in build()) expects v's Result to equal whatever was put into
        # A. That only holds if Factor is pinned to 0 (same reasoning as the
        # identical `_sv(byp, "Factor", 0.0)` in _tex_slot()); left at the new
        # -node default of 0.5 -- with B also left at its default -- v's
        # Result silently blends toward B and option 0 in particular (which
        # skips the compare/select ladder below entirely and is returned
        # as-is when i == 0) comes out wrong for EVERY caller of this menu.
        _sv(v, "Factor", 0.0)
        values.append(v)
        if i == 0:
            prev = v
            continue
        c = _new(tree, "ShaderNodeMath", "%s_eq%d" % (name, i),
                 (loc[0] + MENU_COL_EQ, loc[1] - i * MENU_ROW_PITCH + 110),
                 "selector == %d" % i, holder)
        _prop(c, "operation", "COMPARE")
        _sv(c, 1, float(i))
        _sv(c, 2, 0.5)
        try:
            tree.links.new(sel_socket, c.inputs[0])
        except Exception:
            log('menu selector link failed')
        m = _new(tree, "ShaderNodeMix", "%s_mix%d" % (name, i),
                 (loc[0] + MENU_COL_MIX, loc[1] - i * MENU_ROW_PITCH),
                 "select: %s" % it, holder)
        _prop(m, "data_type", data_type)
        _link(tree, c, 0, m, "Factor")
        _link(tree, prev, "Result", m, "A")
        _link(tree, v, "Result", m, "B")
        prev = m
    return prev, sel_socket, [_in(v, "A") for v in values]


def _menu_out(node):
    # Manual Blender 5.2 menus intentionally exist even though HAS_MENU_SWITCH
    # is False (that flag means *drivable*).  Inspect the actual node rather
    # than the global capability flag, otherwise their Output socket is never
    # connected to the shader.
    return 0 if getattr(node, "bl_idname", "") == MENU_SWITCH_IDNAME else "Result"


def _menu_feed(tree, vals, index, source, out=0):
    if index >= len(vals) or vals[index] is None:
        log("menu option %d missing" % index)
        return
    try:
        tree.links.new(source.outputs[out], vals[index])
    except Exception:
        log("menu feed failed at option %d" % index)


def _menu_set(vals, index, value):
    if index >= len(vals) or vals[index] is None:
        return
    tgt = vals[index]
    try:
        dv = tgt.default_value
        if hasattr(dv, "__len__") and not isinstance(dv, str):
            n = len(dv)
            v = list(value) if hasattr(value, "__len__") else [value] * n
            tgt.default_value = (v + [1.0] * n)[:n]
        else:
            tgt.default_value = value
    except Exception:
        pass


# ============================================================ UTILITY GROUPS ==

def util_tbn():
    """Explicit tangent frame.  Blender's Tangent node gives T but no bitangent
    sign, so w = sign(dot(B_signed, cross(N,T))) recovers handedness."""
    def _b(g):
        _sock(g, "T", "OUTPUT", "NodeSocketVector")
        _sock(g, "B", "OUTPUT", "NodeSocketVector")
        _sock(g, "N", "OUTPUT", "NodeSocketVector")
        _sock(g, "w", "OUTPUT", "NodeSocketFloat")
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        tan = _new(g, "ShaderNodeTangent", "Tangent", (-620, 140), "UV tangent")
        _prop(tan, "direction_type", "UV_MAP")
        geo = _new(g, "ShaderNodeNewGeometry", "Geometry", (-620, -80))
        nmap = _new(g, "ShaderNodeNormalMap", "Signed B", (-620, -320),
                    "flat green map -> signed bitangent")
        _sv(nmap, "Strength", 1.0)
        _sv(nmap, "Color", (0.5, 1.0, 0.5, 1.0))
        cr = _new(g, "ShaderNodeVectorMath", "cross", (-380, -20), "cross(N, T)")
        _prop(cr, "operation", "CROSS_PRODUCT")
        dt = _new(g, "ShaderNodeVectorMath", "dot", (-180, -180),
                  "dot(B_signed, cross(N,T))")
        _prop(dt, "operation", "DOT_PRODUCT")
        sg = _new(g, "ShaderNodeMath", "sign", (20, -180), "w = +/-1")
        _prop(sg, "operation", "SIGN")
        bsc = _new(g, "ShaderNodeVectorMath", "B", (240, -20), "B = w*cross(N,T)")
        _prop(bsc, "operation", "SCALE")
        _link(g, geo, "Normal", cr, 0)
        _link(g, tan, 0, cr, 1)
        _link(g, nmap, 0, dt, 0)
        _link(g, cr, 0, dt, 1)
        _link(g, dt, "Value", sg, 0)
        _link(g, cr, 0, bsc, 0)
        _link(g, sg, 0, bsc, "Scale")
        _link(g, tan, 0, go, "T")
        _link(g, bsc, 0, go, "B")
        _link(g, geo, "Normal", go, "N")
        _link(g, sg, 0, go, "w")
    return _rebuild_group("FF7R Util/TBN", _b)


def util_unpack_bc5():
    """Loaded normal-map RGB -> a DirectX-authored tangent-space vector.

    Blender's DDS loader (including DDS files supplied by the CUE4Parse bridge)
    already exposes BC5 images with their blue/Z channel reconstructed.  Do not
    reconstruct Z a second time here: preserve the loader's full RGB result,
    remap it to -1..1, and only apply the DirectX green-channel convention.
    The tangent-space result is retained because the master blends several
    normal layers before its final tangent-to-world conversion."""
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "RG", "INPUT", "NodeSocketColor", (0.5, 0.5, 1.0, 1.0))
        _sock(g, "Flip Green", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-760, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        raw = _new(g, "ShaderNodeVectorMath", "rgb_signed", (-520, 120),
                   "loader-reconstructed RGB * 2 - 1")
        _prop(raw, "operation", "MULTIPLY_ADD")
        _sv(raw, 1, (2.0, 2.0, 2.0))
        _sv(raw, 2, (-1.0, -1.0, -1.0))
        flip = _new(g, "ShaderNodeVectorMath", "green_flip", (-300, -80),
                    "DirectX -> Blender: (x, -y, z)")
        _prop(flip, "operation", "MULTIPLY")
        _sv(flip, 1, (1.0, -1.0, 1.0))
        choose = _new(g, "ShaderNodeMix", "convention", (-60, 100),
                      "OpenGL / DirectX")
        _prop(choose, "data_type", "VECTOR")
        nrm = _new(g, "ShaderNodeVectorMath", "normalize", (220, 100))
        _prop(nrm, "operation", "NORMALIZE")
        _link(g, gi, "RG", raw, 0)
        _link(g, raw, 0, flip, 0)
        _link(g, gi, "Flip Green", choose, "Factor")
        _link(g, raw, 0, choose, "A")
        _link(g, flip, 0, choose, "B")
        _link(g, choose, "Result", nrm, 0)
        _link(g, nrm, 0, go, "Normal")
    return _rebuild_group("UE Unpack Normal (RG)", _b)


def util_unpack_rgb():
    """Decode a genuine three-channel tangent-space direction.

    Hair Material_Texture2D_14/WideBentNormal is BC1, not BC5. The hair base
    pass reads all three channels as normalize(rgb*2-1), transforms that vector
    through the UV0 tangent frame, and writes it to GBufferD. Reconstructing Z
    from RG (the old ubershader behavior) destroys authored negative Z values.
    The green flip is the Blender/DirectX convention bridge confirmed by the
    standalone hair reconstruction; it is not present as a separate operation
    in UE because UE's tangent basis already uses the DirectX convention."""
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "RGB", "INPUT", "NodeSocketColor", (0.5, 0.5, 1.0, 1.0))
        _sock(g, "Flip Green", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-760, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        raw = _new(g, "ShaderNodeVectorMath", "rgb_signed", (-520, 120),
                   "rgb * 2 - 1  (full RGB; no BC5 Z reconstruction)")
        _prop(raw, "operation", "MULTIPLY_ADD")
        _sv(raw, 1, (2.0, 2.0, 2.0))
        _sv(raw, 2, (-1.0, -1.0, -1.0))
        flip = _new(g, "ShaderNodeVectorMath", "green_flip", (-300, -80),
                    "DirectX -> Blender: (x, -y, z)")
        _prop(flip, "operation", "MULTIPLY")
        _sv(flip, 1, (1.0, -1.0, 1.0))
        choose = _new(g, "ShaderNodeMix", "convention", (-60, 100),
                      "OpenGL / DirectX")
        _prop(choose, "data_type", "VECTOR")
        norm = _new(g, "ShaderNodeVectorMath", "normalize", (220, 100))
        _prop(norm, "operation", "NORMALIZE")
        _link(g, gi, "RGB", raw, 0)
        _link(g, raw, 0, flip, 0)
        _link(g, gi, "Flip Green", choose, "Factor")
        _link(g, raw, 0, choose, "A")
        _link(g, flip, 0, choose, "B")
        _link(g, choose, "Result", norm, 0)
        _link(g, norm, 0, go, "Normal")
    return _rebuild_group("UE Unpack Normal (RGB)", _b)


def util_tangent_to_world(uv_map=UV_MAP):
    """Tangent-space vector -> world, through a mikktspace Normal Map node.

    From ff7r_hair_material.py.  UE generates ONE vertex tangent frame (from
    UV0) and uses it for every tangent-space vector regardless of which UV the
    texture was sampled on -- so UV0 is the shader-exact basis for slot 14 too,
    even though slot 14 is sampled on UV1. Named-UV variants are separate node
    groups so importing a mesh with another UV0 name cannot rewrite the basis
    underneath materials that already use this transform."""
    group_name = ("UE Tangent To World (mikktspace)" if not uv_map else
                  "UE Tangent To World (mikktspace, %s)" % uv_map)
    def _b(g):
        _sock(g, "Vector", "OUTPUT", "NodeSocketVector")
        _sock(g, "Vector", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        gi = _new(g, "NodeGroupInput", "Group Input", (-520, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (320, 0))
        enc = _new(g, "ShaderNodeVectorMath", "encode", (-300, 0),
                   "encode to 0..1")
        _prop(enc, "operation", "MULTIPLY_ADD")
        _sv(enc, 1, (0.5, 0.5, 0.5))
        _sv(enc, 2, (0.5, 0.5, 0.5))
        nm = _new(g, "ShaderNodeNormalMap", "TBN", (-80, 0),
                  "mikktspace TBN (%s basis)" % (uv_map or "active UV"))
        _prop(nm, "uv_map", uv_map)
        _sv(nm, "Strength", 1.0)
        _link(g, gi, "Vector", enc, 0)
        _link(g, enc, 0, nm, "Color")
        _link(g, nm, "Normal", go, "Vector")
    return _rebuild_group(group_name, _b)


def util_frame_to_world():
    """Explicit TBN transform: v -> T*v.x + B*v.y + N*v.z, normalised.
    Verbatim from ff7r_eye_material_v2.py -- the eye groups index its sockets
    positionally, so the socket ORDER here must not change."""
    def _b(g):
        _sock(g, "Vector", "OUTPUT", "NodeSocketVector")
        _sock(g, "T", "INPUT", "NodeSocketVector")
        _sock(g, "B", "INPUT", "NodeSocketVector")
        _sock(g, "N", "INPUT", "NodeSocketVector")
        _sock(g, "Vector", "INPUT", "NodeSocketVector")
        gi = _new(g, "NodeGroupInput", "Group Input", (-900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (900, 0))
        sep = _new(g, "ShaderNodeSeparateXYZ", "Separate XYZ", (-500, -220))
        a = _new(g, "ShaderNodeVectorMath", "Vector Math", (-280, 220), "T * v.x")
        _prop(a, "operation", "SCALE")
        b = _new(g, "ShaderNodeVectorMath", "Vector Math.001", (-280, 40), "B * v.y")
        _prop(b, "operation", "SCALE")
        c = _new(g, "ShaderNodeVectorMath", "Vector Math.002", (-280, -140), "N * v.z")
        _prop(c, "operation", "SCALE")
        d = _new(g, "ShaderNodeVectorMath", "Vector Math.003", (-60, 140))
        _prop(d, "operation", "ADD")
        e = _new(g, "ShaderNodeVectorMath", "Vector Math.004", (130, 60))
        _prop(e, "operation", "ADD")
        f = _new(g, "ShaderNodeVectorMath", "Vector Math.005", (330, 0))
        _prop(f, "operation", "NORMALIZE")
        _link(g, gi, 3, sep, 0)
        _link(g, gi, 0, a, 0)
        _link(g, sep, 0, a, 3)
        _link(g, gi, 1, b, 0)
        _link(g, sep, 1, b, 3)
        _link(g, gi, 2, c, 0)
        _link(g, sep, 2, c, 3)
        _link(g, a, 0, d, 0)
        _link(g, b, 0, d, 1)
        _link(g, d, 0, e, 0)
        _link(g, c, 0, e, 1)
        _link(g, e, 0, f, 0)
        _link(g, f, 0, go, 0)
        _fix_conventions(g)
    return _rebuild_group("UE Frame To World (explicit TBN)", _b)


def util_slerp():
    """Spherical normal blend -- what Detail_ AND PositiveTransitionNormal_ both
    do (PLAN tier 1, two independent shaders).  theta is clamped away from 0
    because the shader relied on GPU flush behaviour Blender does not
    reproduce; without the guard this NaNs in the common case (PLAN 2.8)."""
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "A", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "B", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "t", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Fast (lerp)", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-980, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (980, 0))
        d = _new(g, "ShaderNodeVectorMath", "dot", (-760, 260), "dot(A,B)")
        _prop(d, "operation", "DOT_PRODUCT")
        cl = _new(g, "ShaderNodeMath", "clampdot", (-580, 260), "min(dot, 0.9999)")
        _prop(cl, "operation", "MINIMUM")
        _sv(cl, 1, 0.9999)
        th = _new(g, "ShaderNodeMath", "theta", (-400, 260), "theta = acos(dot)")
        _prop(th, "operation", "ARCCOSINE")
        thg = _new(g, "ShaderNodeMath", "theta_guard", (-220, 260),
                   "max(theta, 1e-3)   <- PLAN 2.8 singularity guard")
        _prop(thg, "operation", "MAXIMUM")
        _sv(thg, 1, 0.001)
        omt = _new(g, "ShaderNodeMath", "one_minus_t", (-400, 60), "1 - t")
        _prop(omt, "operation", "SUBTRACT")
        _sv(omt, 0, 1.0)
        sa = _new(g, "ShaderNodeMath", "mulA", (-20, 160), "theta*(1-t)")
        _prop(sa, "operation", "MULTIPLY")
        sb = _new(g, "ShaderNodeMath", "mulB", (-20, -20), "theta*t")
        _prop(sb, "operation", "MULTIPLY")
        sna = _new(g, "ShaderNodeMath", "sinA", (160, 160), "sin")
        _prop(sna, "operation", "SINE")
        snb = _new(g, "ShaderNodeMath", "sinB", (160, -20), "sin")
        _prop(snb, "operation", "SINE")
        pa = _new(g, "ShaderNodeVectorMath", "scaleA", (360, 200),
                  "A * sin(theta(1-t))")
        _prop(pa, "operation", "SCALE")
        pb = _new(g, "ShaderNodeVectorMath", "scaleB", (360, 20),
                  "B * sin(theta t)")
        _prop(pb, "operation", "SCALE")
        ad = _new(g, "ShaderNodeVectorMath", "add", (560, 120))
        _prop(ad, "operation", "ADD")
        nz = _new(g, "ShaderNodeVectorMath", "normalize", (720, 120))
        _prop(nz, "operation", "NORMALIZE")
        lp = _new(g, "ShaderNodeMix", "lerp", (560, -260), "fast path: plain mix")
        _prop(lp, "data_type", "VECTOR")
        lpn = _new(g, "ShaderNodeVectorMath", "lerp_norm", (720, -260))
        _prop(lpn, "operation", "NORMALIZE")
        sel = _new(g, "ShaderNodeMix", "select", (860, -60), "slerp / fast")
        _prop(sel, "data_type", "VECTOR")
        _link(g, gi, "A", d, 0)
        _link(g, gi, "B", d, 1)
        _link(g, d, "Value", cl, 0)
        _link(g, cl, 0, th, 0)
        _link(g, th, 0, thg, 0)
        _link(g, gi, "t", omt, 1)
        _link(g, thg, 0, sa, 0)
        _link(g, omt, 0, sa, 1)
        _link(g, thg, 0, sb, 0)
        _link(g, gi, "t", sb, 1)
        _link(g, sa, 0, sna, 0)
        _link(g, sb, 0, snb, 0)
        _link(g, gi, "A", pa, 0)
        _link(g, sna, 0, pa, "Scale")
        _link(g, gi, "B", pb, 0)
        _link(g, snb, 0, pb, "Scale")
        _link(g, pa, 0, ad, 0)
        _link(g, pb, 0, ad, 1)
        _link(g, ad, 0, nz, 0)
        _link(g, gi, "t", lp, "Factor")
        _link(g, gi, "A", lp, "A")
        _link(g, gi, "B", lp, "B")
        _link(g, lp, "Result", lpn, 0)
        _link(g, gi, "Fast (lerp)", sel, "Factor")
        _link(g, nz, 0, sel, "A")
        _link(g, lpn, 0, sel, "B")
        _link(g, sel, "Result", go, "Normal")
    return _rebuild_group("FF7R Util/Normal Slerp", _b)


def util_rnm():
    """Reoriented normal mapping used by the MEC/environment detail path.

    This is the algebra in M_Tile_Rock_Granite_06A_MainPS_19457e58.txt,
    decompiler lines 493-511, after its screen-derivative basis alignment:

        t = base + (0, 0, 1)
        u = (-detail.x, -detail.y, detail.z)
        r = normalize(t * dot(t, u) - u * t.z)

    The shader's derivative work maps independently sampled normals into one
    tangent frame. Blender's Normal Map/TBN stage provides that common frame,
    so the portable node group starts at the actual RNM operation. Strength
    scales the detail away by blending it toward the flat tangent normal first.
    """
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Base", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Detail", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Strength", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1100, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1050, 0))
        dmix = _new(g, "ShaderNodeMix", "detail_strength", (-880, -220),
                    "flat -> detail")
        _prop(dmix, "data_type", "VECTOR")
        _sv(dmix, "A", (0.0, 0.0, 1.0))
        dnorm = _new(g, "ShaderNodeVectorMath", "detail_normalize", (-660, -220))
        _prop(dnorm, "operation", "NORMALIZE")
        t = _new(g, "ShaderNodeVectorMath", "t", (-660, 220),
                 "t = base + (0,0,1)")
        _prop(t, "operation", "ADD")
        _sv(t, 1, (0.0, 0.0, 1.0))
        dsep = _new(g, "ShaderNodeSeparateXYZ", "detail_xyz", (-460, -220))
        nx = _new(g, "ShaderNodeMath", "neg_x", (-260, -100))
        _prop(nx, "operation", "MULTIPLY")
        _sv(nx, 1, -1.0)
        ny = _new(g, "ShaderNodeMath", "neg_y", (-260, -260))
        _prop(ny, "operation", "MULTIPLY")
        _sv(ny, 1, -1.0)
        u = _new(g, "ShaderNodeCombineXYZ", "u", (-40, -180),
                 "u = (-detail.x,-detail.y,detail.z)")
        dot = _new(g, "ShaderNodeVectorMath", "dot_tu", (180, 180), "dot(t,u)")
        _prop(dot, "operation", "DOT_PRODUCT")
        tdot = _new(g, "ShaderNodeVectorMath", "t_dot", (400, 180),
                    "t * dot(t,u)")
        _prop(tdot, "operation", "SCALE")
        tsep = _new(g, "ShaderNodeSeparateXYZ", "t_xyz", (180, -100))
        utz = _new(g, "ShaderNodeVectorMath", "u_tz", (400, -100), "u * t.z")
        _prop(utz, "operation", "SCALE")
        sub = _new(g, "ShaderNodeVectorMath", "r", (640, 80),
                   "t*dot(t,u) - u*t.z")
        _prop(sub, "operation", "SUBTRACT")
        norm = _new(g, "ShaderNodeVectorMath", "normalize", (840, 80))
        _prop(norm, "operation", "NORMALIZE")
        _link(g, gi, "Strength", dmix, "Factor")
        _link(g, gi, "Detail", dmix, "B")
        _link(g, dmix, "Result", dnorm, 0)
        _link(g, gi, "Base", t, 0)
        _link(g, dnorm, 0, dsep, 0)
        _link(g, dsep, "X", nx, 0)
        _link(g, dsep, "Y", ny, 0)
        _link(g, nx, 0, u, "X")
        _link(g, ny, 0, u, "Y")
        _link(g, dsep, "Z", u, "Z")
        _link(g, t, 0, dot, 0)
        _link(g, u, 0, dot, 1)
        _link(g, t, 0, tdot, 0)
        _link(g, dot, "Value", tdot, "Scale")
        _link(g, t, 0, tsep, 0)
        _link(g, u, 0, utz, 0)
        _link(g, tsep, "Z", utz, "Scale")
        _link(g, tdot, 0, sub, 0)
        _link(g, utz, 0, sub, 1)
        _link(g, sub, 0, norm, 0)
        _link(g, norm, 0, go, "Normal")
    return _rebuild_group("FF7R Util/RNM", _b)


def util_ao_combine():
    """m=l1*l2; n=min(l1,l2); t=m+1-n; AO=(1-t^5)^5*(m-n)+n   (PLAN tier 1).

    Identical to the hair script's "UE SoftMin Occlusion" -- expanding both
    forms gives the same expression, an independent confirmation of the decode.
    Kept as one group rather than two doing the same thing."""
    def _b(g):
        _sock(g, "AO", "OUTPUT", "NodeSocketFloat")
        _sock(g, "L1", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "L2", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-760, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (800, 0))
        m = _new(g, "ShaderNodeMath", "m", (-540, 140), "m = l1*l2")
        _prop(m, "operation", "MULTIPLY")
        n = _new(g, "ShaderNodeMath", "n", (-540, -80), "n = min(l1,l2)")
        _prop(n, "operation", "MINIMUM")
        t1 = _new(g, "ShaderNodeMath", "t1", (-360, 80), "m + 1")
        _prop(t1, "operation", "ADD")
        _sv(t1, 1, 1.0)
        t = _new(g, "ShaderNodeMath", "t", (-200, 80), "t = m+1-n")
        _prop(t, "operation", "SUBTRACT")
        p5 = _new(g, "ShaderNodeMath", "t5", (-40, 80), "t^5")
        _prop(p5, "operation", "POWER")
        _sv(p5, 1, 5.0)
        om = _new(g, "ShaderNodeMath", "om", (120, 80), "1 - t^5")
        _prop(om, "operation", "SUBTRACT")
        _sv(om, 0, 1.0)
        q5 = _new(g, "ShaderNodeMath", "q5", (280, 80), "(1-t^5)^5")
        _prop(q5, "operation", "POWER")
        _sv(q5, 1, 5.0)
        df = _new(g, "ShaderNodeMath", "diff", (280, -160), "m - n")
        _prop(df, "operation", "SUBTRACT")
        mu = _new(g, "ShaderNodeMath", "mul", (460, -40))
        _prop(mu, "operation", "MULTIPLY")
        adn = _new(g, "ShaderNodeMath", "add_n", (620, -40), "+ n")
        _prop(adn, "operation", "ADD")
        _prop(adn, "use_clamp", True)
        _link(g, gi, "L1", m, 0)
        _link(g, gi, "L2", m, 1)
        _link(g, gi, "L1", n, 0)
        _link(g, gi, "L2", n, 1)
        _link(g, m, 0, t1, 0)
        _link(g, t1, 0, t, 0)
        _link(g, n, 0, t, 1)
        _link(g, t, 0, p5, 0)
        _link(g, p5, 0, om, 1)
        _link(g, om, 0, q5, 0)
        _link(g, m, 0, df, 0)
        _link(g, n, 0, df, 1)
        _link(g, q5, 0, mu, 0)
        _link(g, df, 0, mu, 1)
        _link(g, mu, 0, adn, 0)
        _link(g, n, 0, adn, 1)
        _link(g, adn, 0, go, "AO")
    return _rebuild_group("FF7R Util/AO Combine", _b)


def util_rough_combine():
    """sqrt(saturate(r1^2 + r2^2))   (PLAN tier 1)"""
    def _b(g):
        _sock(g, "Roughness", "OUTPUT", "NodeSocketFloat")
        _sock(g, "R1", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        _sock(g, "R2", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-560, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (560, 0))
        a = _new(g, "ShaderNodeMath", "r1sq", (-340, 100), "r1^2")
        _prop(a, "operation", "MULTIPLY")
        b = _new(g, "ShaderNodeMath", "r2sq", (-340, -100), "r2^2")
        _prop(b, "operation", "MULTIPLY")
        s = _new(g, "ShaderNodeMath", "sum", (-120, 0), "saturate(sum)")
        _prop(s, "operation", "ADD")
        _prop(s, "use_clamp", True)
        q = _new(g, "ShaderNodeMath", "sqrt", (80, 0), "sqrt")
        _prop(q, "operation", "SQRT")
        _link(g, gi, "R1", a, 0)
        _link(g, gi, "R1", a, 1)
        _link(g, gi, "R2", b, 0)
        _link(g, gi, "R2", b, 1)
        _link(g, a, 0, s, 0)
        _link(g, b, 0, s, 1)
        _link(g, s, 0, q, 0)
        _link(g, q, 0, go, "Roughness")
    return _rebuild_group("FF7R Util/Roughness Combine", _b)


def util_f0():
    """GBufferA.a -> dielectric F0 -> Blender 'Specular IOR Level' (PLAN 2.11).

        F0 = clamp(x < 0.666667 ? x*0.06 : x*0.36 - 0.2, 0, 0.16)
        level = F0 / 0.08              (Blender: level 0.5 <-> F0 0.04)

    The /0.08 divisor is the measured one from the hair script, which arrived at
    0.046520 -> 0.5815.  An earlier draft of this file used /0.16 and was wrong."""
    def _b(g):
        _sock(g, "Specular IOR Level", "OUTPUT", "NodeSocketFloat")
        _sock(g, "F0", "OUTPUT", "NodeSocketFloat")
        _sock(g, "GBufferA.a", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-660, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        lo = _new(g, "ShaderNodeMath", "lo", (-440, 160), "x * 0.06")
        _prop(lo, "operation", "MULTIPLY")
        _sv(lo, 1, F0_LO)
        hi = _new(g, "ShaderNodeMath", "hi", (-440, -20), "x * 0.36 - 0.2")
        _prop(hi, "operation", "MULTIPLY_ADD")
        _sv(hi, 1, F0_HI)
        _sv(hi, 2, -F0_HI_BIAS)
        cm = _new(g, "ShaderNodeMath", "knee", (-440, -220), "x >= 0.666667")
        _prop(cm, "operation", "GREATER_THAN")
        _sv(cm, 1, F0_KNEE)
        mx = _new(g, "ShaderNodeMix", "pick", (-220, 0), "piecewise")
        _prop(mx, "data_type", "FLOAT")
        cl = _new(g, "ShaderNodeMath", "clamp", (-20, 0), "clamp to 0.16")
        _prop(cl, "operation", "MINIMUM")
        _sv(cl, 1, F0_CLAMP)
        _prop(cl, "use_clamp", True)
        nz = _new(g, "ShaderNodeMath", "level", (200, 80), "F0 / 0.08 -> level")
        _prop(nz, "operation", "DIVIDE")
        _sv(nz, 1, SPEC_LEVEL_SCALE)
        _link(g, gi, "GBufferA.a", lo, 0)
        _link(g, gi, "GBufferA.a", hi, 0)
        _link(g, gi, "GBufferA.a", cm, 0)
        _link(g, cm, 0, mx, "Factor")
        _link(g, lo, 0, mx, "A")
        _link(g, hi, 0, mx, "B")
        _link(g, mx, "Result", cl, 0)
        _link(g, cl, 0, nz, 0)
        _link(g, nz, 0, go, "Specular IOR Level")
        _link(g, cl, 0, go, "F0")
    return _rebuild_group("FF7R Util/F0 Remap", _b)


# ================================================================ EYE PATH ===
# Ported from ff7r_eye_material_v2.py.  These four groups are self-contained:
# further eye tweaks should only need edits inside them.

def eye_disc():
    """uvC = (UV - centre)/radius; sclera mask = smoothstep(0.9, 1.1, |uvC|)."""
    def _b(g):
        _sock(g, "UV", "OUTPUT", "NodeSocketVector")
        _sock(g, "uvC", "OUTPUT", "NodeSocketVector")
        _sock(g, "Sclera Mask", "OUTPUT", "NodeSocketFloat")
        _sock(g, "UV0", "INPUT", "NodeSocketVector")
        _sock(g, "Mask Inner", "INPUT", "NodeSocketFloat", MASK_INNER, 0.0, 2.0)
        _sock(g, "Mask Outer", "INPUT", "NodeSocketFloat", MASK_OUTER, 0.0, 2.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-900, -220))
        go = _new(g, "NodeGroupOutput", "Group Output", (700, 0))
        vc = _new(g, "ShaderNodeVectorMath", "uvC", (-460, 0),
                  "uvC = (UV - centre) / radius")
        _prop(vc, "operation", "MULTIPLY_ADD")
        _sv(vc, 1, (_INV, _INV, 0.0))
        _sv(vc, 2, (-EYE_UV_CENTER[0] * _INV, -EYE_UV_CENTER[1] * _INV, 0.0))
        ln = _new(g, "ShaderNodeVectorMath", "r", (-220, 120), "r = |uvC|")
        _prop(ln, "operation", "LENGTH")
        mr = _new(g, "ShaderNodeMapRange", "mask", (20, 180),
                  "sclera mask = smoothstep(inner, outer, r)")
        _prop(mr, "interpolation_type", "SMOOTHSTEP")
        _sv(mr, 3, 0.0)
        _sv(mr, 4, 1.0)
        _link(g, gi, "UV0", vc, 0)
        _link(g, vc, 0, ln, 0)
        _link(g, ln, 1, mr, 0)
        _link(g, gi, "Mask Inner", mr, 1)
        _link(g, gi, "Mask Outer", mr, 2)
        _link(g, gi, "UV0", go, "UV")
        _link(g, vc, 0, go, "uvC")
        _link(g, mr, 0, go, "Sclera Mask")
    return _rebuild_group("FF7R Eye Disc", _b)


def eye_limbal_directional():
    """UE Limbal Occlusion (directional) -- verbatim shader maths from the eye
    capture.  Do not hand-edit; socket order is indexed positionally."""
    def _b(g):
        _sock(g, "Occlusion", "OUTPUT", "NodeSocketFloat")
        _sock(g, "uvC2", "INPUT", "NodeSocketVector")
        _sock(g, "Light Dir", "INPUT", "NodeSocketVector")
        _sock(g, "T2", "INPUT", "NodeSocketVector")
        _sock(g, "B2", "INPUT", "NodeSocketVector")
        _sock(g, "r", "INPUT", "NodeSocketFloat")
        _sock(g, "Offset", "INPUT", "NodeSocketFloat", LIMBAL_OFFSET)
        _sock(g, "Softness", "INPUT", "NodeSocketFloat", LIMBAL_SOFTNESS)
        _sock(g, "Aperture", "INPUT", "NodeSocketFloat", LIMBAL_APERTURE)
        _sock(g, "Normalize", "INPUT", "NodeSocketFloat", 0.5)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1900, 0))

        def vm(nm, loc, op, label=""):
            n = _new(g, "ShaderNodeVectorMath", nm, loc, label)
            _prop(n, "operation", op)
            return n

        def fm(nm, loc, op, label="", clamp=False):
            n = _new(g, "ShaderNodeMath", nm, loc, label)
            _prop(n, "operation", op)
            _prop(n, "use_clamp", clamp)
            return n

        v0 = vm("Vector Math", (-1000, 380), "DOT_PRODUCT", "Lu = dot(L, T2)")
        v1 = vm("Vector Math.001", (-1000, 240), "DOT_PRODUCT", "Lv = dot(L, B2)")
        m0 = fm("Math", (-820, 420), "MULTIPLY")
        m1 = fm("Math.001", (-820, 300), "MULTIPLY")
        m2 = fm("Math.002", (-640, 360), "ADD", "|Lproj|^2")
        sp = _new(g, "ShaderNodeSeparateXYZ", "Separate XYZ", (-1000, 60))
        m3 = fm("Math.003", (-820, 120), "MULTIPLY")
        m4 = fm("Math.004", (-820, 0), "MULTIPLY")
        m5 = fm("Math.005", (-640, 60), "ADD")
        m6 = fm("Math.006", (-460, 60), "DIVIDE", "Lx = radial component")
        m7 = fm("Math.007", (-640, -120), "MULTIPLY", "d2 = r*0.5")
        m8 = fm("Math.008", (-640, -240), "SQRT", "|Lproj|")
        m9 = fm("Math.009", (-460, -160), "MULTIPLY")
        m10 = fm("Math.010", (-280, -160), "MULTIPLY")
        m11 = fm("Math.011", (-100, -160), "SUBTRACT", "1 - sat(...)", True)
        _sv(m11, 0, 1.0)
        m12 = fm("Math.012", (80, -160), "MAXIMUM", "aperture")
        _sv(m12, 1, 0.01)
        m13 = fm("Math.013", (-460, 240), "MULTIPLY")
        m14 = fm("Math.014", (-280, 300), "MULTIPLY")
        m15 = fm("Math.015", (-280, 160), "MULTIPLY")
        m16 = fm("Math.016", (-100, 160), "MULTIPLY")
        m17 = fm("Math.017", (80, 160), "MULTIPLY")
        _sv(m17, 1, 2.0)
        m18 = fm("Math.018", (-280, 20), "MULTIPLY")
        m19 = fm("Math.019", (260, 240), "ADD")
        m20 = fm("Math.020", (440, 240), "ADD")
        m21 = fm("Math.021", (600, 240), "MAXIMUM")
        _sv(m21, 1, 0.0)
        m22 = fm("Math.022", (760, 240), "SQRT", "|offvec|")
        m23 = fm("Math.023", (260, 60), "MULTIPLY")
        m24 = fm("Math.024", (440, 60), "SUBTRACT",
                 "sat(aperture - softness*|offvec|)", True)
        m25 = fm("Math.025", (260, -160), "DIVIDE")
        _sv(m25, 0, 1.0)
        m26 = fm("Math.026", (440, -160), "MAXIMUM")
        _sv(m26, 0, 1.0)
        m27 = fm("Math.027", (440, -60), "DIVIDE")
        m28 = fm("Math.028", (600, -60), "MINIMUM")
        _sv(m28, 1, 1.0)
        m29 = fm("Math.029", (760, -100), "MULTIPLY", "normalised form")
        mix = _new(g, "ShaderNodeMix", "Mix", (760, 60), "raw <-> normalised")
        _prop(mix, "data_type", "FLOAT")
        m30 = fm("Math.030", (900, -100), "MULTIPLY", "f^2, clamped", True)

        _link(g, gi, 1, v0, 0)
        _link(g, gi, 2, v0, 1)
        _link(g, gi, 1, v1, 0)
        _link(g, gi, 3, v1, 1)
        _link(g, v0, 1, m0, 0)
        _link(g, v0, 1, m0, 1)
        _link(g, v1, 1, m1, 0)
        _link(g, v1, 1, m1, 1)
        _link(g, m0, 0, m2, 0)
        _link(g, m1, 0, m2, 1)
        _link(g, gi, 0, sp, 0)
        _link(g, v0, 1, m3, 0)
        _link(g, sp, 0, m3, 1)
        _link(g, v1, 1, m4, 0)
        _link(g, sp, 1, m4, 1)
        _link(g, m3, 0, m5, 0)
        _link(g, m4, 0, m5, 1)
        _link(g, m5, 0, m6, 0)
        _link(g, gi, 4, m6, 1)
        _link(g, gi, 4, m7, 0)
        _link(g, m2, 0, m8, 0)
        _link(g, m7, 0, m9, 0)
        _link(g, gi, 7, m9, 1)
        _link(g, m9, 0, m10, 0)
        _link(g, m8, 0, m10, 1)
        _link(g, m10, 0, m11, 1)
        _link(g, m11, 0, m12, 0)
        _link(g, gi, 5, m13, 0)
        _link(g, gi, 5, m13, 1)
        _link(g, m13, 0, m14, 0)
        _link(g, m2, 0, m14, 1)
        _link(g, m6, 0, m15, 0)
        _link(g, gi, 4, m15, 1)
        _link(g, m15, 0, m16, 0)
        _link(g, gi, 5, m16, 1)
        _link(g, m16, 0, m17, 0)
        _link(g, gi, 4, m18, 0)
        _link(g, gi, 4, m18, 1)
        _link(g, m14, 0, m19, 0)
        _link(g, m17, 0, m19, 1)
        _link(g, m19, 0, m20, 0)
        _link(g, m18, 0, m20, 1)
        _link(g, m20, 0, m21, 0)
        _link(g, m21, 0, m22, 0)
        _link(g, m22, 0, m23, 0)
        _link(g, gi, 6, m23, 1)
        _link(g, m12, 0, m24, 0)
        _link(g, m23, 0, m24, 1)
        _link(g, m12, 0, m25, 1)
        _link(g, m25, 0, m26, 1)
        _link(g, m24, 0, m27, 0)
        _link(g, m12, 0, m27, 1)
        _link(g, m27, 0, m28, 0)
        _link(g, m26, 0, m29, 0)
        _link(g, m28, 0, m29, 1)
        _link(g, gi, 8, mix, 0)
        _link(g, m24, 0, mix, 2)
        _link(g, m29, 0, mix, 3)
        _link(g, mix, 0, m30, 0)
        _link(g, mix, 0, m30, 1)
        _link(g, m30, 0, go, 0)
        _fix_conventions(g)
    return _rebuild_group("UE Limbal Occlusion (directional)", _b)


def eye_cornea():
    """Cornea dome + view-side refraction parallax + iris UV.
    Cornea IOR 1.49 is triangulated three independent ways (PLAN tier 1)."""
    def _b(g):
        _sock(g, "Cornea Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "uvC2", "OUTPUT", "NodeSocketVector")
        _sock(g, "r", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Iris UV", "OUTPUT", "NodeSocketVector")
        _sock(g, "N8", "INPUT", "NodeSocketVector")
        _sock(g, "Incoming", "INPUT", "NodeSocketVector")
        _sock(g, "T2", "INPUT", "NodeSocketVector")
        _sock(g, "B2", "INPUT", "NodeSocketVector")
        _sock(g, "UV", "INPUT", "NodeSocketVector")
        _sock(g, "uvC", "INPUT", "NodeSocketVector")
        _sock(g, "Slope", "INPUT", "NodeSocketFloat", CORNEA_SLOPE, 0.0, 4.0)
        _sock(g, "Eta", "INPUT", "NodeSocketFloat", ETA, 0.0, 2.0)
        _sock(g, "Depth Const", "INPUT", "NodeSocketFloat", DEPTH_CONST, 0.0, 2.0)
        _sock(g, "Depth Falloff", "INPUT", "NodeSocketFloat", DEPTH_FALLOFF, 0.0, 2.0)
        _sock(g, "Depth Scale", "INPUT", "NodeSocketFloat", DEPTH_SCALE, 0.0, 2.0)
        _sock(g, "Pupil Dilation", "INPUT", "NodeSocketFloat", 1.0, 0.05, 4.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1900, 0))

        dome = _new(g, "ShaderNodeVectorMath", "dome", (-634, 228),
                    "dome = (uvC.xy * slope, 1)")
        _prop(dome, "operation", "MULTIPLY_ADD")
        _sv(dome, 2, (0.0, 0.0, 1.0))
        slope3 = _new(g, "ShaderNodeCombineXYZ", "slope3", (-820, 100),
                      "(slope, slope, 0)")
        r2 = _new(g, "ShaderNodeVectorMath", "r2", (-624, 407), "r^2")
        _prop(r2, "operation", "DOT_PRODUCT")
        f2w = _grp(g, "UE Frame To World (explicit TBN)", "Group.003", (38, -89),
                   "cornea normal -> world")
        neg = _new(g, "ShaderNodeVectorMath", "I", (8, 262), "I = -Incoming")
        _prop(neg, "operation", "SCALE")
        _sv(neg, 3, -1.0)
        refr = _new(g, "ShaderNodeVectorMath", "refract", (213, 43),
                    "refract(I, cornea, eta)")
        _prop(refr, "operation", "REFRACT")
        dT = _new(g, "ShaderNodeVectorMath", "dotT", (415, 146), "dot(refr, T2)")
        _prop(dT, "operation", "DOT_PRODUCT")
        dB = _new(g, "ShaderNodeVectorMath", "dotB", (415, 6), "dot(refr, B2)")
        _prop(dB, "operation", "DOT_PRODUCT")
        dN = _new(g, "ShaderNodeVectorMath", "dotN", (415, -162), "dot(refr, N8)")
        _prop(dN, "operation", "DOT_PRODUCT")
        dep = _new(g, "ShaderNodeMath", "depth", (245, 231),
                   "const - falloff*r^2")
        _prop(dep, "operation", "MULTIPLY_ADD")
        negf = _new(g, "ShaderNodeMath", "negfalloff", (60, 231), "-falloff")
        _prop(negf, "operation", "MULTIPLY")
        _sv(negf, 1, -1.0)
        dmax = _new(g, "ShaderNodeMath", "dmax", (422, 318), "max(0, ...)")
        _prop(dmax, "operation", "MAXIMUM")
        _sv(dmax, 1, 0.0)
        dsc = _new(g, "ShaderNodeMath", "dscale", (592, 317), "iris depth")
        _prop(dsc, "operation", "MULTIPLY")
        px = _new(g, "ShaderNodeMath", "px", (599, 108), "refr.x / refr.z")
        _prop(px, "operation", "DIVIDE")
        py = _new(g, "ShaderNodeMath", "py", (611, -123), "refr.y / refr.z")
        _prop(py, "operation", "DIVIDE")
        ox = _new(g, "ShaderNodeMath", "ox", (799, 108))
        _prop(ox, "operation", "MULTIPLY")
        oy = _new(g, "ShaderNodeMath", "oy", (799, -52))
        _prop(oy, "operation", "MULTIPLY")
        off = _new(g, "ShaderNodeCombineXYZ", "offset", (977, 21),
                   "parallax offset")
        uvr = _new(g, "ShaderNodeVectorMath", "uv_refr", (46, -501),
                   "uv_refr = UV0 - offset")
        _prop(uvr, "operation", "SUBTRACT")
        uvc2 = _new(g, "ShaderNodeVectorMath", "uvC2", (198, -501),
                    "uvC2 = (uv_refr - centre) / radius")
        _prop(uvc2, "operation", "MULTIPLY_ADD")
        _sv(uvc2, 1, (_INV, _INV, 0.0))
        _sv(uvc2, 2, (-EYE_UV_CENTER[0] * _INV, -EYE_UV_CENTER[1] * _INV, 0.0))
        ln2 = _new(g, "ShaderNodeVectorMath", "len2", (357, -553), "|uvC2|")
        _prop(ln2, "operation", "LENGTH")
        cl2 = _new(g, "ShaderNodeMath", "clamp2", (537, -553), "min(|uvC2|, 1)")
        _prop(cl2, "operation", "MINIMUM")
        _sv(cl2, 1, 1.0)
        pw = _new(g, "ShaderNodeMath", "pow", (897, -353), "r' = pow(r, k)")
        _prop(pw, "operation", "POWER")
        nz2 = _new(g, "ShaderNodeVectorMath", "dir", (357, -681),
                   "normalize(uvC2)")
        _prop(nz2, "operation", "NORMALIZE")
        sc2 = _new(g, "ShaderNodeVectorMath", "scaled", (1046, -635))
        _prop(sc2, "operation", "SCALE")
        iuv = _new(g, "ShaderNodeVectorMath", "iris_uv", (1226, -635),
                   "iris UV = dir*len*0.5 + 0.5")
        _prop(iuv, "operation", "MULTIPLY_ADD")
        _sv(iuv, 1, (0.5, 0.5, 0.0))
        _sv(iuv, 2, (0.5, 0.5, 0.0))

        _link(g, gi, "Slope", slope3, 0)
        _link(g, gi, "Slope", slope3, 1)
        _link(g, slope3, 0, dome, 1)
        _link(g, gi, "uvC", dome, 0)
        _link(g, gi, "uvC", r2, 0)
        _link(g, gi, "uvC", r2, 1)
        _link(g, gi, "T2", f2w, 0)
        _link(g, gi, "B2", f2w, 1)
        _link(g, gi, "N8", f2w, 2)
        _link(g, dome, 0, f2w, 3)
        _link(g, gi, "Incoming", neg, 0)
        _link(g, neg, 0, refr, 0)
        _link(g, f2w, 0, refr, 1)
        _link(g, gi, "Eta", refr, 3)
        _link(g, refr, 0, dT, 0)
        _link(g, gi, "T2", dT, 1)
        _link(g, refr, 0, dB, 0)
        _link(g, gi, "B2", dB, 1)
        _link(g, refr, 0, dN, 0)
        _link(g, gi, "N8", dN, 1)
        _link(g, gi, "Depth Falloff", negf, 0)
        _link(g, r2, 1, dep, 0)
        _link(g, negf, 0, dep, 1)
        _link(g, gi, "Depth Const", dep, 2)
        _link(g, dep, 0, dmax, 0)
        _link(g, dmax, 0, dsc, 0)
        _link(g, gi, "Depth Scale", dsc, 1)
        _link(g, dT, 1, px, 0)
        _link(g, dN, 1, px, 1)
        _link(g, dB, 1, py, 0)
        _link(g, dN, 1, py, 1)
        _link(g, px, 0, ox, 0)
        _link(g, dsc, 0, ox, 1)
        _link(g, py, 0, oy, 0)
        _link(g, dsc, 0, oy, 1)
        _link(g, ox, 0, off, 0)
        _link(g, oy, 0, off, 1)
        _link(g, gi, "UV", uvr, 0)
        _link(g, off, 0, uvr, 1)
        _link(g, uvr, 0, uvc2, 0)
        _link(g, uvc2, 0, ln2, 0)
        _link(g, ln2, 1, cl2, 0)
        _link(g, cl2, 0, pw, 0)
        _link(g, gi, "Pupil Dilation", pw, 1)
        _link(g, uvc2, 0, nz2, 0)
        _link(g, nz2, 0, sc2, 0)
        _link(g, pw, 0, sc2, 3)
        _link(g, sc2, 0, iuv, 0)
        _link(g, f2w, 0, go, "Cornea Normal")
        _link(g, uvc2, 0, go, "uvC2")
        _link(g, ln2, 1, go, "r")
        _link(g, iuv, 0, go, "Iris UV")
        _fix_conventions(g)
    return _rebuild_group("FF7R Cornea", _b)


def eye_limbal():
    """Limbal occlusion: radial term blended with the directional term, applied
    to the iris only (the sclera stays at 1.0)."""
    def _b(g):
        _sock(g, "Occlusion", "OUTPUT", "NodeSocketFloat")
        _sock(g, "r", "INPUT", "NodeSocketFloat")
        _sock(g, "Sclera Mask", "INPUT", "NodeSocketFloat")
        _sock(g, "N8", "INPUT", "NodeSocketVector")
        _sock(g, "uvC2", "INPUT", "NodeSocketVector")
        _sock(g, "T2", "INPUT", "NodeSocketVector")
        _sock(g, "B2", "INPUT", "NodeSocketVector")
        _sock(g, "Light Position", "INPUT", "NodeSocketVector")
        _sock(g, "Shading Point", "INPUT", "NodeSocketVector")
        _sock(g, "Eta", "INPUT", "NodeSocketFloat", ETA, 0.0, 2.0)
        _sock(g, "Offset", "INPUT", "NodeSocketFloat", LIMBAL_OFFSET, 0.0, 2.0)
        _sock(g, "Softness", "INPUT", "NodeSocketFloat", LIMBAL_SOFTNESS, 0.0, 2.0)
        _sock(g, "Aperture", "INPUT", "NodeSocketFloat", LIMBAL_APERTURE, 0.0, 8.0)
        _sock(g, "Amount", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Directional", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1900, 0))
        rad = _new(g, "ShaderNodeMath", "radial", (111, 284),
                   "1 - 0.55*|uvC2|")
        _prop(rad, "operation", "MULTIPLY_ADD")
        _prop(rad, "use_clamp", True)
        _sv(rad, 2, 1.0)
        negs = _new(g, "ShaderNodeMath", "negsoft", (-70, 284), "-softness")
        _prop(negs, "operation", "MULTIPLY")
        _sv(negs, 1, -1.0)
        sq = _new(g, "ShaderNodeMath", "squared", (269, 274), "squared")
        _prop(sq, "operation", "MULTIPLY")
        iris_only = _new(g, "ShaderNodeMix", "iris_only", (628, 396),
                         "iris only -- sclera stays 1.0")
        _prop(iris_only, "data_type", "FLOAT")
        _sv(iris_only, 3, 1.0)
        amt = _new(g, "ShaderNodeMix", "amount", (793, 396),
                   "limbal occlusion amount")
        _prop(amt, "data_type", "FLOAT")
        _sv(amt, 2, 1.0)
        lsub = _new(g, "ShaderNodeVectorMath", "L", (-675, -139),
                    "L = light position - shading point")
        _prop(lsub, "operation", "SUBTRACT")
        lnz = _new(g, "ShaderNodeVectorMath", "Lhat", (-519, -157),
                   "unit light dir")
        _prop(lnz, "operation", "NORMALIZE")
        lref = _new(g, "ShaderNodeMix", "refract_L", (-363, -130),
                    "lerp(N, L, eta) -- light-side refraction")
        _prop(lref, "data_type", "VECTOR")
        lrn = _new(g, "ShaderNodeVectorMath", "Lrefr", (-214, -130),
                   "refracted light dir")
        _prop(lrn, "operation", "NORMALIZE")
        lsc = _new(g, "ShaderNodeVectorMath", "Lscaled", (-67, -132),
                   "* directional")
        _prop(lsc, "operation", "SCALE")
        dirg = _grp(g, "UE Limbal Occlusion (directional)", "directional",
                    (254, 88), "shader maths -- do not hand-edit")
        blend = _new(g, "ShaderNodeMix", "blend", (448, 262),
                     "radial <-> directional")
        _prop(blend, "data_type", "FLOAT")
        _link(g, gi, "Softness", negs, 0)
        _link(g, gi, "r", rad, 0)
        _link(g, negs, 0, rad, 1)
        _link(g, rad, 0, sq, 0)
        _link(g, rad, 0, sq, 1)
        _link(g, gi, "Light Position", lsub, 0)
        _link(g, gi, "Shading Point", lsub, 1)
        _link(g, lsub, 0, lnz, 0)
        _link(g, gi, "Eta", lref, "Factor")
        _link(g, gi, "N8", lref, "A")
        _link(g, lnz, 0, lref, "B")
        _link(g, lref, "Result", lrn, 0)
        _link(g, lrn, 0, lsc, 0)
        _link(g, gi, "Directional", lsc, "Scale")
        _link(g, gi, "uvC2", dirg, 0)
        _link(g, lsc, 0, dirg, 1)
        _link(g, gi, "T2", dirg, 2)
        _link(g, gi, "B2", dirg, 3)
        _link(g, gi, "r", dirg, 4)
        _link(g, gi, "Offset", dirg, 5)
        _link(g, gi, "Softness", dirg, 6)
        _link(g, gi, "Aperture", dirg, 7)
        _link(g, gi, "Directional", blend, "Factor")
        _link(g, sq, 0, blend, "A")
        _link(g, dirg, 0, blend, "B")
        _link(g, blend, "Result", iris_only, "A")
        _link(g, gi, "Sclera Mask", iris_only, "Factor")
        _link(g, gi, "Amount", amt, "Factor")
        _link(g, iris_only, "Result", amt, "B")
        _link(g, amt, "Result", go, "Occlusion")
        _fix_conventions(g)
    return _rebuild_group("FF7R Limbal Occlusion", _b)


def eye_uv():
    """Geometry-only half of the eye path: disc -> cornea, producing Iris UV and
    the cornea-local frame.  Deliberately has NO input derived from an iris
    texture colour.

    This split exists to avoid a node-graph cycle.  In the material tree the
    iris textures (IrisColor/IrisNormal/IrisOcclusion/IrisEmissive) must be
    sampled at the Iris UV this group computes -- but Blender treats a Group
    node as opaque for cycle detection, so if ONE group both consumed those
    textures' colours AND produced the UV they are sampled at, Blender refuses
    the graph outright ("This link forms a cycle which is not supported"),
    even though internally Iris UV never actually depends on iris colour.
    Keeping UV production in its own node sidesteps that: eye_uv has no path
    back into the textures that read its own output, so the two can wire to
    each other safely.  See `grp_eye()`, which consumes these outputs, and
    `build()` for the wiring."""
    def _b(g):
        _sock(g, "Iris UV", "OUTPUT", "NodeSocketVector")
        _sock(g, "Cornea Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "uvC2", "OUTPUT", "NodeSocketVector")
        _sock(g, "r", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Sclera Mask", "OUTPUT", "NodeSocketFloat")
        _sock(g, "T2", "OUTPUT", "NodeSocketVector")
        _sock(g, "B2", "OUTPUT", "NodeSocketVector")
        _sock(g, "UV0", "INPUT", "NodeSocketVector")
        _sock(g, "Cornea Normal Map", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Pupil Dilation", "INPUT", "NodeSocketFloat", 1.0, 0.05, 4.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1400, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (700, 0))

        geo = _new(g, "ShaderNodeNewGeometry", "Geometry", (-1400, 300))
        tbn = _grp(g, "FF7R Util/TBN", "TBN", (-1400, 0), "tangent frame")
        disc = _grp(g, "FF7R Eye Disc", "Disc", (-1100, 300), "eye disc / limbus")
        # cornea frame: B2 = normalize(cross(N8,T)), T2 = cross(B2,N8)
        b2 = _new(g, "ShaderNodeVectorMath", "B2", (-900, 100), "cross(N8, T)")
        _prop(b2, "operation", "CROSS_PRODUCT")
        b2n = _new(g, "ShaderNodeVectorMath", "B2n", (-740, 100))
        _prop(b2n, "operation", "NORMALIZE")
        b2w = _new(g, "ShaderNodeVectorMath", "B2w", (-580, 100), "* w")
        _prop(b2w, "operation", "SCALE")
        t2 = _new(g, "ShaderNodeVectorMath", "T2", (-900, -100), "cross(B2, N8)")
        _prop(t2, "operation", "CROSS_PRODUCT")
        t2n = _new(g, "ShaderNodeVectorMath", "T2n", (-740, -100))
        _prop(t2n, "operation", "NORMALIZE")
        t2w = _new(g, "ShaderNodeVectorMath", "T2w", (-580, -100), "* w")
        _prop(t2w, "operation", "SCALE")

        cornea = _grp(g, "FF7R Cornea", "Cornea", (-300, 100), "cornea / parallax")

        _link(g, gi, "UV0", disc, "UV0")
        _link(g, tbn, "T", b2, 1)
        _link(g, gi, "Cornea Normal Map", b2, 0)
        _link(g, b2, 0, b2n, 0)
        _link(g, b2n, 0, b2w, 0)
        _link(g, tbn, "w", b2w, "Scale")
        _link(g, b2w, 0, t2, 0)
        _link(g, gi, "Cornea Normal Map", t2, 1)
        _link(g, t2, 0, t2n, 0)
        _link(g, t2n, 0, t2w, 0)
        _link(g, tbn, "w", t2w, "Scale")

        _link(g, gi, "Cornea Normal Map", cornea, "N8")
        _link(g, geo, "Incoming", cornea, "Incoming")
        _link(g, t2w, 0, cornea, "T2")
        _link(g, b2w, 0, cornea, "B2")
        _link(g, disc, "UV", cornea, "UV")
        _link(g, disc, "uvC", cornea, "uvC")
        _link(g, gi, "Pupil Dilation", cornea, "Pupil Dilation")

        _link(g, cornea, "Iris UV", go, "Iris UV")
        _link(g, cornea, "Cornea Normal", go, "Cornea Normal")
        _link(g, cornea, "uvC2", go, "uvC2")
        _link(g, cornea, "r", go, "r")
        _link(g, disc, "Sclera Mask", go, "Sclera Mask")
        _link(g, t2w, 0, go, "T2")
        _link(g, b2w, 0, go, "B2")
        _fix_conventions(g)
    return _rebuild_group("FF7R Eye UV", _b)


def grp_eye():
    """Colour half of the eye path: limbal occlusion + iris/sclera composition.
    Outputs everything the Principled needs for SM 9.  Takes the cornea-local
    frame and Iris UV as INPUTS from `eye_uv()` rather than computing them --
    see that function's docstring for why the two are split.

    PLAN 0.3: slot 11 is `IrisOcclusion`, not a specular mask -- it multiplies
    base colour (AO), it does not drive specular."""
    def _b(g):
        _sock(g, "Base Color", "OUTPUT", "NodeSocketColor")
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Coat Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Emission", "OUTPUT", "NodeSocketColor")
        _sock(g, "Sclera Color", "INPUT", "NodeSocketColor", (0.6, 0.6, 0.6, 1.0))
        _sock(g, "Iris Color", "INPUT", "NodeSocketColor", (0.3, 0.2, 0.1, 1.0))
        _sock(g, "Iris Occlusion", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Iris Emissive", "INPUT", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0))
        _sock(g, "Sclera Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Gaze Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Cornea Normal Map", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Iris Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Cornea Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "uvC2", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
        _sock(g, "r", "INPUT", "NodeSocketFloat", 0.0, 0.0, 2.0)
        _sock(g, "Sclera Mask", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "T2", "INPUT", "NodeSocketVector", (1.0, 0.0, 0.0))
        _sock(g, "B2", "INPUT", "NodeSocketVector", (0.0, 1.0, 0.0))
        _sock(g, "Light Position", "INPUT", "NodeSocketVector", (0.0, 0.0, 3.0))
        _sock(g, "IrisEmissive_", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Limbal Amount", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1200, -200))
        go = _new(g, "NodeGroupOutput", "Group Output", (900, 0))

        geo = _new(g, "ShaderNodeNewGeometry", "Geometry", (-1200, 400),
                   "only used for Position (limbal shading point)")
        limbal = _grp(g, "FF7R Limbal Occlusion", "Limbal", (-700, 0),
                      "limbal occlusion")

        albedo = _new(g, "ShaderNodeMix", "iris_sclera", (200, 400),
                      "lerp(iris, sclera, mask)")
        _prop(albedo, "data_type", "RGBA")
        irisao = _new(g, "ShaderNodeMix", "iris_ao", (20, 560),
                      "iris colour * IrisOcclusion  [PLAN 0.3]")
        _prop(irisao, "data_type", "RGBA")
        _prop(irisao, "blend_type", "MULTIPLY")
        _sv(irisao, "Factor", 1.0)
        limbmul = _new(g, "ShaderNodeMix", "limbal_mul", (400, 400),
                       "base colour * limbal occlusion")
        _prop(limbmul, "data_type", "RGBA")
        _prop(limbmul, "blend_type", "MULTIPLY")
        _sv(limbmul, "Factor", 1.0)
        nrm = _new(g, "ShaderNodeMix", "normal_mix", (200, 100),
                   "lerp(cornea, sclera N, mask)")
        _prop(nrm, "data_type", "VECTOR")
        n2nd = _new(g, "ShaderNodeMix", "second_normal", (200, -200),
                    "lerp(cornea map, gaze, mask) -> GBufferE")
        _prop(n2nd, "data_type", "VECTOR")
        em = _new(g, "ShaderNodeMix", "emissive", (400, -420),
                  "IrisEmissive_ gate (additive, unshadowed)")
        _prop(em, "data_type", "RGBA")
        _sv(em, "A", (0.0, 0.0, 0.0, 1.0))
        eminv = _new(g, "ShaderNodeMath", "iris_side", (200, -420),
                     "1 - sclera mask")
        _prop(eminv, "operation", "SUBTRACT")
        _sv(eminv, 0, 1.0)
        emg = _new(g, "ShaderNodeMath", "em_gate", (360, -560), "* IrisEmissive_")
        _prop(emg, "operation", "MULTIPLY")

        _link(g, gi, "r", limbal, "r")
        _link(g, gi, "Sclera Mask", limbal, "Sclera Mask")
        _link(g, gi, "Cornea Normal Map", limbal, "N8")
        _link(g, gi, "uvC2", limbal, "uvC2")
        _link(g, gi, "T2", limbal, "T2")
        _link(g, gi, "B2", limbal, "B2")
        _link(g, gi, "Light Position", limbal, "Light Position")
        _link(g, geo, "Position", limbal, "Shading Point")
        _link(g, gi, "Limbal Amount", limbal, "Amount")

        _link(g, gi, "Iris Color", irisao, "A")
        _link(g, gi, "Iris Occlusion", irisao, "B")
        _link(g, irisao, "Result", albedo, "A")
        _link(g, gi, "Sclera Color", albedo, "B")
        _link(g, gi, "Sclera Mask", albedo, "Factor")
        _link(g, albedo, "Result", limbmul, "A")
        _link(g, limbal, "Occlusion", limbmul, "B")

        _link(g, gi, "Sclera Mask", nrm, "Factor")
        _link(g, gi, "Cornea Normal", nrm, "A")
        _link(g, gi, "Sclera Normal", nrm, "B")
        _link(g, gi, "Sclera Mask", n2nd, "Factor")
        _link(g, gi, "Cornea Normal Map", n2nd, "A")
        _link(g, gi, "Gaze Normal", n2nd, "B")

        _link(g, gi, "Sclera Mask", eminv, 1)
        _link(g, eminv, 0, emg, 0)
        _link(g, gi, "IrisEmissive_", emg, 1)
        _link(g, emg, 0, em, "Factor")
        _link(g, gi, "Iris Emissive", em, "B")

        _link(g, limbmul, "Result", go, "Base Color")
        _link(g, nrm, "Result", go, "Normal")
        _link(g, n2nd, "Result", go, "Coat Normal")
        _link(g, em, "Result", go, "Emission")
        _fix_conventions(g)
    return _rebuild_group("FF7R Eye", _b)


# =============================================================== HAIR PATH ===

def grp_hair_strand(t2w_group_name="UE Tangent To World (mikktspace)",
                    uv0_name=UV_MAP):
    """Hair base-pass strand axis + view-tracking shading normal.

        strand_ts = normalize( n.x*n.y , -(n.x^2+n.z^2) , n.y*n.z )
        d = dot(V, strand);  if |d| < MIN: d = sign((2*noise-1) + d/MIN) * MIN
        N = normalize(V - d*strand)

    The downstream DeferredLightPixelMain `_label53` branch is now decoded: it
    is a three-lobe strand-aligned anisotropic GGX model, not Kajiya-Kay. It
    reconstructs the strand T from this view-tracking N and V, while GBufferD's
    authored full-RGB flow vector is consumed only through dot(flow, L).
    Blender 5.2 exposes no per-light L in a material tree, so exact highlight
    placement is impossible here. Principled's Anisotropic is ~93% suppressed
    by this N in Cycles and ignored by EEVEE. `Flow Influence` is therefore an
    explicitly non-game approximation: it projects flow into the surface plane
    and folds it into the strand axis so the map can still steer the normal."""
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Mapped Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Tangent", "OUTPUT", "NodeSocketVector")
        _sock(g, "Roughness", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Strand Map", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Flow Map", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Roughness Map", "INPUT", "NodeSocketFloat", 0.35, 0.0, 1.0)
        _sock(g, "Flow Influence", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Min VdotT", "INPUT", "NodeSocketFloat", MIN_VDOTT, 0.001, 0.5)
        _sock(g, "Dither Sign", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Roughness Curvature", "INPUT", "NodeSocketFloat",
              ROUGHNESS_CURVATURE, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1700, 0))
        geo = _new(g, "ShaderNodeNewGeometry", "Geometry", (-1900, 600))

        sep = _new(g, "ShaderNodeSeparateXYZ", "n", (-1600, 500), "strand map")
        sx = _new(g, "ShaderNodeMath", "sx", (-1400, 640), "n.x * n.y")
        _prop(sx, "operation", "MULTIPLY")
        nxx = _new(g, "ShaderNodeMath", "nxx", (-1400, 480), "n.x^2")
        _prop(nxx, "operation", "MULTIPLY")
        nzz = _new(g, "ShaderNodeMath", "nzz", (-1400, 320), "n.z^2")
        _prop(nzz, "operation", "MULTIPLY")
        syp = _new(g, "ShaderNodeMath", "syp", (-1220, 400), "n.x^2 + n.z^2")
        _prop(syp, "operation", "ADD")
        sy = _new(g, "ShaderNodeMath", "sy", (-1040, 400), "-(n.x^2 + n.z^2)")
        _prop(sy, "operation", "MULTIPLY")
        _sv(sy, 1, -1.0)
        sz = _new(g, "ShaderNodeMath", "sz", (-1400, 160), "n.y * n.z")
        _prop(sz, "operation", "MULTIPLY")
        sc = _new(g, "ShaderNodeCombineXYZ", "strand_ts", (-860, 460),
                  "strand (tangent space)")
        sn = _new(g, "ShaderNodeVectorMath", "strand_n", (-680, 460))
        _prop(sn, "operation", "NORMALIZE")
        sw = _grp(g, t2w_group_name, "strand_w", (-500, 460),
                  "strand -> world")
        fw = _grp(g, t2w_group_name, "flow_w", (-500, 200),
                  "flow -> world (UV0 basis, shader-exact)")
        fdot = _new(g, "ShaderNodeVectorMath", "flow_dot_n", (-300, 80),
                    "dot(flow, geometric N)")
        _prop(fdot, "operation", "DOT_PRODUCT")
        fn = _new(g, "ShaderNodeVectorMath", "flow_n", (-120, 40),
                  "N * dot(flow,N)")
        _prop(fn, "operation", "SCALE")
        fsub = _new(g, "ShaderNodeVectorMath", "flow_plane", (60, 140),
                    "flow projected into surface plane")
        _prop(fsub, "operation", "SUBTRACT")
        fproj = _new(g, "ShaderNodeVectorMath", "flow_plane_n", (240, 140))
        _prop(fproj, "operation", "NORMALIZE")
        smix = _new(g, "ShaderNodeMix", "flow_blend", (-300, 340),
                    "APPROXIMATION: blend projected flow into strand")
        _prop(smix, "data_type", "VECTOR")
        sused = _new(g, "ShaderNodeVectorMath", "strand_used", (-120, 340),
                     "strand axis used for the normal")
        _prop(sused, "operation", "NORMALIZE")

        vdot = _new(g, "ShaderNodeVectorMath", "vdot", (80, 560),
                    "d = dot(V, strand)")
        _prop(vdot, "operation", "DOT_PRODUCT")
        vabs = _new(g, "ShaderNodeMath", "vabs", (260, 660), "|d|")
        _prop(vabs, "operation", "ABSOLUTE")
        wn = _new(g, "ShaderNodeTexWhiteNoise", "noise", (80, 200),
                  "blue-noise stand-in (world position)")
        _prop(wn, "noise_dimensions", "3D")
        nsc = _new(g, "ShaderNodeMath", "noise_signed", (260, 200), "noise*2 - 1")
        _prop(nsc, "operation", "MULTIPLY_ADD")
        _sv(nsc, 1, 2.0)
        _sv(nsc, 2, -1.0)
        dinv = _new(g, "ShaderNodeMath", "inv_min", (80, 40), "1 / MinVdotT")
        _prop(dinv, "operation", "DIVIDE")
        _sv(dinv, 0, 1.0)
        d20 = _new(g, "ShaderNodeMath", "d_scaled", (260, 40), "d / MinVdotT")
        _prop(d20, "operation", "MULTIPLY")
        rsum = _new(g, "ShaderNodeMath", "rsum", (440, 120))
        _prop(rsum, "operation", "ADD")
        dsgn = _new(g, "ShaderNodeMath", "dith_sign", (600, 120), "dithered sign")
        _prop(dsgn, "operation", "SIGN")
        hsgn = _new(g, "ShaderNodeMath", "hard_sign", (600, -60), "sign(d)")
        _prop(hsgn, "operation", "SIGN")
        signsel = _new(g, "ShaderNodeMix", "sign_mode", (760, 40),
                       "hard sign / dithered sign")
        _prop(signsel, "data_type", "FLOAT")
        dsmall = _new(g, "ShaderNodeMath", "d_small", (920, 40), "+/- MinVdotT")
        _prop(dsmall, "operation", "MULTIPLY")
        near = _new(g, "ShaderNodeMath", "near", (440, 660), "|d| < MinVdotT")
        _prop(near, "operation", "LESS_THAN")
        dmix = _new(g, "ShaderNodeMix", "d_clamped", (1080, 560), "clamped d")
        _prop(dmix, "data_type", "FLOAT")
        proj = _new(g, "ShaderNodeVectorMath", "proj", (1240, 460), "d * strand")
        _prop(proj, "operation", "SCALE")
        nsub = _new(g, "ShaderNodeVectorMath", "nsub", (1400, 560), "V - d*strand")
        _prop(nsub, "operation", "SUBTRACT")
        nsh = _new(g, "ShaderNodeVectorMath", "nshade", (1560, 560),
                   "shading normal (world)")
        _prop(nsh, "operation", "NORMALIZE")
        n2w = _grp(g, t2w_group_name, "mapped_n", (1240, 760),
                   "strand map as a plain normal")
        rsq = _new(g, "ShaderNodeMath", "rsq", (1080, -320), "roughness^2")
        _prop(rsq, "operation", "MULTIPLY")
        radd = _new(g, "ShaderNodeMath", "radd", (1240, -320), "+ curvature")
        _prop(radd, "operation", "ADD")
        _prop(radd, "use_clamp", True)
        rsqrt = _new(g, "ShaderNodeMath", "rsqrt", (1400, -320),
                     "sqrt(clamp(curv + r^2))")
        _prop(rsqrt, "operation", "SQRT")

        _link(g, gi, "Strand Map", sep, 0)
        _link(g, sep, 0, sx, 0)
        _link(g, sep, 1, sx, 1)
        _link(g, sep, 0, nxx, 0)
        _link(g, sep, 0, nxx, 1)
        _link(g, sep, 2, nzz, 0)
        _link(g, sep, 2, nzz, 1)
        _link(g, nxx, 0, syp, 0)
        _link(g, nzz, 0, syp, 1)
        _link(g, syp, 0, sy, 0)
        _link(g, sep, 1, sz, 0)
        _link(g, sep, 2, sz, 1)
        _link(g, sx, 0, sc, 0)
        _link(g, sy, 0, sc, 1)
        _link(g, sz, 0, sc, 2)
        _link(g, sc, 0, sn, 0)
        _link(g, sn, 0, sw, 0)
        _link(g, gi, "Flow Map", fw, 0)
        _link(g, fw, 0, fdot, 0)
        _link(g, geo, "Normal", fdot, 1)
        _link(g, geo, "Normal", fn, 0)
        _link(g, fdot, "Value", fn, "Scale")
        _link(g, fw, 0, fsub, 0)
        _link(g, fn, 0, fsub, 1)
        _link(g, fsub, 0, fproj, 0)
        _link(g, gi, "Flow Influence", smix, "Factor")
        _link(g, sw, 0, smix, "A")
        _link(g, fproj, 0, smix, "B")
        _link(g, smix, "Result", sused, 0)
        _link(g, geo, "Incoming", vdot, 0)
        _link(g, sused, 0, vdot, 1)
        _link(g, vdot, "Value", vabs, 0)
        _link(g, geo, "Position", wn, "Vector")
        _link(g, wn, "Value", nsc, 0)
        _link(g, gi, "Min VdotT", dinv, 1)
        _link(g, vdot, "Value", d20, 0)
        _link(g, dinv, 0, d20, 1)
        _link(g, nsc, 0, rsum, 0)
        _link(g, d20, 0, rsum, 1)
        _link(g, rsum, 0, dsgn, 0)
        _link(g, vdot, "Value", hsgn, 0)
        _link(g, gi, "Dither Sign", signsel, "Factor")
        _link(g, hsgn, 0, signsel, "A")
        _link(g, dsgn, 0, signsel, "B")
        _link(g, signsel, "Result", dsmall, 0)
        _link(g, gi, "Min VdotT", dsmall, 1)
        _link(g, vabs, 0, near, 0)
        _link(g, gi, "Min VdotT", near, 1)
        _link(g, near, 0, dmix, "Factor")
        _link(g, vdot, "Value", dmix, "A")
        _link(g, dsmall, 0, dmix, "B")
        _link(g, sused, 0, proj, 0)
        _link(g, dmix, "Result", proj, "Scale")
        _link(g, geo, "Incoming", nsub, 0)
        _link(g, proj, 0, nsub, 1)
        _link(g, nsub, 0, nsh, 0)
        _link(g, gi, "Strand Map", n2w, 0)
        _link(g, gi, "Roughness Map", rsq, 0)
        _link(g, gi, "Roughness Map", rsq, 1)
        _link(g, rsq, 0, radd, 0)
        _link(g, gi, "Roughness Curvature", radd, 1)
        _link(g, radd, 0, rsqrt, 0)
        _link(g, nsh, 0, go, "Normal")
        _link(g, n2w, 0, go, "Mapped Normal")
        # Texture 14/GBufferD is the only authored downstream flow direction.
        # Feeding it to Principled Tangent is approximate, but less misleading
        # than exporting the reconstructed strand as though it were that map.
        _link(g, fw, 0, go, "Tangent")
        _link(g, rsqrt, 0, go, "Roughness")
    group_name = ("FF7R Hair Strand" if not uv0_name else
                  "FF7R Hair Strand (%s)" % uv0_name)
    return _rebuild_group(group_name, _b)


# ============================================================ OTHER FEATURES ==

def grp_coverage():
    """PLAN 2.6 -- three modes.  Native Alpha is the default; the blue-noise
    dither only reads as soft coverage because UE's TAA resolves it, which
    Blender has no equivalent of, so it is present for reference only."""
    def _b(g):
        _sock(g, "Alpha", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Coverage", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Object Fade", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Clip", "INPUT", "NodeSocketFloat", OPACITY_CLIP, 0.0, 1.0)
        _sock(g, "Enable", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Mode", "INPUT", "NodeSocketFloat", 0.0, 0.0, 2.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1100, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1400, 0))
        fade = _new(g, "ShaderNodeMath", "fade", (-880, 200),
                    "coverage * object fade")
        _prop(fade, "operation", "MULTIPLY")
        cut = _new(g, "ShaderNodeMath", "cutout", (-880, 20),
                   "hard cutout: > clip  (authored 0.3333)")
        _prop(cut, "operation", "GREATER_THAN")
        noise = _new(g, "ShaderNodeTexWhiteNoise", "dither_noise", (-880, -220),
                     "stand-in for View_SpatiotemporalBlueNoise [not portable]")
        _prop(noise, "noise_dimensions", "2D")
        dth = _new(g, "ShaderNodeMath", "dither", (-660, -220),
                   "n*(255/256) + 1/512")
        _prop(dth, "operation", "MULTIPLY_ADD")
        _sv(dth, 1, 255.0 / 256.0)
        _sv(dth, 2, 1.0 / 512.0)
        cmp_ = _new(g, "ShaderNodeMath", "dither_test", (-460, -220),
                    "coverage > dither(n)")
        _prop(cmp_, "operation", "GREATER_THAN")
        mode, sel, vals = _menu(g, "CoverageMode", (-200, 60),
                                ["Alpha (native)", "Hard cutout",
                                 "Dithered (faithful)"], "FLOAT", "Coverage Mode",
                                selector_src=(gi, "Mode"))
        _menu_feed(g, vals, 0, fade)
        _menu_feed(g, vals, 1, cut)
        _menu_feed(g, vals, 2, cmp_)
        onoff = _new(g, "ShaderNodeMix", "enable", (1200, 0),
                     "Coverage_ off -> alpha 1")
        _prop(onoff, "data_type", "FLOAT")
        _sv(onoff, "A", 1.0)
        _link(g, gi, "Coverage", fade, 0)
        _link(g, gi, "Object Fade", fade, 1)
        _link(g, gi, "Coverage", cut, 0)
        _link(g, gi, "Clip", cut, 1)
        _link(g, gi, "Coverage", cmp_, 0)
        _link(g, noise, "Value", dth, 0)
        _link(g, dth, 0, cmp_, 1)
        _link(g, gi, "Enable", onoff, "Factor")
        _link(g, mode, _menu_out(mode), onoff, "B")
        _link(g, onoff, "Result", go, "Alpha")
    return _rebuild_group("FF7R Coverage", _b)


def grp_detail_layer():
    """Character detail (slerp) and MEC/environment detail (exact RNM core).

    The RNM option is transcribed from the retained Granite shader and is no
    longer a stub. `build()` forces this option when Isotropy_ is enabled and
    enables the layer from the actual detail-role switches, because current
    RMI_Surface_Standard_Overlay_Isotropy_Detail variants do not carry Detail_.
    Chained remains an artist-friendly preview option, not game shader math."""
    def _b(g):
        _sock(g, "Base Color", "OUTPUT", "NodeSocketColor")
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "Roughness", "OUTPUT", "NodeSocketFloat")
        _sock(g, "AO", "OUTPUT", "NodeSocketFloat")
        for nm_, ty, dv in (("Base Color", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)),
                            ("Base Normal", "NodeSocketVector", (0.0, 0.0, 1.0)),
                            ("Base Roughness", "NodeSocketFloat", 0.5),
                            ("Base AO", "NodeSocketFloat", 1.0),
                            ("Detail Color", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)),
                            ("Detail Normal", "NodeSocketVector", (0.0, 0.0, 1.0)),
                            ("Detail Roughness", "NodeSocketFloat", 0.0),
                            ("Detail AO", "NodeSocketFloat", 1.0),
                            ("Detail Mask", "NodeSocketFloat", 0.0),
                            ("Enable", "NodeSocketFloat", 0.0),
                            ("Detail Blend", "NodeSocketFloat", 0.0)):
            _sock(g, nm_, "INPUT", ty, dv)
        _sock(g, "Normal Strength", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1100, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1400, 0))
        gate = _new(g, "ShaderNodeMath", "gate", (-860, -520),
                    "mask * Detail_ enable")
        _prop(gate, "operation", "MULTIPLY")
        # Detail-normal intensity: scale the tangent xy and renormalize, the
        # same shaping a Normal Map node's Strength applies.  Separate from the
        # gate, which is the mask; this is the artist-facing amount.
        dsep = _new(g, "ShaderNodeSeparateXYZ", "detail_normal_xyz", (-1000, 300))
        dsx = _new(g, "ShaderNodeMath", "detail_normal_x", (-860, 380),
                   "detail.x * strength")
        _prop(dsx, "operation", "MULTIPLY")
        dsy = _new(g, "ShaderNodeMath", "detail_normal_y", (-860, 220),
                   "detail.y * strength")
        _prop(dsy, "operation", "MULTIPLY")
        dcomb = _new(g, "ShaderNodeCombineXYZ", "detail_normal_scaled", (-720, 300))
        dnorm = _new(g, "ShaderNodeVectorMath", "detail_normal_renormalize",
                     (-720, 140), "scaled detail normal")
        _prop(dnorm, "operation", "NORMALIZE")
        _link(g, gi, "Detail Normal", dsep, 0)
        _link(g, dsep, "X", dsx, 0)
        _link(g, gi, "Normal Strength", dsx, 1)
        _link(g, dsep, "Y", dsy, 0)
        _link(g, gi, "Normal Strength", dsy, 1)
        _link(g, dsx, 0, dcomb, "X")
        _link(g, dsy, 0, dcomb, "Y")
        _link(g, dsep, "Z", dcomb, "Z")
        _link(g, dcomb, 0, dnorm, 0)
        ov = _new(g, "ShaderNodeMix", "overlay", (-560, 460),
                  "albedo overlay (native Blender mix)")
        _prop(ov, "data_type", "RGBA")
        _prop(ov, "blend_type", "OVERLAY")
        slerp = _grp(g, "FF7R Util/Normal Slerp", "slerp", (-560, 140),
                     "normal slerp [tier 1]")
        rnm = _grp(g, "FF7R Util/RNM", "rnm", (-560, -20),
                   "reoriented normal mapping [tier 1]")
        rc = _grp(g, "FF7R Util/Roughness Combine", "rough", (-560, -180))
        ao = _grp(g, "FF7R Util/AO Combine", "ao", (-560, -360))
        chain = _new(g, "ShaderNodeMix", "chained", (-560, -60),
                     "chained: detail over base (Blender-idiomatic)")
        _prop(chain, "data_type", "VECTOR")
        chn = _new(g, "ShaderNodeVectorMath", "chain_n", (-380, -60))
        _prop(chn, "operation", "NORMALIZE")
        blend, sel, bvals = _menu(g, "DetailBlend", (-160, 140),
                                  ["Slerp (character)", "Chained Normal Map",
                                   "RNM (MEC, decompiled)"], "VECTOR",
                                  "Detail Normal Blend",
                                  selector_src=(gi, "Detail Blend"))
        _menu_feed(g, bvals, 0, slerp)
        _menu_feed(g, bvals, 1, chn)
        _menu_feed(g, bvals, 2, rnm)
        _link(g, gi, "Detail Mask", gate, 0)
        _link(g, gi, "Enable", gate, 1)
        _link(g, gi, "Base Color", ov, "A")
        _link(g, gi, "Detail Color", ov, "B")
        _link(g, gate, 0, ov, "Factor")
        _link(g, gi, "Base Normal", slerp, "A")
        _link(g, dnorm, 0, slerp, "B")
        _link(g, gate, 0, slerp, "t")
        _link(g, gi, "Base Normal", rnm, "Base")
        _link(g, dnorm, 0, rnm, "Detail")
        _link(g, gate, 0, rnm, "Strength")
        _link(g, gate, 0, chain, "Factor")
        _link(g, gi, "Base Normal", chain, "A")
        _link(g, dnorm, 0, chain, "B")
        _link(g, chain, "Result", chn, 0)
        _link(g, gi, "Base Roughness", rc, "R1")
        _link(g, gi, "Detail Roughness", rc, "R2")
        _link(g, gi, "Base AO", ao, "L1")
        _link(g, gi, "Detail AO", ao, "L2")
        _link(g, ov, "Result", go, "Base Color")
        _link(g, blend, _menu_out(blend), go, "Normal")
        _link(g, rc, "Roughness", go, "Roughness")
        _link(g, ao, "AO", go, "AO")
    return _rebuild_group("FF7R Detail Layer", _b)


def grp_segment_layers():
    """Segmented_ / SegmentLayer0_ / 1_  -- two independently tinted, masked
    overlays (PLAN tier 2):  color += clamp(tint.w,0,1) * mask.x"""
    def _b(g):
        _sock(g, "Base Color", "OUTPUT", "NodeSocketColor")
        _sock(g, "Base Color", "INPUT", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0))
        _sock(g, "Mask 0", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Tint 0", "INPUT", "NodeSocketColor", (1.0, 0.0, 0.0, 1.0))
        _sock(g, "Opacity 0", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Mask 1", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Tint 1", "INPUT", "NodeSocketColor", (0.0, 0.0, 1.0, 1.0))
        _sock(g, "Opacity 1", "INPUT", "NodeSocketFloat", 1.0, 0.0, 1.0)
        _sock(g, "Enable", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-820, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (820, 0))
        f0 = _new(g, "ShaderNodeMath", "f0", (-580, 200), "clamp(op0) * mask0")
        _prop(f0, "operation", "MULTIPLY")
        _prop(f0, "use_clamp", True)
        f1 = _new(g, "ShaderNodeMath", "f1", (-580, -200), "clamp(op1) * mask1")
        _prop(f1, "operation", "MULTIPLY")
        _prop(f1, "use_clamp", True)
        g0 = _new(g, "ShaderNodeMath", "g0", (-380, 200), "* Segmented_")
        _prop(g0, "operation", "MULTIPLY")
        g1 = _new(g, "ShaderNodeMath", "g1", (-380, -200), "* Segmented_")
        _prop(g1, "operation", "MULTIPLY")
        m0 = _new(g, "ShaderNodeMix", "layer0", (-120, 80), "paint layer 0")
        _prop(m0, "data_type", "RGBA")
        m1 = _new(g, "ShaderNodeMix", "layer1", (160, 0), "paint layer 1")
        _prop(m1, "data_type", "RGBA")
        _link(g, gi, "Opacity 0", f0, 0)
        _link(g, gi, "Mask 0", f0, 1)
        _link(g, gi, "Opacity 1", f1, 0)
        _link(g, gi, "Mask 1", f1, 1)
        _link(g, f0, 0, g0, 0)
        _link(g, gi, "Enable", g0, 1)
        _link(g, f1, 0, g1, 0)
        _link(g, gi, "Enable", g1, 1)
        _link(g, g0, 0, m0, "Factor")
        _link(g, gi, "Base Color", m0, "A")
        _link(g, gi, "Tint 0", m0, "B")
        _link(g, g1, 0, m1, "Factor")
        _link(g, m0, "Result", m1, "A")
        _link(g, gi, "Tint 1", m1, "B")
        _link(g, m1, "Result", go, "Base Color")
    return _rebuild_group("FF7R Segment Layers", _b)


def grp_layer_blend():
    """Layered_ / ShadingLayer_ -- Overlay the layer colour through its mask,
    while roughness and metallic retain scalar mixes (PLAN tier 2). Corrects
    an earlier wrong assumption: Fabric_ and Standard_ are NOT mutually
    exclusive; Chocobo Body carries both."""
    def _b(g):
        _sock(g, "Base Color", "OUTPUT", "NodeSocketColor")
        _sock(g, "Roughness", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Metallic", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Base Color", "INPUT", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0))
        _sock(g, "Layer Color", "INPUT", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0))
        _sock(g, "Base Roughness", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        _sock(g, "Layer Roughness", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        _sock(g, "Base Metallic", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Layer Metallic", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Mask", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Enable", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-700, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (700, 0))
        gate = _new(g, "ShaderNodeMath", "gate", (-460, -300), "mask * Layered_")
        _prop(gate, "operation", "MULTIPLY")
        c = _new(g, "ShaderNodeMix", "color", (-160, 220))
        _prop(c, "data_type", "RGBA")
        _prop(c, "blend_type", "OVERLAY")
        r = _new(g, "ShaderNodeMix", "rough", (-160, 0))
        _prop(r, "data_type", "FLOAT")
        m = _new(g, "ShaderNodeMix", "metal", (-160, -200))
        _prop(m, "data_type", "FLOAT")
        _link(g, gi, "Mask", gate, 0)
        _link(g, gi, "Enable", gate, 1)
        for node, a, b in ((c, "Base Color", "Layer Color"),
                           (r, "Base Roughness", "Layer Roughness"),
                           (m, "Base Metallic", "Layer Metallic")):
            _link(g, gate, 0, node, "Factor")
            _link(g, gi, a, node, "A")
            _link(g, gi, b, node, "B")
        _link(g, c, "Result", go, "Base Color")
        _link(g, r, "Result", go, "Roughness")
        _link(g, m, "Result", go, "Metallic")
    return _rebuild_group("FF7R Layer Blend", _b)


def grp_transition_normal():
    """PositiveTransitionNormal_ / VertexExpressionBone_ -- the tier-1 slerp with
    a blend factor that comes from BONE ANIMATION STATE rather than a texture.

    Blender has no skeleton-driven shader input, so the source is a menu:
    Constant / Attribute (a named vertex layer) / Object property.  Only 2
    materials in the game use it and the driving bone is unknown."""
    def _b(g):
        _sock(g, "Normal", "OUTPUT", "NodeSocketVector")
        _sock(g, "t", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Base Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Transition Normal", "INPUT", "NodeSocketVector", (0.0, 0.0, 1.0))
        _sock(g, "Constant", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Enable", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Source", "INPUT", "NodeSocketFloat", 0.0, 0.0, 2.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-900, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (900, 0))
        attr = _new(g, "ShaderNodeAttribute", "vertex_expression", (-700, -240),
                    'geometry attribute "VertexExpression"')
        _prop(attr, "attribute_type", "GEOMETRY")
        _prop(attr, "attribute_name", "VertexExpression")
        obj = _new(g, "ShaderNodeAttribute", "object_prop", (-700, -460),
                   'object["VertexExpressionBone"] -- keyframe this')
        _prop(obj, "attribute_type", "OBJECT")
        _prop(obj, "attribute_name", "VertexExpressionBone")
        src, sel, svals = _menu(g, "VExprSource", (-440, -140),
                                ["Constant", "Attribute", "Object property"],
                                "FLOAT", "Vertex Expression Source",
                                selector_src=(gi, "Source"))
        _menu_feed(g, svals, 1, attr, "Fac")
        _menu_feed(g, svals, 2, obj, "Fac")
        gate = _new(g, "ShaderNodeMath", "gate", (400, -140),
                    "t * PositiveTransitionNormal_")
        _prop(gate, "operation", "MULTIPLY")
        _prop(gate, "use_clamp", True)
        slerp = _grp(g, "FF7R Util/Normal Slerp", "slerp", (620, 140),
                     "same slerp as Detail_ [tier 1]")
        _menu_feed(g, svals, 0, gi, "Constant")
        _link(g, src, _menu_out(src), gate, 0)
        _link(g, gi, "Enable", gate, 1)
        _link(g, gi, "Base Normal", slerp, "A")
        _link(g, gi, "Transition Normal", slerp, "B")
        _link(g, gate, 0, slerp, "t")
        _link(g, slerp, "Normal", go, "Normal")
        _link(g, gate, 0, go, "t")
    return _rebuild_group("FF7R Transition Normal", _b)


def grp_emissive():
    """Emissive_ / ExtraEmissive_ / ExternalEmissive_.

    ExternalEmissive_, confirmed by the user (2026-08-24) from their own use of
    the identical pattern on light-up props that need different colours in
    different areas of the same map: multiplies the emissive sum by an
    UNCLAMPED RGB value set per OBJECT INSTANCE, not a material parameter --
    UE Custom Primitive Data or equivalent, set by the actor/Blueprint. This is
    why `ExternalEmissiveContext` reads (1,1,1,0) in every material in the game
    with zero exceptions: that is the material-level no-op default, not
    evidence the multiplier is always neutral -- the real value lives entirely
    outside what a material JSON can show. Unclamped matters: it is what lets
    this drive HDR/bloom intensity per-instance without a separate material per
    state or per colour. Still PLAN tier 3 for the exact formula (plain
    multiply is the working model, not shader-confirmed byte-for-byte) -- but
    the per-object, not per-material, locality is now confirmed, not guessed.

    Blender mechanism, and why it is NOT a driver: every other control in this
    file is a MATERIAL custom property read through an AVERAGE driver (see
    `_drive()`), which works because there is exactly one shared master
    material. A driver can't do the same job here -- a driver's target is a
    static reference to ONE specific object, chosen at authoring time, so it
    cannot represent "whichever object this shared material happens to be on."
    The correct primitive is a `ShaderNodeAttribute` with
    `attribute_type='OBJECT'`, which reads a named custom property off
    whichever object is actually being shaded, entirely inside the node graph,
    varying correctly per instance with no driver at all -- the same pattern
    `grp_transition_normal()` already uses for `VertexExpressionBone_`. Reads
    `object["ExternalEmissiveTint"]`; `build()` ensures that property exists
    (defaulting to white) on every object this material is assigned to, so an
    object nobody has customised yet is neutral rather than going black."""
    def _b(g):
        _sock(g, "Emission", "OUTPUT", "NodeSocketColor")
        _sock(g, "Strength", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Emissive", "INPUT", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0))
        _sock(g, "Extra Emissive", "INPUT", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0))
        _sock(g, "Emissive_", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "ExtraEmissive_", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "ExternalEmissive_", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-800, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (800, 0))
        e0 = _new(g, "ShaderNodeMix", "gate0", (-560, 200), "Emissive_ gate")
        _prop(e0, "data_type", "RGBA")
        _sv(e0, "A", (0.0, 0.0, 0.0, 1.0))
        e1 = _new(g, "ShaderNodeMix", "gate1", (-560, -80), "ExtraEmissive_ gate")
        _prop(e1, "data_type", "RGBA")
        _sv(e1, "A", (0.0, 0.0, 0.0, 1.0))
        add = _new(g, "ShaderNodeMix", "sum", (-300, 60), "additive")
        _prop(add, "data_type", "RGBA")
        _prop(add, "blend_type", "ADD")
        _sv(add, "Factor", 1.0)
        objtint = _new(g, "ShaderNodeAttribute", "external_tint", (-560, -260),
                       'object["ExternalEmissiveTint"] -- per-instance, UNCLAMPED')
        _prop(objtint, "attribute_type", "OBJECT")
        _prop(objtint, "attribute_name", "ExternalEmissiveTint")
        tint = _new(g, "ShaderNodeMix", "external", (-60, 60),
                    "* per-instance tint, UNCLAMPED -- see docstring")
        _prop(tint, "data_type", "RGBA")
        _prop(tint, "blend_type", "MULTIPLY")
        _prop(tint, "clamp_result", False)
        st = _new(g, "ShaderNodeMath", "strength", (-60, -260),
                  "max(Emissive_, ExtraEmissive_)")
        _prop(st, "operation", "MAXIMUM")
        _link(g, gi, "Emissive_", e0, "Factor")
        _link(g, gi, "Emissive", e0, "B")
        _link(g, gi, "ExtraEmissive_", e1, "Factor")
        _link(g, gi, "Extra Emissive", e1, "B")
        _link(g, e0, "Result", add, "A")
        _link(g, e1, "Result", add, "B")
        _link(g, gi, "ExternalEmissive_", tint, "Factor")
        _link(g, add, "Result", tint, "A")
        _link(g, objtint, "Color", tint, "B")
        _link(g, gi, "Emissive_", st, 0)
        _link(g, gi, "ExtraEmissive_", st, 1)
        _link(g, tint, "Result", go, "Emission")
        _link(g, st, 0, go, "Strength")
    return _rebuild_group("FF7R Emissive", _b)


def grp_blood():
    """Blood_ / OxygenSaturation_ -- procedural haemoglobin colour (PLAN tier 2).
    PLAN 0.4: s is a per-material CONSTANT (3 assets game-wide, all solid), not a
    sampled map, so the whole ramp is three Math nodes."""
    def _b(g):
        _sock(g, "Base Color", "OUTPUT", "NodeSocketColor")
        _sock(g, "Oxygen Saturation", "INPUT", "NodeSocketFloat", 0.078, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-660, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        outs = []
        for i, (nm_, (a, b)) in enumerate((("R", BLOOD_R), ("G", BLOOD_G),
                                           ("B", BLOOD_B))):
            n = _new(g, "ShaderNodeMath", nm_, (-400, 220 - i * 200),
                     "%s = %.5f + %.5f*s" % (nm_, a, b))
            _prop(n, "operation", "MULTIPLY_ADD")
            _sv(n, 1, b)
            _sv(n, 2, a)
            _link(g, gi, "Oxygen Saturation", n, 0)
            outs.append(n)
        c = _new(g, "ShaderNodeCombineColor", "combine", (-80, 0),
                 "dark venous -> bright arterial")
        for i, n in enumerate(outs):
            _link(g, n, 0, c, i)
        _link(g, c, 0, go, "Base Color")
    return _rebuild_group("FF7R Blood", _b)


def grp_film():
    """Film_ / FilmThickness_ / FilmStructure_ -- thin-film iridescence.
    PLAN 2.12: Principled has this natively, and since the branch is tier 3 (one
    material in the game, never diffed) a native mapping at the right magnitude
    beats reproducing unknown maths."""
    def _b(g):
        _sock(g, "Thin Film Thickness", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Thin Film IOR", "OUTPUT", "NodeSocketFloat")
        _sock(g, "Thickness", "INPUT", "NodeSocketFloat", 0.5, 0.0, 1.0)
        _sock(g, "Structure", "INPUT", "NodeSocketFloat", 0.4, 0.0, 1.0)
        _sock(g, "Enable", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-620, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (620, 0))
        nm = _new(g, "ShaderNodeMath", "nm", (-380, 120),
                  "0..1 -> 0..800 nm  [ROUGH: magnitude only]")
        _prop(nm, "operation", "MULTIPLY")
        _sv(nm, 1, 800.0)
        gate = _new(g, "ShaderNodeMath", "gate", (-160, 120), "* Film_")
        _prop(gate, "operation", "MULTIPLY")
        ior = _new(g, "ShaderNodeMath", "ior", (-160, -140),
                   "structure -> film IOR 1.0..2.0")
        _prop(ior, "operation", "MULTIPLY_ADD")
        _sv(ior, 1, 1.0)
        _sv(ior, 2, 1.0)
        _link(g, gi, "Thickness", nm, 0)
        _link(g, nm, 0, gate, 0)
        _link(g, gi, "Enable", gate, 1)
        _link(g, gi, "Structure", ior, 0)
        _link(g, gate, 0, go, "Thin Film Thickness")
        _link(g, ior, 0, go, "Thin Film IOR")
    return _rebuild_group("FF7R Film", _b)


SHADING_MODELS = ["Standard (SM1)", "Skin (SM3)", "Subsurface Profile (SM5)",
                  "Hair (SM7)", "Cloth (SM8)", "Eye (SM9)", "Unlit (SM0)"]


def grp_shading_model():
    """PLAN 2.1 -- the GBuffer shading-model id does not select a different
    shader, it selects a different reading of the same buffers.  So this switches
    Principled's PARAMETERS and the material keeps exactly one BSDF."""
    def _b(g):
        outs = ["Subsurface Weight", "Sheen Weight", "Anisotropic",
                "Coat Weight", "Metallic Scale", "Emission Boost",
                "Diffuse Roughness"]
        if THIN_WALL_SOCKET:
            outs.append(THIN_WALL_SOCKET)
        for nm_ in outs:
            _sock(g, nm_, "OUTPUT", "NodeSocketFloat")
        if HAS_MANUAL_MENU_SWITCH:
            _sock(g, "Is Hair", "OUTPUT", "NodeSocketBool")
            _sock(g, "Is Eye", "OUTPUT", "NodeSocketBool")
        _sock(g, "Subsurface Radius", "OUTPUT", "NodeSocketVector")
        # One selector for the entire material, supplied from the top-level
        # Constant Menu.  A menu socket preserves the enum definition through
        # the group boundary in Blender 5.2; older builds keep the numeric input
        # and comparison-ladder implementation below.
        if HAS_MANUAL_MENU_SWITCH:
            _sock(g, "Shading Model", "INPUT", "NodeSocketMenu")
        else:
            _sock(g, "Shading Model", "INPUT", "NodeSocketFloat", 0.0, 0.0,
                  float(len(SHADING_MODELS) - 1))
        _sock(g, "Diffusion", "INPUT", "NodeSocketFloat", 0.0, 0.0, 1.0)
        _sock(g, "Thickness", "INPUT", "NodeSocketFloat", 0.2, 0.0, 1.0)
        _sock(g, "Subsurface Scale", "INPUT", "NodeSocketFloat", 0.05, 0.0, 1.0)
        _sock(g, "Hair Anisotropy", "INPUT", "NodeSocketFloat", 0.0, -1.0, 1.0)
        gi = _new(g, "NodeGroupInput", "Group Input", (-1500, 0))
        go = _new(g, "NodeGroupOutput", "Group Output", (1600, 0))
        # Cloth is a thin translucent sheet, so from 5.2 on it takes full
        # subsurface together with Principled's thin-wall mode.  Both stay off
        # on 5.1 and earlier: that build has no thin-wall input, and turning
        # subsurface on by itself would just darken and blur the cloth with no
        # translucency to show for it.
        cloth = SHADING_MODELS.index("Cloth (SM8)")
        cloth_sss = 1.0 if THIN_WALL_SOCKET else 0.0
        if HAS_MANUAL_MENU_SWITCH:
            # One Menu Switch supplies a one-hot boolean output for every model.
            # Those booleans are much clearer than seven separate compare/mix
            # ladders and all follow the same menu arriving from the material.
            flags, _sel, flag_values = _menu(
                g, "SM_ModelFlags", (-1100, 700), SHADING_MODELS, "FLOAT",
                "Selected shading model flags", selector_src=(gi, "Shading Model"),
                manual=True)
            for i in range(len(SHADING_MODELS)):
                _menu_set(flag_values, i, 0.0)

            def flag(model_name):
                return flags.outputs[model_name]

            def scaled_flag(name, model_name, scale, loc):
                node = _new(g, "ShaderNodeMath", name, loc,
                            "%s × %.3g" % (model_name, scale))
                _prop(node, "operation", "MULTIPLY")
                g.links.new(flag(model_name), node.inputs[0])
                node.inputs[1].default_value = scale
                return node

            skin_sss = scaled_flag("SM_SkinSSS", "Skin (SM3)", 0.35, (-700, 800))
            profile_sss = scaled_flag("SM_ProfileSSS", "Subsurface Profile (SM5)",
                                      0.5, (-700, 600))
            sss_sum = _new(g, "ShaderNodeMath", "SM_SSSSum", (-420, 750),
                           "Skin + profile SSS")
            _prop(sss_sum, "operation", "ADD")
            _link(g, skin_sss, 0, sss_sum, 0)
            _link(g, profile_sss, 0, sss_sum, 1)
            sss_out = sss_sum
            if cloth_sss:
                cloth_sum = _new(g, "ShaderNodeMath", "SM_SSSWithCloth", (-160, 750),
                                 "SSS + cloth thin-wall transmission")
                _prop(cloth_sum, "operation", "ADD")
                _link(g, sss_sum, 0, cloth_sum, 0)
                g.links.new(flag("Cloth (SM8)"), cloth_sum.inputs[1])
                sss_out = cloth_sum
            _link(g, sss_out, 0, go, "Subsurface Weight")

            sheen = _new(g, "ShaderNodeMath", "SM_ClothSheen", (-420, 400),
                         "Cloth × Diffusion")
            _prop(sheen, "operation", "MULTIPLY")
            g.links.new(flag("Cloth (SM8)"), sheen.inputs[0])
            _link(g, gi, "Diffusion", sheen, 1)
            _link(g, sheen, 0, go, "Sheen Weight")

            aniso = _new(g, "ShaderNodeMath", "SM_HairAnisotropy", (-420, 160),
                         "Hair × anisotropy")
            _prop(aniso, "operation", "MULTIPLY")
            g.links.new(flag("Hair (SM7)"), aniso.inputs[0])
            _link(g, gi, "Hair Anisotropy", aniso, 1)
            _link(g, aniso, 0, go, "Anisotropic")

            g.links.new(flag("Eye (SM9)"), go.inputs["Coat Weight"])
            g.links.new(flag("Unlit (SM0)"), go.inputs["Emission Boost"])
            g.links.new(flag("Hair (SM7)"), go.inputs["Is Hair"])
            g.links.new(flag("Eye (SM9)"), go.inputs["Is Eye"])

            nonmetal_a = _new(g, "ShaderNodeMath", "SM_NonMetalA", (-700, -120),
                              "Skin or profile")
            _prop(nonmetal_a, "operation", "MAXIMUM")
            g.links.new(flag("Skin (SM3)"), nonmetal_a.inputs[0])
            g.links.new(flag("Subsurface Profile (SM5)"), nonmetal_a.inputs[1])
            nonmetal_b = _new(g, "ShaderNodeMath", "SM_NonMetalB", (-420, -120),
                              "Skin/profile or eye")
            _prop(nonmetal_b, "operation", "MAXIMUM")
            _link(g, nonmetal_a, 0, nonmetal_b, 0)
            g.links.new(flag("Eye (SM9)"), nonmetal_b.inputs[1])
            metallic = _new(g, "ShaderNodeMath", "SM_MetallicScale", (-160, -120),
                            "1 - non-metal shading models")
            _prop(metallic, "operation", "SUBTRACT")
            metallic.inputs[0].default_value = 1.0
            _link(g, nonmetal_b, 0, metallic, 1)
            _link(g, metallic, 0, go, "Metallic Scale")

            diffuse = scaled_flag("SM_ClothDiffuseRoughness", "Cloth (SM8)",
                                  0.5, (-420, -360))
            _link(g, diffuse, 0, go, "Diffuse Roughness")
            if THIN_WALL_SOCKET:
                g.links.new(flag("Cloth (SM8)"), go.inputs[THIN_WALL_SOCKET])
        else:
            # per-model constants indexed by SHADING_MODELS; None = wire an input
            specs = [
                ("Subsurface Weight",  [0.0, 0.35, 0.5, 0.0, cloth_sss, 0.0, 0.0]),
                ("Sheen Weight",       [0.0, 0.0, 0.0, 0.0, None, 0.0, 0.0]),
                ("Anisotropic",        [0.0, 0.0, 0.0, None, 0.0, 0.0, 0.0]),
                ("Coat Weight",        [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]),
                ("Metallic Scale",     [1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 1.0]),
                ("Emission Boost",     [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]),
                ("Diffuse Roughness",  [0.0, 0.0, 0.0, 0.0, 0.5, 0.0, 0.0]),
            ]
            if THIN_WALL_SOCKET:
                thin = [0.0] * len(SHADING_MODELS)
                thin[cloth] = 1.0
                specs.append((THIN_WALL_SOCKET, thin))
            wire = {"Sheen Weight": "Diffusion", "Anisotropic": "Hair Anisotropy"}
            for row, (out_name, consts) in enumerate(specs):
                y = 900 - row * 620
                node, sel, vals = _menu(g, "SM_" + out_name.replace(" ", ""),
                                        (-1100, y), SHADING_MODELS, "FLOAT", out_name,
                                        selector_src=(gi, "Shading Model"))
                for i, c in enumerate(consts):
                    if c is None:
                        _menu_feed(g, vals, i, gi, wire[out_name])
                    else:
                        _menu_set(vals, i, c)
                _link(g, node, _menu_out(node), go, out_name)
        rscale = _new(g, "ShaderNodeMath", "radius_scale", (-700, -3600),
                      "thickness * subsurface scale")
        _prop(rscale, "operation", "MULTIPLY")
        _link(g, gi, "Thickness", rscale, 0)
        _link(g, gi, "Subsurface Scale", rscale, 1)
        rad = _new(g, "ShaderNodeCombineXYZ", "sss_radius", (-460, -3600),
                   "SSS radius  [ROUGH: no real unit mapping]")
        for i in range(3):
            _link(g, rscale, 0, rad, i)
        _link(g, rad, 0, go, "Subsurface Radius")
    return _rebuild_group("FF7R Shading Model", _b)


# ================================================================= MATERIAL ==

# frame, role, kind, colourspace, neutral default        (PLAN 0.1 / 0.4)
#   kind: 'col' colour | 'val' scalar | 'nrm' loaded tangent-space normal RGB
ROLES = [
    ("Base",       "Color",                    "col", "sRGB",      (0.18, 0.18, 0.18, 1.0)),
    ("Base",       "Normal",                   "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    ("Base",       "Roughness",                "val", "Non-Color", 0.5),
    ("Base",       "Metallic",                 "val", "Non-Color", 0.0),
    ("Base",       "Occlusion",                "val", "Non-Color", 1.0),
    ("Base",       "WideOcclusion",            "val", "Non-Color", 1.0),
    ("Coverage",   "Coverage",                 "val", "Non-Color", 1.0),
    # Fur/card coverage. EID 10082 samples this separately in the alpha-test
    # path; neutral 1 keeps ordinary Coverage-only materials unchanged.
    ("Coverage",   "DetailCoverage",           "val", "Non-Color", 1.0),
    ("Detail",     "Detail",                   "val", "Non-Color", 1.0),
    ("Detail",     "DetailNormal",             "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    ("Detail",     "DetailRoughness",          "val", "Non-Color", 0.0),
    # DetailColor is an Overlay input; middle grey is its neutral element.
    ("Detail",     "DetailColor",              "col", "sRGB",      (0.5, 0.5, 0.5, 1.0)),
    ("Detail",     "DetailOcclusion",          "val", "Non-Color", 1.0),
    ("SkinCloth",  "Pores",                    "val", "Non-Color", 1.0),
    ("SkinCloth",  "Diffusion",                "val", "Non-Color", 1.0),
    ("Segment",    "SegmentLayer0",            "val", "Non-Color", 0.0),
    ("Segment",    "SegmentLayer1",            "val", "Non-Color", 0.0),
    ("Segment",    "ShadingLayer",             "val", "Non-Color", 0.0),
    ("Segment",    "LayeredColor",             "col", "sRGB",      (0.5, 0.5, 0.5, 1.0)),
    ("Emissive",   "Emissive",                 "col", "sRGB",      (0.0, 0.0, 0.0, 1.0)),
    ("Emissive",   "ExtraEmissive",            "col", "sRGB",      (0.0, 0.0, 0.0, 1.0)),
    # BC1 full-RGB direction, not BC5. Flat/default Z is authored, never rebuilt.
    ("Hair",       "WideBentNormal",           "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    ("Transition", "PositiveTransitionNormal", "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    # eye  (PLAN 0.3 -- roles named from the JSON, not the shader slot numbers)
    ("Eye",        "IrisColor",                "col", "sRGB",      (0.3, 0.2, 0.1, 1.0)),
    ("Eye",        "IrisNormal",               "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    ("Eye",        "IrisOcclusion",            "val", "Non-Color", 1.0),
    ("Eye",        "IrisEmissive",             "col", "sRGB",      (0.0, 0.0, 0.0, 1.0)),
    ("Eye",        "ScrelaNormal",             "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
    ("Eye",        "GazeNormal",               "nrm", "Non-Color", (0.5, 0.5, 1.0, 1.0)),
]

# Which UV set each role samples on.  Hair's flow map is the one that must be on
# UV1; getting this wrong looks like "the flow map does nothing" rather than an
# error (see the UV note in ff7r_hair_material.py).
# EID 10082's baked detail textures (coverage, colour, normal, roughness, and
# occlusion) use TEXCOORD_1.xy, the third packed coordinate. Detail itself is
# the placement mask, sampled on the second UV set; the hair flow map is also
# on UV1.
ROLE_UV = {"WideBentNormal": 1, "DetailCoverage": 2, "DetailColor": 2, "DetailNormal": 2,
           "DetailRoughness": 2, "Detail": 1, "DetailOcclusion": 2}

SWITCH_FOR_ROLE = {
    "Color": "Color_", "Normal": "Normal_", "Roughness": "Roughness_",
    "Metallic": "Metallic_", "Occlusion": "Occlusion_",
    "WideOcclusion": "WideOcclusion_", "Coverage": "Coverage_",
    "DetailCoverage": "DetailCoverage_",
    "Detail": "Detail_", "DetailNormal": "DetailNormal_",
    "DetailRoughness": "DetailRoughness_", "DetailColor": "DetailColor_",
    "DetailOcclusion": "DetailOcclusion_", "Pores": "Pores_",
    "Diffusion": "Diffusion_", "SegmentLayer0": "SegmentLayer0_",
    "SegmentLayer1": "SegmentLayer1_", "ShadingLayer": "ShadingLayer_",
    "LayeredColor": "LayeredColor_", "Emissive": "Emissive_",
    "ExtraEmissive": "ExtraEmissive_", "WideBentNormal": "WideBentNormal_",
    "PositiveTransitionNormal": "PositiveTransitionNormal_",
    "IrisColor": "IrisColor_", "IrisNormal": "IrisNormal_",
    "IrisOcclusion": "IrisOcclusion_", "IrisEmissive": "IrisEmissive_",
    "ScrelaNormal": "ScleraNormal_", "GazeNormal": "GazeNormal_",
}

# PLAN 2.13 / 2.15 -- deliberately absent, reported at the end so the omission
# reads as intentional rather than an oversight.
OUT_OF_SCOPE = {
    "vertex domain (needs modifiers/geometry nodes, not a shader tree)":
        ["Deform_", "SoftBody_", "RigidBody_", "Convex_",
         "VertexExpressionPosition_", "ViewCoordinate_"],
    "separate shader family (forward-shaded, one shader per material instance)":
        ["Transparency_", "Transmittance_", "Opacity_", "Thin_", "Thick_",
         "ReflectanceGlass_", "ReflectanceWater_", "ReflectanceCrystal_",
         "PhaseGGlass_", "PhaseGWater_", "PhaseGCrystal_", "Muddiness_"],
    "late compositing pass (reads resolved SceneColor/Depth)":
        ["Hologram_", "Unlit_", "Phantom_", "Ghost_", "HologramEye_"],
    "captured permutation/base pass exists, but its feature math is not traced":
        ["Gradient*_", "BrokenFlow*_", "WideFlow*_", "CoverageIdentifier*_",
         "EyeMigration_", "HologramEye_", "Materia*_",
         "Wetness*_", "Ocean*_", "Flow*_", "Foam_", "Froth_", "Pool_"],
    "no captured carrier shader yet":
        ["Distribution_", "ExtraColor_", "Ghost_", "RigidBody_",
         "ViewCoordinate_"],
    "deferred-light behavior replaced by Blender-native Principled approximation":
        ["SM1 default", "SM3 skin", "SM5 subsurface profile", "SM7 hair",
         "SM8 cloth", "SM9 eye -- see DEFERRED_LIGHTING_HANDOFF.md"],
}


def _tex_slot(t, mat, role, kind, cs, default, frame, x, y, uvnodes,
              switch_source=None):
    """Image node + neutral-constant bypass, built in the MATERIAL tree.

    Groups share their node tree between instances, so an Image Texture inside a
    group would be the same image for every user of it -- texture slots have to
    live out here (see module docstring)."""
    img = _new(t, "ShaderNodeTexImage", "TEX_" + role, (x, y), role, frame)
    # Cubic softens Blender's harsh close-range normal-map filtering.
    _prop(img, "interpolation", "Cubic" if kind == "nrm" else "Linear")
    _prop(img, "extension", "REPEAT")
    dt = "RGBA" if kind in ("col", "nrm") else "FLOAT"
    sw = SWITCH_FOR_ROLE.get(role)
    byp = _new(t, "ShaderNodeMix", "USE_" + role, (x + SLOT_USE_DX, y),
               "%s   neutral / texture   [%s]" % (role, sw or "always"), frame)
    _prop(byp, "data_type", dt)
    _sv(byp, "A", default)
    _sv(byp, "Factor", 0.0)
    _link(t, img, "Color", byp, "B")
    if sw:
        if switch_source is not None:
            _link(t, switch_source, 0, byp, "Factor")
        else:
            _drive(_in(byp, "Factor"), mat, CONTROL_FOR_SWITCH.get(sw, sw))
    uv = uvnodes[ROLE_UV.get(role, 0)]
    _link(t, uv, 0, img, "Vector")
    return img, byp


def _resolve_uv_names(n=4):
    """Auto-populate the UV Coordinate Sets from `bpy.context.active_object`'s
    UV layers, in the order they appear on the mesh -- layer 0 -> Coordinate0_,
    layer 1 -> Coordinate1_, and so on.

    Same convention as `resolve_uv_names()` in ff7r_hair_material.py, extended
    from 2 slots to N: a slot beyond the object's layer count is left blank
    ("") rather than aliased onto another layer.  A blank `uv_map` on a
    ShaderNodeUVMap means "the object's ACTIVE layer" (see the module
    docstring's UV_MAP note), which is the correct graceful degradation --
    pointing it at a named layer that does not exist would not fall back
    gracefully, it would just read zeros. Returns (names, notes); names[i] is
    "" for any slot with no corresponding layer."""
    notes = []
    obj = getattr(bpy.context, "active_object", None)
    layers = []
    if obj is not None and obj.type == "MESH":
        layers = [uvl.name for uvl in obj.data.uv_layers]
    elif obj is not None:
        notes.append("active object %r is not a mesh -- UV Coordinate Sets "
                     "left blank (active layer at render time)" % obj.name)
    else:
        notes.append("no active object -- UV Coordinate Sets left blank "
                     "(active layer at render time); select the target mesh "
                     "and re-run to auto-populate them")
    names = []
    for i in range(n):
        if i < len(layers):
            names.append(layers[i])
            notes.append("Coordinate%d_ auto-resolved to %r (layer %d of %s)"
                         % (i, layers[i], i, obj.name))
        else:
            names.append("")
            if layers:
                notes.append("Coordinate%d_ left blank -- %s has only %d UV "
                             "layer%s" % (i, obj.name, len(layers),
                                         "" if len(layers) == 1 else "s"))
    return names, notes


def _ensure_external_emissive_tint(mat, default=(1.0, 1.0, 1.0)):
    """grp_emissive() reads ExternalEmissive_'s tint via a ShaderNodeAttribute
    on `object["ExternalEmissiveTint"]` (PLAN tier 3 note, confirmed by the
    user 2026-08-24) rather than a driver, because a driver's target is a
    static reference to one object and cannot represent "whichever object
    this shared material is on" -- see that group's docstring.

    A ShaderNodeAttribute reading a missing ID-property returns zero, which
    would silently render as pure black rather than neutral for any object
    nobody has customised yet. This gives every object the material is
    currently assigned to a default of white, ONLY if the property is
    absent -- an artist-authored tint is never touched. Mirrors
    `_resolve_uv_names()`'s reactive, logged-not-silent approach."""
    notes = []
    n = 0
    for obj in bpy.data.objects:
        if not any(slot.material == mat for slot in obj.material_slots):
            continue
        if "ExternalEmissiveTint" not in obj:
            obj["ExternalEmissiveTint"] = default
            n += 1
    if n:
        notes.append('ExternalEmissiveTint defaulted to white on %d object(s) '
                     'that did not have it set' % n)
    else:
        notes.append('no objects use this material yet -- ExternalEmissiveTint '
                     'will read as BLACK (a ShaderNodeAttribute reading a '
                     'missing property returns zero) until set per object, '
                     'e.g. obj["ExternalEmissiveTint"] = (1.0, 1.0, 1.0)')
    return notes


def build():
    # Resolve coordinate names before constructing UV-dependent groups. Hair's
    # slot 14 samples UV1 but UE transforms both slot 2 and slot 14 through the
    # mesh's UV0 tangent frame; relying on Blender's active layer is too fragile.
    uv_names, uv_notes = _resolve_uv_names(4)
    _UV_NOTES.extend(uv_notes)

    # --- groups, in dependency order
    util_tbn()
    util_unpack_bc5()
    util_unpack_rgb()
    util_tangent_to_world()
    hair_t2w = util_tangent_to_world(uv_names[0])
    util_frame_to_world()
    util_slerp()
    util_rnm()
    util_ao_combine()
    util_rough_combine()
    util_f0()
    eye_disc()
    eye_limbal_directional()
    eye_cornea()
    eye_limbal()
    eye_uv()
    grp_eye()
    hair_group = grp_hair_strand(hair_t2w.name, uv_names[0])
    grp_coverage()
    grp_detail_layer()
    grp_segment_layers()
    grp_layer_blend()
    grp_transition_normal()
    grp_emissive()
    grp_blood()
    grp_film()
    grp_shading_model()

    mat = bpy.data.materials.get(MAT_NAME) or bpy.data.materials.new(MAT_NAME)
    if mat.node_tree is None:
        mat.use_nodes = True
    _ensure_props(mat)
    t = mat.node_tree
    # re-running keeps whatever images are already assigned, matched by role
    preserved = {n.name: n.image for n in t.nodes
                 if n.bl_idname == "ShaderNodeTexImage" and n.image
                 and n.name.startswith("TEX_")}
    t.nodes.clear()

    F = {}
    for key, label in [("Props", "MATERIAL CONTROLS  ->  PROPERTY BUNDLE"),
                       ("Coords", "UV COORDINATE SETS"),
                       ("Base", "TEXTURES   Base"),
                       ("Coverage", "TEXTURES   Coverage"),
                       ("Detail", "TEXTURES   Detail layer"),
                       ("SkinCloth", "TEXTURES   Skin / Cloth"),
                       ("Segment", "TEXTURES   Segment / Layer blend"),
                       ("Emissive", "TEXTURES   Emissive"),
                       ("Hair", "TEXTURES   Hair"),
                       ("Transition", "TEXTURES   Transition normal"),
                       ("Eye", "TEXTURES   Eye"),
                       ("Shade", "SHADING"),
                       ("EyePath", "SHADING   Eye  (SM 9)"),
                       ("HairPath", "SHADING   Hair  (SM 7)"),
                       ("Out", "OUTPUT")]:
        F[key] = _frame(t, "FRAME_" + key, label)

    # ---- driven parameter nodes ---------------------------------------
    # Every switch and float parameter is a Value node driven from the
    # material's custom properties, so the graph mirrors the property panel.
    P = {}
    property_sources = {}
    property_types = {}
    for i, (name, members, _default) in enumerate(SWITCH_CONTROLS):
        node_name = "SW_" + members[0]
        col = min(i // SWITCH_COLUMN_LEN, len(SWITCH_COLUMNS) - 1)
        cx, cy = SWITCH_COLUMNS[col]
        row = i - col * SWITCH_COLUMN_LEN
        v = _new(t, "ShaderNodeValue", node_name,
                 (cx, cy - row * PROP_ROW_PITCH),
                 " + ".join(members), F["Props"])
        _drive(v.outputs[0], mat, name)
        property_sources[name] = v
        property_types[name] = "BOOLEAN"
        for member in members:
            P[member] = v
    for i, (name, _d, _mn, _mx, _desc) in enumerate(ENUM_PROPS):
        if name == "Shading Model" and HAS_MANUAL_MENU_SWITCH:
            v = _new(t, MENU_INPUT_IDNAME, "E_Shading_Model",
                     (ENUM_COLUMN[0], ENUM_COLUMN[1] - i * PROP_ROW_PITCH),
                     "Shading Model", F["Props"])
        else:
            v = _new(t, "ShaderNodeValue", "E_" + name.replace(" ", "_"),
                     (ENUM_COLUMN[0], ENUM_COLUMN[1] - i * PROP_ROW_PITCH),
                     name, F["Props"])
            _drive(v.outputs[0], mat, name)
        P[name] = v
        property_sources[name] = v
        property_types[name] = ("MENU" if name == "Shading Model" and
                                HAS_MANUAL_MENU_SWITCH else "FLOAT")
    for i, (name, _d, _mn, _mx, _desc) in enumerate(FLOAT_PROPS):
        v = _new(t, "ShaderNodeValue", "P_" + name.replace(" ", "_"),
                 (FLOAT_COLUMN[0], FLOAT_COLUMN[1] - i * PROP_ROW_PITCH),
                 name, F["Props"])
        _drive(v.outputs[0], mat, name)
        P[name] = v
        property_sources[name] = v
        property_types[name] = "FLOAT"

    # ---- UV sets --------------------------------------------------------
    uv = {}
    for i in range(4):
        uv[i] = _new(t, "ShaderNodeUVMap", "UV%d" % i,
                     (UV_COLUMN[0], UV_COLUMN[1] - i * UV_ROW_PITCH),
                     "Coordinate%d_" % i, F["Coords"])
        if uv_names[i]:
            _prop(uv[i], "uv_map", uv_names[i])

    # ---- texture slots ------------------------------------------------
    slots, ycur = {}, {}
    for frame_key, role, kind, cs, default in ROLES:
        ox, oy = FRAME_ORIGIN[frame_key]
        y = ycur.get(frame_key, oy + FRAME_PAD[1])
        ycur[frame_key] = y - SLOT_ROW_PITCH
        slots[role] = _tex_slot(t, mat, role, kind, cs, default, F[frame_key],
                                ox + FRAME_PAD[0], y, uv,
                                P.get(SWITCH_FOR_ROLE.get(role)))

    def S(role):
        return slots[role][1]

    # ---- shared shading -----------------------------------------------
    # Hand-arranged in 4.5 (see the node layout block): the normal chain runs
    # along the top of the Shade frame, the coverage/alpha chain along the
    # bottom, and the per-pixel feature groups stack down its right-hand edge.
    nbase = _grp(t, "UE Unpack Normal (RG)", "N_Base", (1993, 1138),
                 "Normal  (BC5 -> vector)", F["Shade"])
    ndet = _grp(t, "UE Unpack Normal (RG)", "N_Detail", (1985, 167),
                "DetailNormal  (BC5 -> vector)", F["Shade"])
    ntrans = _grp(t, "UE Unpack Normal (RG)", "N_Transition", (2203, 342),
                  "PositiveTransitionNormal  (BC5)", F["Shade"])
    detail = _grp(t, "FF7R Detail Layer", "DetailLayer", (2204, 825), "", F["Shade"])
    trans = _grp(t, "FF7R Transition Normal", "TransitionNormal", (2442, 525),
                 "", F["Shade"])
    nworld = _grp(t, "UE Tangent To World (mikktspace)", "N_SharedWorld",
                  (2655, 680), "final tangent normal -> world", F["Shade"])
    seg = _grp(t, "FF7R Segment Layers", "SegmentLayers", (2448, 1126), "", F["Shade"])
    layer = _grp(t, "FF7R Layer Blend", "LayerBlend", (2657, 1012), "", F["Shade"])
    coverage_detail = _new(t, "ShaderNodeMath", "CoverageWithDetail", (1797, -197),
                           "Coverage × DetailCoverage (fur/card alpha)", F["Shade"])
    _prop(coverage_detail, "operation", "MULTIPLY")
    cov = _grp(t, "FF7R Coverage", "Coverage", (1979, -209), "", F["Shade"])
    shadow_path = _new(t, "ShaderNodeLightPath", "DetailCoverageShadowRay",
                       (1981, 31), "Is Shadow Ray", F["Shade"])
    shadow_gate = _new(t, "ShaderNodeMath", "DetailCoverageShadowGate",
                       (2189, 197), "DetailCoverage_ × Is Shadow Ray", F["Shade"])
    _prop(shadow_gate, "operation", "MULTIPLY")
    alpha_shadow = _new(t, "ShaderNodeMath", "AlphaWithoutDetailCoverageShadow",
                        (2186, 23), "alpha - DetailCoverage_ shadow ray", F["Shade"])
    _prop(alpha_shadow, "operation", "SUBTRACT")
    _prop(alpha_shadow, "use_clamp", True)
    backface = _new(t, "ShaderNodeNewGeometry", "BackfaceCulling",
                    (2415, 205), "Backfacing", F["Shade"])
    alpha_backface = _new(t, "ShaderNodeMath", "AlphaWithoutBackfaces",
                          (2656, 547), "alpha - backfacing", F["Shade"])
    _prop(alpha_backface, "operation", "SUBTRACT")
    _prop(alpha_backface, "use_clamp", True)
    blood = _grp(t, "FF7R Blood", "Blood", (2654, 14), "", F["Shade"])
    film = _grp(t, "FF7R Film", "Film", (2652, -127), "", F["Shade"])
    f0 = _grp(t, "FF7R Util/F0 Remap", "F0", (2648, -336), "", F["Shade"])
    emis = _grp(t, "FF7R Emissive", "Emissive", (2644, -484), "", F["Shade"])
    smodel = _grp(t, "FF7R Shading Model", "ShadingModel", (2655, 366), "", F["Shade"])

    _link(t, S("Normal"), "Result", nbase, "RG")
    _link(t, S("DetailNormal"), "Result", ndet, "RG")
    _link(t, S("PositiveTransitionNormal"), "Result", ntrans, "RG")

    _link(t, S("Color"), "Result", detail, "Base Color")
    _link(t, nbase, "Normal", detail, "Base Normal")
    _link(t, S("Roughness"), "Result", detail, "Base Roughness")
    _link(t, S("WideOcclusion"), "Result", detail, "Base AO")
    _link(t, S("DetailColor"), "Result", detail, "Detail Color")
    _link(t, ndet, "Normal", detail, "Detail Normal")
    _link(t, S("DetailRoughness"), "Result", detail, "Detail Roughness")
    _link(t, S("DetailOcclusion"), "Result", detail, "Detail AO")
    _link(t, S("Detail"), "Result", detail, "Detail Mask")

    # Character variants carry Detail_; the current MEC/environment overlay
    # variants do not. Enable from any actual detail-role switch so those exact
    # parent switch sets work without inventing a Detail_ flag they never had.
    detail_flags = ["Detail_", "DetailColor_", "DetailMetallic_",
                    "DetailNormal_", "DetailRoughness_", "DetailOcclusion_"]
    # One rung per extra flag, climbing the left edge of the Shade frame.
    DETAIL_ENABLE_POS = [(1987, 995), (1981, 826), (1976, 659),
                         (1981, 499), (1990, 325)]
    detail_enable = P[detail_flags[0]]
    for j, flag in enumerate(detail_flags[1:]):
        mx = _new(t, "ShaderNodeMath", "DetailEnable%d" % j,
                  DETAIL_ENABLE_POS[min(j, len(DETAIL_ENABLE_POS) - 1)],
                  "OR detail-role switches", F["Shade"])
        _prop(mx, "operation", "MAXIMUM")
        _link(t, detail_enable, 0, mx, 0)
        _link(t, P[flag], 0, mx, 1)
        detail_enable = mx
    _link(t, detail_enable, 0, detail, "Enable")

    # Isotropy_ identifies the confirmed MEC/environment path and forces exact
    # RNM option 2. The manual selector remains useful for non-Isotropy previews.
    rnm_index = _new(t, "ShaderNodeMath", "RNMWhenIsotropy", (1805, -34),
                     "Isotropy_ * 2", F["Shade"])
    _prop(rnm_index, "operation", "MULTIPLY")
    _sv(rnm_index, 1, 2.0)
    detail_mode = _new(t, "ShaderNodeMath", "EffectiveDetailBlend", (1980, -31),
                       "max(manual blend, Isotropy RNM)", F["Shade"])
    _prop(detail_mode, "operation", "MAXIMUM")
    _link(t, P["Isotropy_"], 0, rnm_index, 0)
    _link(t, P["Detail Blend"], 0, detail_mode, 0)
    _link(t, rnm_index, 0, detail_mode, 1)
    _link(t, detail_mode, 0, detail, "Detail Blend")
    _link(t, P["Detail Normal Strength"], 0, detail, "Normal Strength")

    _link(t, detail, "Normal", trans, "Base Normal")
    _link(t, ntrans, "Normal", trans, "Transition Normal")
    _link(t, P["PositiveTransitionNormal_"], 0, trans, "Enable")
    _link(t, P["Vertex Expression Source"], 0, trans, "Source")
    _link(t, trans, "Normal", nworld, "Vector")

    _link(t, detail, "Base Color", seg, "Base Color")
    _link(t, S("SegmentLayer0"), "Result", seg, "Mask 0")
    _link(t, S("SegmentLayer1"), "Result", seg, "Mask 1")
    _link(t, P["Segmented_"], 0, seg, "Enable")

    _link(t, seg, "Base Color", layer, "Base Color")
    _link(t, S("LayeredColor"), "Result", layer, "Layer Color")
    _link(t, detail, "Roughness", layer, "Base Roughness")
    _link(t, S("ShadingLayer"), "Result", layer, "Mask")
    _link(t, P["Layered_"], 0, layer, "Enable")

    # The EID 10082 fur base pass samples DetailCoverage separately on the
    # detail coordinate and consumes it in its alpha-test branch. A product
    # makes PC0004_00_Fur_A the effective card mask while retaining the base
    # Coverage map; DetailCoverage's neutral bypass is 1 when unavailable.
    _link(t, S("Coverage"), "Result", coverage_detail, 0)
    _link(t, S("DetailCoverage"), "Result", coverage_detail, 1)
    _link(t, coverage_detail, 0, cov, "Coverage")
    _link(t, P["Object Fade"], 0, cov, "Object Fade")
    _link(t, P["Opacity Clip"], 0, cov, "Clip")
    _link(t, P["Coverage_"], 0, cov, "Enable")
    _link(t, P["Coverage Mode"], 0, cov, "Mode")
    _link(t, shadow_path, "Is Shadow Ray", shadow_gate, 0)
    _link(t, P["DetailCoverage_"], 0, shadow_gate, 1)
    _link(t, cov, "Alpha", alpha_shadow, 0)
    _link(t, shadow_gate, 0, alpha_shadow, 1)
    _link(t, alpha_shadow, 0, alpha_backface, 0)
    _link(t, backface, "Backfacing", alpha_backface, 1)

    _link(t, P["OxygenSaturation"], 0, blood, "Oxygen Saturation")
    _link(t, P["FilmThickness"], 0, film, "Thickness")
    _link(t, P["FilmStructure"], 0, film, "Structure")
    _link(t, P["Film_"], 0, film, "Enable")
    _link(t, P["GBufferA.a"], 0, f0, "GBufferA.a")

    _link(t, S("Emissive"), "Result", emis, "Emissive")
    _link(t, S("ExtraEmissive"), "Result", emis, "Extra Emissive")
    _link(t, P["Emissive_"], 0, emis, "Emissive_")
    _link(t, P["ExtraEmissive_"], 0, emis, "ExtraEmissive_")
    _link(t, P["ExternalEmissive_"], 0, emis, "ExternalEmissive_")

    _link(t, S("Diffusion"), "Result", smodel, "Diffusion")
    _link(t, P["Thickness"], 0, smodel, "Thickness")
    _link(t, P["Subsurface Scale"], 0, smodel, "Subsurface Scale")
    _link(t, P["Hair Anisotropic"], 0, smodel, "Hair Anisotropy")

    # ---- eye path -----------------------------------------------------
    # The EyePath and HairPath frames sit above the Shade frame in the arranged
    # layout, in their own y bands: the alternate shading models reuse Shade's
    # x band, so separating the three frames is what keeps them from being
    # drawn on top of each other.
    # Standalone ff7r_eye_material_v2.py slot order is authoritative here:
    #   0 Color, 2 Normal (sclera), 5 IrisEmissive, 7 ScrelaNormal
    #   (secondary), 8 GazeNormal (N8/cornea), 9 IrisColor,
    #   10 IrisNormal, 11 IrisOcclusion.
    # Its slots 2/7/8 pass through DirectX Normal Map nodes before entering the
    # eye maths, so these three values are world-space -- unlike the shared
    # tangent-space normal layering path above.
    nsclera = _new(t, "ShaderNodeNormalMap", "N_Sclera_World", (2318, 2324),
                   "Normal / slot 2 -> sclera world normal", F["EyePath"])
    nsecondary = _new(t, "ShaderNodeNormalMap", "N_Secondary_World",
                      (2146, 2202),
                      "ScrelaNormal / slot 7 -> secondary world normal",
                      F["EyePath"])
    ncornea = _new(t, "ShaderNodeNormalMap", "N_Cornea_World",
                   (2147, 2048),
                   "GazeNormal / slot 8 -> N8 cornea world normal",
                   F["EyePath"])
    for eye_nmap in (nsclera, nsecondary, ncornea):
        _prop(eye_nmap, "space", "TANGENT")
        if uv_names[0]:
            _prop(eye_nmap, "uv_map", uv_names[0])
        _convention(eye_nmap, "DIRECTX")
        _sv(eye_nmap, "Strength", 1.0)
    niris = _grp(t, "UE Unpack Normal (RG)", "N_Iris", (2318, 2008),
                 "IrisNormal (BC5)", F["EyePath"])
    # split geometry (produces Iris UV) from colour (consumes iris textures) --
    # a single group doing both is a node-graph cycle, see eye_uv()'s docstring
    eyeuv = _grp(t, "FF7R Eye UV", "EyeUV", (2319, 1887),
                "eye disc + cornea (Iris UV)", F["EyePath"])
    # Held clear of EyeUV's own row: the eye group is ~530 tall, so it takes
    # the column to the right rather than the slot underneath.
    eye = _grp(t, "FF7R Eye", "Eye", (2502, 2300), "eye path (SM 9)",
               F["EyePath"])
    _link(t, S("Normal"), "Result", nsclera, "Color")
    _link(t, S("ScrelaNormal"), "Result", nsecondary, "Color")
    _link(t, S("GazeNormal"), "Result", ncornea, "Color")
    _link(t, S("IrisNormal"), "Result", niris, "RG")
    _link(t, uv[0], "UV", eyeuv, "UV0")
    _link(t, ncornea, "Normal", eyeuv, "Cornea Normal Map")
    _link(t, P["Pupil Dilation"], 0, eyeuv, "Pupil Dilation")
    _link(t, S("Color"), "Result", eye, "Sclera Color")
    _link(t, S("IrisColor"), "Result", eye, "Iris Color")
    _link(t, S("IrisOcclusion"), "Result", eye, "Iris Occlusion")
    _link(t, S("IrisEmissive"), "Result", eye, "Iris Emissive")
    _link(t, nsclera, "Normal", eye, "Sclera Normal")
    _link(t, nsecondary, "Normal", eye, "Gaze Normal")
    _link(t, ncornea, "Normal", eye, "Cornea Normal Map")
    _link(t, niris, "Normal", eye, "Iris Normal")
    _link(t, eyeuv, "Cornea Normal", eye, "Cornea Normal")
    _link(t, eyeuv, "uvC2", eye, "uvC2")
    _link(t, eyeuv, "r", eye, "r")
    _link(t, eyeuv, "Sclera Mask", eye, "Sclera Mask")
    _link(t, eyeuv, "T2", eye, "T2")
    _link(t, eyeuv, "B2", eye, "B2")
    # Deliberately leave Light Position unlinked.  It is an artist hook for a
    # driven Empty/bone position that fakes the limbal-occlusion source.
    _link(t, P["IrisEmissive_"], 0, eye, "IrisEmissive_")
    _link(t, P["Eye Limbal Amount"], 0, eye, "Limbal Amount")
    # the iris textures sample on the parallax-corrected UV eyeuv produces.
    # eyeuv has no path back into eye or into these texture nodes, so this
    # closes the loop the OTHER way round without forming a cycle.
    for role in ("IrisColor", "IrisNormal", "IrisOcclusion", "IrisEmissive"):
        _link(t, eyeuv, "Iris UV", slots[role][0], "Vector")

    # ---- hair path ----------------------------------------------------
    nflow = _grp(t, "UE Unpack Normal (RGB)", "N_Flow", (1933, 1515),
                 "WideBentNormal / flow (UV1, BC1 full RGB)", F["HairPath"])
    hair = _grp(t, hair_group.name, "HairStrand", (2583, 1515),
                "hair path (SM 7)", F["HairPath"])
    _link(t, S("WideBentNormal"), "Result", nflow, "RGB")
    _link(t, nbase, "Normal", hair, "Strand Map")
    _link(t, nflow, "Normal", hair, "Flow Map")
    _link(t, S("Roughness"), "Result", hair, "Roughness Map")
    _link(t, P["Hair Flow Influence"], 0, hair, "Flow Influence")
    _link(t, P["Hair Min VdotT"], 0, hair, "Min VdotT")
    _link(t, P["Hair Dither Sign"], 0, hair, "Dither Sign")
    _link(t, P["Hair Roughness Curvature"], 0, hair, "Roughness Curvature")

    # ---- per-model routing --------------------------------------------
    # Blender 5.2 uses one Constant Menu -> FF7R Shading Model Menu Switch.
    # The group's one-hot Hair/Eye outputs drive these small routing mixes, so
    # there is exactly one enum definition and one user-facing dropdown.
    if HAS_MANUAL_MENU_SWITCH:
        colsel = _new(t, "ShaderNodeMix", "SM_BaseColor",
                      _out_pos("SM_BaseColor"),
                      "Eye selects eye base colour", F["Out"])
        _prop(colsel, "data_type", "RGBA")
        _link(t, smodel, "Is Eye", colsel, "Factor")
        _link(t, layer, "Base Color", colsel, "A")
        _link(t, eye, "Base Color", colsel, "B")

        nrmhair = _new(t, "ShaderNodeMix", "SM_Normal_Hair",
                       _out_pos("SM_Normal_Hair"),
                       "Hair selects strand normal", F["Out"])
        _prop(nrmhair, "data_type", "VECTOR")
        _link(t, smodel, "Is Hair", nrmhair, "Factor")
        _link(t, nworld, "Vector", nrmhair, "A")
        _link(t, hair, "Normal", nrmhair, "B")
        nrmsel = _new(t, "ShaderNodeMix", "SM_Normal", _out_pos("SM_Normal"),
                      "Eye selects refracted eye normal", F["Out"])
        _prop(nrmsel, "data_type", "VECTOR")
        _link(t, smodel, "Is Eye", nrmsel, "Factor")
        _link(t, nrmhair, "Result", nrmsel, "A")
        _link(t, eye, "Normal", nrmsel, "B")

        rgsel = _new(t, "ShaderNodeMix", "SM_Roughness",
                     _out_pos("SM_Roughness"),
                     "Hair selects strand roughness", F["Out"])
        _prop(rgsel, "data_type", "FLOAT")
        _link(t, smodel, "Is Hair", rgsel, "Factor")
        _link(t, layer, "Roughness", rgsel, "A")
        _link(t, hair, "Roughness", rgsel, "B")

        try:
            t.links.new(P["Shading Model"].outputs[0],
                        _in(smodel, "Shading Model"))
            P["Shading Model"].value = SHADING_MODELS[int(mat.get("Shading Model", 0))]
        except (IndexError, TypeError, ValueError):
            P["Shading Model"].value = SHADING_MODELS[0]
        except Exception:
            log("link refused: shared Shading Model menu")
    else:
        # Older builds retain the numeric property-driven ladders.
        colsel, colsel_in, colvals = _menu(
            t, "SM_BaseColor", MENU_ANCHOR["SM_BaseColor"], SHADING_MODELS,
            "RGBA", "Base Colour by shading model", F["Out"])
        nrmsel, nrmsel_in, nrmvals = _menu(
            t, "SM_Normal", MENU_ANCHOR["SM_Normal"], SHADING_MODELS,
            "VECTOR", "Normal by shading model", F["Out"])
        rgsel, rgsel_in, rgvals = _menu(
            t, "SM_Roughness", MENU_ANCHOR["SM_Roughness"],
            SHADING_MODELS, "FLOAT", "Roughness by shading model", F["Out"])
        for i in range(7):
            _menu_feed(t, colvals, i, layer, "Base Color")
            _menu_feed(t, nrmvals, i, nworld, "Vector")
            _menu_feed(t, rgvals, i, layer, "Roughness")
        _menu_feed(t, colvals, 5, eye, "Base Color")
        _menu_feed(t, nrmvals, 5, eye, "Normal")
        _menu_feed(t, nrmvals, 3, hair, "Normal")
        _menu_feed(t, rgvals, 3, hair, "Roughness")
        model_sockets = (colsel_in, nrmsel_in, rgsel_in,
                         _in(smodel, "Shading Model"))
        for sock_ in model_sockets:
            if sock_ is None:
                continue
            try:
                t.links.new(P["Shading Model"].outputs[0], sock_)
            except Exception:
                _drive(sock_, mat, "Shading Model")

    # blood replaces base colour wholesale when Blood_ is on
    bloodmix = _new(t, "ShaderNodeMix", "BloodOverride", _out_pos("BloodOverride"),
                    "Blood_ overrides base colour", F["Out"])
    _prop(bloodmix, "data_type", "RGBA")
    _link(t, colsel, _menu_out(colsel), bloodmix, "A")
    _link(t, blood, "Base Color", bloodmix, "B")
    _link(t, P["Blood_"], 0, bloodmix, "Factor")

    # ---- one Principled, parameterised (PLAN 2.1) ---------------------
    bsdf = _new(t, "ShaderNodeBsdfPrincipled", "Principled BSDF",
                _out_pos("Principled BSDF"), "RMI_Surface", F["Out"])
    _expand_panels(bsdf)
    _sv(bsdf, "IOR", 1.3)
    _sv(bsdf, "Subsurface Anisotropy", -0.5)
    out = _new(t, "ShaderNodeOutputMaterial", "Material Output",
               _out_pos("Material Output"), "", F["Out"])
    _link(t, bloodmix, "Result", bsdf, "Base Color")
    _link(t, nrmsel, _menu_out(nrmsel), bsdf, "Normal")
    _link(t, rgsel, _menu_out(rgsel), bsdf, "Roughness")
    _link(t, eye, "Coat Normal", bsdf, "Coat Normal")
    _link(t, alpha_backface, 0, bsdf, "Alpha")
    _link(t, f0, "Specular IOR Level", bsdf, "Specular IOR Level")
    _link(t, hair, "Tangent", bsdf, "Tangent")
    _link(t, film, "Thin Film Thickness", bsdf, "Thin Film Thickness")
    _link(t, film, "Thin Film IOR", bsdf, "Thin Film IOR")
    smodel_params = ["Subsurface Weight", "Sheen Weight", "Anisotropic",
                     "Coat Weight", "Subsurface Radius", "Diffuse Roughness"]
    if THIN_WALL_SOCKET:
        smodel_params.append(THIN_WALL_SOCKET)
    for s in smodel_params:
        _link(t, smodel, s, bsdf, s)
    _link(t, P["Sheen Roughness"], 0, bsdf, "Sheen Roughness")

    # emission: shared emissive plus the eye's unshadowed iris glow
    emsum = _new(t, "ShaderNodeMix", "EmissionSum", _out_pos("EmissionSum"),
                 "emissive + iris glow", F["Out"])
    _prop(emsum, "data_type", "RGBA")
    _prop(emsum, "blend_type", "ADD")
    _sv(emsum, "Factor", 1.0)
    _link(t, emis, "Emission", emsum, "A")
    _link(t, eye, "Emission", emsum, "B")
    _link(t, emsum, "Result", bsdf, "Emission Color")
    embst = _new(t, "ShaderNodeMath", "EmissionStrength", _out_pos("EmissionStrength"),
                 "max(emissive strength, unlit boost)", F["Out"])
    _prop(embst, "operation", "MAXIMUM")
    _link(t, emis, "Strength", embst, 0)
    _link(t, smodel, "Emission Boost", embst, 1)
    _link(t, embst, 0, bsdf, "Emission Strength")

    # metallic: map * Metallic_ * per-model scale, with Layered_ able to override
    mg = _new(t, "ShaderNodeMath", "MetallicGate", _out_pos("MetallicGate"),
              "Metallic map * Metallic_", F["Out"])
    _prop(mg, "operation", "MULTIPLY")
    _link(t, S("Metallic"), "Result", mg, 0)
    _link(t, P["Metallic_"], 0, mg, 1)
    mg2 = _new(t, "ShaderNodeMath", "MetallicModelGate", _out_pos("MetallicModelGate"),
               "* per-model scale", F["Out"])
    _prop(mg2, "operation", "MULTIPLY")
    _link(t, mg, 0, mg2, 0)
    _link(t, smodel, "Metallic Scale", mg2, 1)
    _link(t, mg2, 0, bsdf, "Metallic")
    _link(t, bsdf, 0, out, "Surface")

    _bundle_material_properties(t, property_sources, property_types, F["Props"])
    _fix_conventions(t)
    _hide_unused_system_outputs(t)
    for name, img in preserved.items():
        n = t.nodes.get(name)
        if n is not None:
            n.image = img
    _OBJ_PROP_NOTES.extend(_ensure_external_emissive_tint(mat))
    return mat


# Guarded so that OTHER scripts can `importlib` this file to reuse its tables
# (SWITCH_CONTROLS, ROLES, SWITCH_FOR_ROLE, ...) without triggering build().
# That distinction matters: build() calls every util_*()/grp_*() function,
# each of which runs _rebuild_group() on a SHARED node group ("FF7R Eye",
# "UE Unpack Normal (RG)", etc.) -- and _rebuild_group() wipes and recreates
# that group's interface sockets on an already-existing group. Any OTHER
# material in the .blend that also has a ShaderNodeGroup instance pointed at
# that same shared group (which is by design -- see the module docstring)
# loses every link into/out of that instance the moment the interface is
# rebuilt, because the old socket identities are gone. So merely importing
# this file used to be destructive to every hand-built variant copy in the
# file, not just to FF7R_RMI_Surface_Master itself. See
# ff7r_rmi_surface_variant.py for the companion script that relies on this.
if __name__ == "__main__":
    mat = build()
    _ng = [g for g in bpy.data.node_groups
           if g.name.startswith("FF7R") or g.name.startswith("UE ")]
    print("=" * 72)
    print("Built  %r" % mat.name)
    print("  Blender %s   MenuSwitch=%s   NormalMap.convention=%s"
          % (bpy.app.version_string, HAS_MENU_SWITCH, HAS_CONVENTION))
    print("  material nodes: %-5d node groups: %-4d texture slots: %d"
          % (len(mat.node_tree.nodes), len(_ng), len(ROLES)))
    print("  custom properties: %d bool controls for %d known flags + "
          "%d int selectors + %d floats"
          % (len(SWITCH_CONTROLS), len(KNOWN_SWITCHES),
             len(ENUM_PROPS), len(FLOAT_PROPS)))
    print("     (Material Properties > Custom Properties)")
    if _UV_NOTES:
        print("  UV Coordinate Sets:")
        for m_ in _UV_NOTES:
            print("     " + m_)
    if _OBJ_PROP_NOTES:
        print("  Object custom properties:")
        for m_ in _OBJ_PROP_NOTES:
            print("     " + m_)
    if not HAS_MENU_SWITCH:
        print("  NOTE: menus built as property-driven compare+mix ladders -- not "
              "a version gate, see _detect_menu_switch()'s docstring for why.")
    if _CONVENTION_FIXED:
        print("  NOTE: Blender < 5.2 -- spliced explicit green-flip maths before %s"
              % ", ".join(sorted(set(_CONVENTION_FIXED))))
    print("  Not reconstructed / deliberately delegated:")
    for why, items in OUT_OF_SCOPE.items():
        print("     %s\n        %s" % (why, ", ".join(items)))
    if _LOG:
        print("  %d issue(s):" % len(_LOG))
        for m in _LOG[:60]:
            print("     " + m)
    else:
        print("  no issues.")
print("=" * 72)
