"""
material.py
Hash-to-path lookup, UE texture helpers, and Principled BSDF node-tree builders.

Sections
--------
1. Hash lookup (texture_hashes.csv fallback table)
2. Texture path constants and helpers
3. Node-tree builders:
     _setup_nodes_standard   — 7-slot single-layer layout
     _setup_nodes_two_layer  — the real 14-slot layout, transcribed from the
                               MEC base-pass pixel shader
     _setup_nodes_9slot      — legacy, de-duplicated slot lists (fallback only)
     _setup_nodes_10slot     — legacy (fallback only)
     _setup_nodes_extended   — legacy 11/12-slot (fallback only)
   Dispatcher:
     setup_material_nodes
"""

import os
import csv
from contextlib import contextmanager
import bpy


# ============================================================
#  1. Hash lookup
# ============================================================

_CSV_NAME = "texture_hashes.csv"

# Module-level table; populated by load_hash_table()
_hash_table: dict[str, str] = {}
_image_loader_override = None


@contextmanager
def image_loader_override(loader):
    """Temporarily resolve UE texture paths with *loader* instead of disk lookup."""
    global _image_loader_override
    previous = _image_loader_override
    _image_loader_override = loader
    try:
        yield
    finally:
        _image_loader_override = previous


def has_image_loader_override() -> bool:
    return _image_loader_override is not None


def load_hash_table(addon_dir: str | None = None) -> None:
    """Read texture_hashes.csv from *addon_dir* (defaults to this file's directory).

    Safe to call multiple times — the table is rebuilt on each call so edits
    take effect without restarting Blender.
    """
    global _hash_table
    _hash_table = {}

    if addon_dir is None:
        addon_dir = os.path.dirname(__file__)

    csv_path = os.path.join(addon_dir, _CSV_NAME)
    if not os.path.isfile(csv_path):
        return

    count = 0
    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            for row in csv.reader(f):
                if not row or row[0].strip().startswith('#'):
                    continue
                if len(row) < 2:
                    continue
                filename = row[0].strip()
                hash_val = row[1].strip().upper()
                if filename and hash_val:
                    _hash_table[hash_val] = filename
                    count += 1
        print(f"[ff7r_umap] Loaded {count} hash→path entries from {_CSV_NAME}")
    except Exception as err:
        print(f"[ff7r_umap] Warning: could not read {csv_path}: {err}")


def resolve_hash(hash_str: str) -> str | None:
    """Return the UE asset path for *hash_str*, or None if not in the table.

    Matched case-insensitively after normalising to uppercase.
    """
    if not hash_str:
        return None
    return _hash_table.get(hash_str.upper())


# ============================================================
#  2. Texture path constants and helpers
# ============================================================

# /Game/Renderer/Texture paths that resolve to plain white or black.
_CONST_WHITE = frozenset({
    '/game/renderer/texture/ffffffff_bc4',
    '/game/renderer/texture/ffffffff_srgb',
    '/game/renderer/texture/ffffffff_hdr',
    '/game/renderer/texture/xxxx7bff_hdr',
})
_CONST_BLACK = frozenset({
    '/game/renderer/texture/000000ff_bc4',
})


def _strip_ue_suffix(path: str) -> str:
    """Remove the UE Package.ObjectName suffix from an asset path.

        /Game/Renderer/Texture/000000FF_BC4.000000FF_BC4
        →  /Game/Renderer/Texture/000000FF_BC4
    """
    slash = path.rfind('/')
    dot   = path.rfind('.')
    if dot > slash > -1:
        return path[:dot]
    return path


def _is_const_white(path: str) -> bool:
    return bool(path) and _strip_ue_suffix(path).lower() in _CONST_WHITE


def _is_const_black(path: str) -> bool:
    return bool(path) and _strip_ue_suffix(path).lower() in _CONST_BLACK


def _is_renderer_texture(path: str) -> bool:
    """Return True for renderer constants/placeholders, which are not texture inputs."""
    return bool(path) and _strip_ue_suffix(path).lower().startswith('/game/renderer/texture/')


def _usable_texture_path(path: str | None) -> str | None:
    """Treat /Game/Renderer/Texture entries as placeholders, not texture inputs."""
    if path and not _is_renderer_texture(path):
        return path
    return None


def _slot(hashes: list, index: int) -> str | None:
    return _usable_texture_path(hashes[index]) if index < len(hashes) else None


def load_image(game_path: str, tex_root: str, tex_ext: str,
               tex_index: "dict[str, str] | None" = None):
    """Resolve a /Game/... asset path to a local file and return a bpy.types.Image.

    When *tex_index* is provided (filename-only mode) the full /Game/ path is
    ignored and the basename is looked up directly in the pre-built index.
    Returns None when tex_root is empty or the file cannot be found.
    """
    if not game_path:
        return None
    if _strip_ue_suffix(game_path).lower().startswith('/game/renderer/texture/'):
        return None
    if _image_loader_override is not None:
        return _image_loader_override(game_path)
    if not tex_root:
        return None

    if tex_index is not None:
        # Filename-only mode: extract the last path segment (no extension) and look it up.
        stem = _strip_ue_suffix(game_path).rstrip('/').rsplit('/', 1)[-1]
        ext  = tex_ext.lstrip('.')
        key  = (stem + '.' + ext).lower()
        full = tex_index.get(key)
        if full is None:
            return None
    else:
        p = _strip_ue_suffix(game_path)
        if p.lower().startswith('/game/'):
            p = p[6:]
        else:
            return None
        rel  = p.replace('/', os.sep)
        ext  = tex_ext.lstrip('.')
        full = os.path.join(tex_root, rel + '.' + ext)
        if not os.path.isfile(full):
            return None

    name = os.path.basename(full)
    img  = bpy.data.images.get(name)
    if img is None:
        try:
            img = bpy.data.images.load(full)
        except Exception as err:
            print(f"  Warning: could not load texture '{full}': {err}")
            return None
    return img


def build_texture_index(tex_root: str, tex_ext: str) -> "dict[str, str]":
    """Walk *tex_root* and return a dict mapping ``filename.ext`` (lowercase) -> absolute path.

    Called once per import run when the 'match by filename' preference is on.
    """
    index: dict[str, str] = {}
    if not tex_root or not os.path.isdir(tex_root):
        return index
    ext_lower = ('.' + tex_ext.lstrip('.')).lower()
    for dirpath, _dirnames, filenames in os.walk(tex_root):
        for fname in filenames:
            if fname.lower().endswith(ext_lower):
                index[fname.lower()] = os.path.join(dirpath, fname)
    print(f"[ff7r_umap] Texture index built: {len(index)} file(s) under '{tex_root}'")
    return index


# ============================================================
#  3. Node-tree builders
# ============================================================

# -- Shared helpers ------------------------------------------

def _activate_nodes(mat) -> None:
    if bpy.app.version < (5, 0, 0):
        mat.use_nodes = True


def _make_bsdf(nt, links, out_node, label: str, ior: float):
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.label = label
    links.new(bsdf.outputs['BSDF'], out_node.inputs['Surface'])
    bsdf.inputs['IOR'].default_value = ior
    return bsdf


def _set_normal_convention(nm_node) -> None:
    try:
        nm_node.convention = "DIRECTX"
    except AttributeError:
        pass


def _make_y_invert_curves(nt, loc):
    """RGB Curves node that inverts only the Green channel.

    Used on Blender < 5.2 which lacks the DirectX normal-map convention
    toggle, so the Y channel must be flipped manually.  An RGB Curves
    node keeps the node-count low and fits neatly between the texture
    column and the Normal Map node.
    """
    crv = nt.nodes.new('ShaderNodeRGBCurve')
    crv.label    = 'DX Normal Y-flip'
    crv.location = loc
    # Move the Green curve's two default points from (0→0, 1→1) to (0→1, 1→0).
    g = crv.mapping.curves[1]
    g.points[0].location = (0.0, 1.0)
    g.points[1].location = (1.0, 0.0)
    crv.mapping.update()
    return crv


def _make_normal_map(nt, links, color_out, loc, *,
                     legacy_loc=None, legacy_curves_loc=None):
    """Wire *color_out* into a Normal Map node that honours UE's DirectX green.

    Blender 5.2+ does the flip on the node itself through
    convention='DIRECTX'.  Older builds get the equivalent RGB Curves Y-flip
    spliced in front, at *legacy_curves_loc* (default: one column left of the
    Normal Map node), with the Normal Map node itself moved to *legacy_loc*
    where the layout has no room for the extra column.  Returns the Normal Map
    node; the caller links its Normal output.
    """
    if bpy.app.version < (5, 2, 0):
        if legacy_loc is not None:
            loc = legacy_loc
        crv = _make_y_invert_curves(nt, legacy_curves_loc or (loc[0] - 300, loc[1]))
        links.new(color_out, crv.inputs['Color'])
        color_out = crv.outputs['Color']
    nm = nt.nodes.new('ShaderNodeNormalMap')
    nm.location = loc
    _set_normal_convention(nm)
    links.new(color_out, nm.inputs['Color'])
    return nm


def _make_tex_node(nt, game_path: str, tex_root: str, tex_ext: str,
                   color_space: str, loc, *, links=None, uv_node=None,
                   tex_index=None):
    img = load_image(game_path, tex_root, tex_ext, tex_index)
    if img is None:
        return None
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.location = loc
    tex.image = img
    try:
        img.colorspace_settings.name = color_space
    except Exception:
        pass
    if links is not None and uv_node is not None:
        links.new(uv_node.outputs['UV'], tex.inputs['Vector'])
    return tex


def _mix_rgb(nt, links, left, right, loc, label: str, blend_type: str = 'MULTIPLY'):
    mix = nt.nodes.new('ShaderNodeMixRGB')
    mix.blend_type = blend_type
    mix.inputs['Fac'].default_value = 1.0
    mix.location = loc
    mix.label = label
    links.new(left, mix.inputs['Color1'])
    links.new(right, mix.inputs['Color2'])
    return mix.outputs['Color']


def _base_color_texture(nt):
    """The Image Texture node that supplies Base Color, or None.

    Walks back from the Principled BSDF depth-first, following each node's
    inputs in socket order. Order matters: the colour path is always wired to
    the first colour input of whatever mix sits in front of it, so a
    depth-first walk lands on the base-colour map rather than on the AO or
    detail map that feeds the same mix's second input.
    """
    bsdf = next((n for n in nt.nodes
                 if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if bsdf is None:
        return None

    seen = set()

    def walk(socket):
        for link in socket.links:
            node = link.from_node
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node.bl_idname == 'ShaderNodeTexImage':
                return node
            for inp in node.inputs:
                found = walk(inp)
                if found is not None:
                    return found
        return None

    return walk(bsdf.inputs['Base Color'])


def _set_active_texture(nt, node) -> None:
    """Make *node* the active/selected node.

    Solid shading with Color set to Texture displays the material's active
    image texture node, so pointing it at the base-colour map is what makes
    that viewport mode show something recognisable.
    """
    if node is None:
        return
    for n in nt.nodes:
        try:
            n.select = False
        except Exception:
            pass
    node.select = True
    try:
        nt.nodes.active = node
    except Exception:
        pass


# -- Standard layout (≤ 9 slots) -----------------------------

_COL_TEX  = -900
_COL_MID  = -320
_COL_BSDF =  200
_COL_OUT  =  550


def _setup_nodes_standard(mat, hashes: list, tex_root: str, tex_ext: str,
                           tex_index=None) -> None:
    """Classic layout for < 9 slots (UV1 only).

    Index → socket:
      0  Base Color          → Base Color (× Occlusion if present)
      1  Metalness           → Metallic
      2  Normal              → Normal Map → Normal
      3  Roughness           → Roughness
      4  Occlusion           → Multiply with Base Color
      5  Alpha               → Alpha  (enables BLEND transparency)
      6  Specular Anisotropy → Anisotropic
    """
    _activate_nodes(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    links = nt.links

    out_node = nt.nodes.new('ShaderNodeOutputMaterial')
    out_node.location = (_COL_OUT, 0)
    bsdf = _make_bsdf(nt, links, out_node, "FF7R Principled", ior=1.3)
    bsdf.location = (_COL_BSDF, 0)

    def slot(i):
        return _slot(hashes, i)

    def make_tex(game_path, color_space, loc):
        return _make_tex_node(nt, game_path, tex_root, tex_ext, color_space, loc,
                              tex_index=tex_index)

    # Slot 4: Occlusion — built first to wire into the Mix node
    occ_path = slot(4)
    occ_node = None
    if occ_path and not _is_const_white(occ_path) and not _is_const_black(occ_path):
        occ_node = make_tex(occ_path, 'Non-Color', (_COL_TEX, 100))
        if occ_node:
            occ_node.label = 'Occlusion'

    # Slot 0: Base Color
    base_path = slot(0)
    if _is_const_white(base_path):
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(base_path):
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif base_path:
        bc_node = make_tex(base_path, 'sRGB', (_COL_TEX, 400))
        if bc_node:
            bc_node.label = 'Base Color'
            if occ_node:
                out = _mix_rgb(nt, links, bc_node.outputs['Color'], occ_node.outputs['Color'],
                               (_COL_MID, 280), 'BC x Occlusion')
                links.new(out, bsdf.inputs['Base Color'])
            else:
                links.new(bc_node.outputs['Color'], bsdf.inputs['Base Color'])

    # Slot 1: Metalness
    met_path = slot(1)
    if _is_const_white(met_path):
        bsdf.inputs['Metallic'].default_value = 1.0
    elif _is_const_black(met_path):
        bsdf.inputs['Metallic'].default_value = 0.0
    elif met_path:
        n = make_tex(met_path, 'Non-Color', (_COL_TEX, 150))
        if n:
            n.label = 'Metalness'
            links.new(n.outputs['Color'], bsdf.inputs['Metallic'])

    # Slot 2: Normal
    nrm_path = slot(2)
    if nrm_path and not _is_const_white(nrm_path) and not _is_const_black(nrm_path):
        n = make_tex(nrm_path, 'Non-Color', (_COL_TEX, -100))
        if n:
            n.label = 'Normal'
            # On < 5.2 the curves fallback lands in the existing gap between
            # the texture and Normal Map columns.
            nm = _make_normal_map(nt, links, n.outputs['Color'], (_COL_MID, -100))
            links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Slot 3: Roughness
    rgh_path = slot(3)
    if _is_const_white(rgh_path):
        bsdf.inputs['Roughness'].default_value = 1.0
    elif _is_const_black(rgh_path):
        bsdf.inputs['Roughness'].default_value = 0.0
    elif rgh_path:
        n = make_tex(rgh_path, 'Non-Color', (_COL_TEX, -350))
        if n:
            n.label = 'Roughness'
            links.new(n.outputs['Color'], bsdf.inputs['Roughness'])

    # Slot 5: Alpha
    alp_path = slot(5)
    if _is_const_white(alp_path):
        bsdf.inputs['Alpha'].default_value = 1.0
    elif _is_const_black(alp_path):
        bsdf.inputs['Alpha'].default_value = 0.0
    elif alp_path:
        n = make_tex(alp_path, 'Non-Color', (_COL_TEX, -600))
        if n:
            n.label = 'Alpha'
            links.new(n.outputs['Color'], bsdf.inputs['Alpha'])
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'BLEND'

    # Slot 6: Specular Anisotropy
    spc_path = slot(6)
    if spc_path:
        spc_socket = (
            bsdf.inputs.get('Anisotropic')
            or bsdf.inputs.get('Specular Anisotropy')
            or bsdf.inputs.get('Specular IOR Level')
            or bsdf.inputs.get('Specular')
        )
        if spc_socket is not None:
            if _is_const_white(spc_path):
                spc_socket.default_value = 1.0
            elif _is_const_black(spc_path):
                spc_socket.default_value = 0.0
            elif spc_path:
                n = make_tex(spc_path, 'Non-Color', (_COL_TEX, -850))
                if n:
                    n.label = 'Specular Anisotropy'
                    links.new(n.outputs['Color'], spc_socket)


# -- 9-slot layout -------------------------------------------

# UV2 detail textures sit one column further left of the primary texture column.
_9_CX_UV2  = _COL_TEX - 400
_9_CX_TEX2 = _COL_TEX - 400   # same column as UV2 node (stacked vertically)


def _setup_nodes_9slot(mat, hashes: list, tex_root: str, tex_ext: str,
                        tex_index=None) -> None:
    """9-slot layout: slots 0-5 identical to standard; 6 = Color Override,
    7 = unknown/ignored, 8 = Detail Normal (UV2), 9 = Detail AO (UV2).

    Index → socket:
      0  Base Color          → Base Color (× AO if present)
      1  Metalness           → Metallic
      2  Normal              → Normal Map → Normal  (+ Detail Normal overlay)
      3  Roughness           → Roughness
      4  Occlusion           → Multiply with Base Color
      5  Alpha               → Alpha  (enables BLEND transparency)
      6  Color Override      → replaces slot 0 output
      7  (unknown / ignored)
      8  Detail Normal (UV2) → overlay-blend before Normal Map
      9  Detail AO    (UV2)  → Multiply with base-color output
    """
    _activate_nodes(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    links = nt.links

    out_node = nt.nodes.new('ShaderNodeOutputMaterial')
    out_node.location = (_COL_OUT, 0)
    bsdf = _make_bsdf(nt, links, out_node, "FF7R Principled (9-slot)", ior=1.3)
    bsdf.location = (_COL_BSDF, 0)

    def slot(i):
        return _slot(hashes, i)

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_9_CX_UV2, -200)

    def make_tex(game_path, color_space, loc):
        return _make_tex_node(nt, game_path, tex_root, tex_ext, color_space, loc,
                              tex_index=tex_index)

    def make_tex_uv2(game_path, color_space, loc):
        n = make_tex(game_path, color_space, loc)
        if n:
            links.new(uv2.outputs['UV'], n.inputs['Vector'])
        return n

    # Slot 9: Detail AO (UV2) — built first so it can be wired into the color mix.
    det_ao_path = slot(9)
    det_ao_out  = None
    if det_ao_path and not _is_const_white(det_ao_path) and not _is_const_black(det_ao_path):
        det_ao = make_tex_uv2(det_ao_path, 'Non-Color', (_9_CX_TEX2, 100))
        if det_ao:
            det_ao.label = 'Detail AO (UV2)'
            det_ao_out = det_ao.outputs['Color']

    # Slot 4: Occlusion (UV1) — combined with Detail AO if both present.
    occ_path = slot(4)
    occ_out   = None
    if occ_path and not _is_const_white(occ_path) and not _is_const_black(occ_path):
        occ_node = make_tex(occ_path, 'Non-Color', (_COL_TEX, 100))
        if occ_node:
            occ_node.label = 'Occlusion'
            occ_out = occ_node.outputs['Color']

    # Merge AO sources: UV1 × UV2 (whichever are present).
    final_ao_out = None
    if occ_out and det_ao_out:
        final_ao_out = _mix_rgb(nt, links, occ_out, det_ao_out,
                                (_COL_MID - 150, 100), 'AO x Detail AO')
    elif occ_out:
        final_ao_out = occ_out
    elif det_ao_out:
        final_ao_out = det_ao_out

    # Slot 0: Base Color.
    bc_out = None
    base_path = slot(0)
    if _is_const_white(base_path):
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(base_path):
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif base_path:
        bc_node = make_tex(base_path, 'sRGB', (_COL_TEX, 400))
        if bc_node:
            bc_node.label = 'Base Color'
            bc_out = bc_node.outputs['Color']

    # Slot 6: Color Override — replaces slot 0 when present.
    ov_path = slot(6)
    if _is_const_white(ov_path):
        bc_out = None
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(ov_path):
        bc_out = None
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif ov_path:
        ov_node = make_tex(ov_path, 'sRGB', (_COL_TEX, 550))
        if ov_node:
            ov_node.label = 'Color Override'
            bc_out = ov_node.outputs['Color']

    if bc_out is not None:
        if final_ao_out is not None:
            out = _mix_rgb(nt, links, bc_out, final_ao_out, (_COL_MID, 280), 'Color x AO')
            links.new(out, bsdf.inputs['Base Color'])
        else:
            links.new(bc_out, bsdf.inputs['Base Color'])

    # Slot 1: Metalness.
    met_path = slot(1)
    if _is_const_white(met_path):
        bsdf.inputs['Metallic'].default_value = 1.0
    elif _is_const_black(met_path):
        bsdf.inputs['Metallic'].default_value = 0.0
    elif met_path:
        n = make_tex(met_path, 'Non-Color', (_COL_TEX, 150))
        if n:
            n.label = 'Metalness'
            links.new(n.outputs['Color'], bsdf.inputs['Metallic'])

    # Slot 2: Normal (UV1) + Slot 8: Detail Normal (UV2) overlay.
    nrm_path     = slot(2)
    det_nrm_path = slot(8)
    nrm_tex_out  = None

    if nrm_path and not _is_const_white(nrm_path) and not _is_const_black(nrm_path):
        n = make_tex(nrm_path, 'Non-Color', (_COL_TEX, -100))
        if n:
            n.label = 'Normal'
            nrm_tex_out = n.outputs['Color']

    if det_nrm_path and not _is_const_white(det_nrm_path) and not _is_const_black(det_nrm_path):
        det_nrm = make_tex_uv2(det_nrm_path, 'Non-Color', (_9_CX_TEX2, -400))
        if det_nrm:
            det_nrm.label = 'Detail Normal (UV2)'
            if nrm_tex_out is not None:
                nrm_tex_out = _mix_rgb(nt, links, nrm_tex_out, det_nrm.outputs['Color'],
                                       (_COL_MID - 300, -100), 'Normal Overlay Detail', 'OVERLAY')
            else:
                nrm_tex_out = det_nrm.outputs['Color']

    if nrm_tex_out is not None:
        nm = _make_normal_map(nt, links, nrm_tex_out, (_COL_MID, -100),
                              legacy_loc=(_COL_MID + 130, -100),
                              legacy_curves_loc=(_COL_MID - 150, -100))
        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Slot 3: Roughness.
    rgh_path = slot(3)
    if _is_const_white(rgh_path):
        bsdf.inputs['Roughness'].default_value = 1.0
    elif _is_const_black(rgh_path):
        bsdf.inputs['Roughness'].default_value = 0.0
    elif rgh_path:
        n = make_tex(rgh_path, 'Non-Color', (_COL_TEX, -350))
        if n:
            n.label = 'Roughness'
            links.new(n.outputs['Color'], bsdf.inputs['Roughness'])

    # Slot 5: Alpha.
    alp_path = slot(5)
    if _is_const_white(alp_path):
        bsdf.inputs['Alpha'].default_value = 1.0
    elif _is_const_black(alp_path):
        bsdf.inputs['Alpha'].default_value = 0.0
    elif alp_path:
        n = make_tex(alp_path, 'Non-Color', (_COL_TEX, -600))
        if n:
            n.label = 'Alpha'
            links.new(n.outputs['Color'], bsdf.inputs['Alpha'])
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'BLEND'


# -- 10-slot layout ------------------------------------------

_10_CX_UV2  = -1300
_10_CX_TEX  =  -900
_10_CX_MID  =  -320
_10_CX_BSDF =   200
_10_CX_OUT  =   550



def _setup_nodes_10slot(mat, hashes: list, tex_root: str, tex_ext: str,
                         tex_index=None) -> None:
    """10-slot layout: UV1 base fields + LayeredColor + UV2 detail fields.

    UV1 base fields (indices 0-4) are used directly when slots 5-9 contain
    only renderer placeholders. Otherwise, the material uses the layered/detail
    interpretation below.

    LayeredColor routing (index 5):
      - /Game/Renderer/Texture/...  -> ignore and fall back to Base/Detail Color
      - any other path              -> UV1 base color override ("LayeredColor")

    Color fallback:
      - index 0 Base Color is used when it is a real, non-renderer texture
      - index 6 Detail Color is used when index 0 is only a renderer placeholder

    Index -> socket:
      0  Base Color (fallback only) -> Base Color (* AO if present)
      1  Metalness                  -> Metallic in first-five fallback mode
      2  Normal                     -> Normal in first-five fallback mode
      3  AO                         -> AO in first-five fallback mode
      4  Roughness                  -> Roughness in first-five fallback mode
      5  LayeredColor               -> Base Color override when not a renderer constant
      6  Detail Color               -> Base Color fallback on UV2
      7  Detail Normal              -> Normal Map -> Normal
      8  Detail Roughness           -> Roughness
      9  Detail AO                  -> Multiply with Color before Base Color
    """
    _activate_nodes(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    links = nt.links

    out_node = nt.nodes.new('ShaderNodeOutputMaterial')
    out_node.location = (_10_CX_OUT, 0)
    bsdf = _make_bsdf(nt, links, out_node, "FF7R Principled (10-slot UV2)", ior=1.3)
    bsdf.location = (_10_CX_BSDF, 0)

    def slot(i):
        return _slot(hashes, i)

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_10_CX_UV2, 0)

    def make_tex_uv1(game_path, color_space, loc):
        """Load a texture on UV1 (default coordinates — no Vector link)."""
        return _make_tex_node(nt, game_path, tex_root, tex_ext, color_space, loc,
                              tex_index=tex_index)

    def make_tex(game_path, color_space, loc):
        """Load a texture on UV2."""
        n = make_tex_uv1(game_path, color_space, loc)
        if n:
            links.new(uv2.outputs['UV'], n.inputs['Vector'])
        return n

    use_base_fields = not any(slot(i) for i in range(5, 10))

    # Index 9: AO — built first to wire into the Mix node
    ao_node = None
    ao_path = slot(3) if use_base_fields else slot(9)
    if ao_path and not _is_const_white(ao_path) and not _is_const_black(ao_path):
        make_ao_tex = make_tex_uv1 if use_base_fields else make_tex
        ao_node = make_ao_tex(ao_path, 'Non-Color', (_10_CX_TEX, 100))
        if ao_node:
            ao_node.label = 'AO (UV1)' if use_base_fields else 'AO (UV2)'
    # Index 5: LayeredColor. Renderer constants do not carry useful color data
    # here, so ignore them and fall back to Base Color or Detail Color.
    layered_color_path = slot(5)
    base_path = slot(0)
    detail_color_path = slot(6)
    bc_node = None
    bc_label = None
    if use_base_fields and base_path:
        bc_node = make_tex_uv1(base_path, 'sRGB', (_10_CX_TEX, 400))
        bc_label = 'Base Color (UV1)'
    elif layered_color_path:
        bc_node = make_tex_uv1(layered_color_path, 'sRGB', (_10_CX_TEX, 400))
        bc_label = 'LayeredColor Override (UV1)'
    elif base_path:
        bc_node = make_tex_uv1(base_path, 'sRGB', (_10_CX_TEX, 400))
        bc_label = 'Base Color (UV1 fallback)'
    elif _is_const_white(detail_color_path):
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(detail_color_path):
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif detail_color_path:
        bc_node = make_tex(detail_color_path, 'sRGB', (_10_CX_TEX, 400))
        bc_label = 'Detail Color (UV2 as Base Color)'

    if bc_node:
        bc_node.label = bc_label
        if ao_node:
            out = _mix_rgb(nt, links, bc_node.outputs['Color'], ao_node.outputs['Color'],
                           (_10_CX_MID, 280), 'Color x AO')
            links.new(out, bsdf.inputs['Base Color'])
        else:
            links.new(bc_node.outputs['Color'], bsdf.inputs['Base Color'])

    if use_base_fields:
        met_path = slot(1)
        if met_path:
            n = make_tex_uv1(met_path, 'Non-Color', (_10_CX_TEX, 150))
            if n:
                n.label = 'Metalness (UV1)'
                links.new(n.outputs['Color'], bsdf.inputs['Metallic'])

    # Normal
    nrm_path = slot(2) if use_base_fields else slot(7)
    if nrm_path and not _is_const_white(nrm_path) and not _is_const_black(nrm_path):
        make_nrm_tex = make_tex_uv1 if use_base_fields else make_tex
        n = make_nrm_tex(nrm_path, 'Non-Color', (_10_CX_TEX, -100))
        if n:
            n.label = 'Normal (UV1)' if use_base_fields else 'Normal (UV2)'
            nm = _make_normal_map(nt, links, n.outputs['Color'],
                                  (_10_CX_MID, -100))
            links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Roughness
    rgh_path = slot(4) if use_base_fields else slot(8)
    if _is_const_white(rgh_path):
        bsdf.inputs['Roughness'].default_value = 1.0
    elif _is_const_black(rgh_path):
        bsdf.inputs['Roughness'].default_value = 0.0
    elif rgh_path:
        make_rgh_tex = make_tex_uv1 if use_base_fields else make_tex
        n = make_rgh_tex(rgh_path, 'Non-Color', (_10_CX_TEX, -350))
        if n:
            n.label = 'Roughness (UV1)' if use_base_fields else 'Roughness (UV2)'
            links.new(n.outputs['Color'], bsdf.inputs['Roughness'])


# -- Extended layout (11-12 slots) ---------------------------

_EX_CX_UV2  = -1700
_EX_CX_TEX  = -1300
_EX_CX_MID  =  -650
_EX_CX_BSDF =   200
_EX_CX_OUT  =   600


def _setup_nodes_extended(mat, hashes: list, tex_root: str, tex_ext: str,
                           tex_index=None) -> None:
    """LEGACY 11/12-slot layout: UV1 base textures + UV2 detail textures.

    These index sets describe *de-duplicated* slot lists, which is what the
    importer used to produce. They are shifted relative to the real
    FMassiveEnvironmentMaterialInfo slots and cannot reach the detail layer's
    base colour, so they are reachable only through the dispatcher's fallback
    branches. The accurate two-layer path is _setup_nodes_two_layer.

    Shared base indices (UV1):
      0  Base Color      → Base Color
      1  Metalness       → Metallic
      2  Normal          → Normal Map → Normal
      3  Roughness       → Roughness
      4  AO              → Multiply with Base Color
      5  (unused)
      6  Color Override  → replaces slot 0
      7  (unused)

    12-slot detail indices (UV2):
      8  Detail Metalness  →  multiply / replace Metalness
      9  Detail Normal     →  overlay-blend before NormalMap
      10 Detail Roughness  →  combine with Roughness
      11 Detail AO         →  multiply with AO

    11-slot detail indices (UV2, no Detail Metalness):
      8  Detail Normal     →  overlay-blend before NormalMap
      9  Detail Roughness  →  combine with Roughness
      10 Detail AO         →  multiply with AO
    """
    _activate_nodes(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    links = nt.links

    is_12 = (len(hashes) >= 12)

    out_node = nt.nodes.new('ShaderNodeOutputMaterial')
    out_node.location = (_EX_CX_OUT, 0)
    bsdf = _make_bsdf(nt, links, out_node, "FF7R Principled", ior=1.2)
    bsdf.location = (_EX_CX_BSDF, 0)

    def slot(i):
        return _slot(hashes, i)

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_EX_CX_UV2, -1300)

    def make_tex(game_path, color_space, loc, use_uv2=False):
        return _make_tex_node(
            nt, game_path, tex_root, tex_ext, color_space, loc,
            links=links if use_uv2 else None,
            uv_node=uv2 if use_uv2 else None,
            tex_index=tex_index,
        )

    if is_12:
        IDX_OVERRIDE  = 6
        IDX_DET_METAL = 8
        IDX_DET_NRM   = 9
        IDX_DET_RGH   = 10
        IDX_DET_AO    = 11
    else:
        IDX_OVERRIDE  = 6
        IDX_DET_METAL = None
        IDX_DET_NRM   = 8
        IDX_DET_RGH   = 9
        IDX_DET_AO    = 10

    # Slot 4: AO
    ao_path    = slot(4)
    ao_tex_out = None
    if ao_path and not _is_const_white(ao_path) and not _is_const_black(ao_path):
        ao_node = make_tex(ao_path, 'Non-Color', (_EX_CX_TEX, -400))
        if ao_node:
            ao_node.label = 'AO'
            ao_tex_out = ao_node.outputs['Color']

    # Detail AO (UV2) → multiply by AO
    det_ao_path  = slot(IDX_DET_AO)
    final_ao_out = ao_tex_out
    if det_ao_path and not _is_const_white(det_ao_path) and not _is_const_black(det_ao_path):
        det_ao = make_tex(det_ao_path, 'Non-Color', (_EX_CX_TEX, -1700), use_uv2=True)
        if det_ao:
            det_ao.label = 'Detail AO (UV2)'
            if ao_tex_out is not None:
                final_ao_out = _mix_rgb(nt, links, ao_tex_out, det_ao.outputs['Color'],
                                        (_EX_CX_MID, -900), 'AO x Detail AO')
            else:
                final_ao_out = det_ao.outputs['Color']

    # Slot 0: Base Color
    bc_path = slot(0)
    bc_out  = None
    if _is_const_white(bc_path):
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(bc_path):
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif bc_path:
        bc_node = make_tex(bc_path, 'sRGB', (_EX_CX_TEX, 1600))
        if bc_node:
            bc_node.label = 'Base Color'
            bc_out = bc_node.outputs['Color']

    # Color Override — replaces slot 0. In the true 14-slot layout this is
    # slot 7, the base layer's _CM map: slot 0 is a constant white in every
    # sampled two-layer mesh type, so the colour genuinely lives here.
    ov_path = slot(IDX_OVERRIDE)
    if _is_const_white(ov_path):
        bc_out = None
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(ov_path):
        bc_out = None
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif ov_path:
        ov_node = make_tex(ov_path, 'sRGB', (_EX_CX_TEX, 1200))
        if ov_node:
            ov_node.label = 'Color Override'
            bc_out = ov_node.outputs['Color']

    if bc_out is not None:
        if final_ao_out is not None:
            out = _mix_rgb(nt, links, bc_out, final_ao_out, (_EX_CX_MID, 600), 'Color x AO')
            links.new(out, bsdf.inputs['Base Color'])
        else:
            links.new(bc_out, bsdf.inputs['Base Color'])

    # Slot 1: Metalness
    met_path     = slot(1)
    met_out      = None
    met_is_black = _is_const_black(met_path)
    if _is_const_white(met_path):
        bsdf.inputs['Metallic'].default_value = 1.0
    elif met_is_black:
        bsdf.inputs['Metallic'].default_value = 0.0
    elif met_path:
        met_node = make_tex(met_path, 'Non-Color', (_EX_CX_TEX, 800))
        if met_node:
            met_node.label = 'Metalness'
            met_out = met_node.outputs['Color']

    # Detail Metalness (UV2) — 12-slot only
    if IDX_DET_METAL is not None:
        det_met_path = slot(IDX_DET_METAL)
        if det_met_path and not _is_const_white(det_met_path) and not _is_const_black(det_met_path):
            det_met = make_tex(det_met_path, 'Non-Color', (_EX_CX_TEX, -800), use_uv2=True)
            if det_met:
                det_met.label = 'Detail Metalness (UV2)'
                if met_is_black or met_out is None:
                    links.new(det_met.outputs['Color'], bsdf.inputs['Metallic'])
                else:
                    out = _mix_rgb(nt, links, met_out, det_met.outputs['Color'],
                                   (_EX_CX_MID, 100), 'Metal x Detail Metal')
                    links.new(out, bsdf.inputs['Metallic'])
            elif met_out is not None:
                links.new(met_out, bsdf.inputs['Metallic'])
        elif met_out is not None:
            links.new(met_out, bsdf.inputs['Metallic'])
    elif met_out is not None:
        links.new(met_out, bsdf.inputs['Metallic'])

    # Slot 2: Normal
    nrm_path    = slot(2)
    nrm_tex_out = None
    if nrm_path and not _is_const_white(nrm_path) and not _is_const_black(nrm_path):
        nrm_node = make_tex(nrm_path, 'Non-Color', (_EX_CX_TEX, 400))
        if nrm_node:
            nrm_node.label = 'Normal'
            nrm_tex_out = nrm_node.outputs['Color']

    # Detail Normal (UV2) → overlay-blend before NormalMap node
    det_nrm_path = slot(IDX_DET_NRM)
    if det_nrm_path and not _is_const_white(det_nrm_path) and not _is_const_black(det_nrm_path):
        det_nrm = make_tex(det_nrm_path, 'Non-Color', (_EX_CX_TEX, -1100), use_uv2=True)
        if det_nrm:
            det_nrm.label = 'Detail Normal (UV2)'
            if nrm_tex_out is not None:
                nrm_tex_out = _mix_rgb(nt, links, nrm_tex_out, det_nrm.outputs['Color'],
                                       (_EX_CX_MID - 150, 300), 'Normal Overlay Detail', 'OVERLAY')
            else:
                nrm_tex_out = det_nrm.outputs['Color']

    if nrm_tex_out is not None:
        # On < 5.2 the curves node takes the Normal Map slot and NM moves right.
        nm = _make_normal_map(nt, links, nrm_tex_out, (_EX_CX_MID + 100, 300),
                              legacy_loc=(_EX_CX_MID + 380, 300),
                              legacy_curves_loc=(_EX_CX_MID + 100, 300))
        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Slot 3: Roughness
    rgh_path = slot(3)
    rgh_out  = None
    if _is_const_white(rgh_path):
        bsdf.inputs['Roughness'].default_value = 1.0
    elif _is_const_black(rgh_path):
        bsdf.inputs['Roughness'].default_value = 0.0
    elif rgh_path:
        rgh_node = make_tex(rgh_path, 'Non-Color', (_EX_CX_TEX, 0))
        if rgh_node:
            rgh_node.label = 'Roughness'
            rgh_out = rgh_node.outputs['Color']

    # Detail Roughness (UV2)
    det_rgh_path = slot(IDX_DET_RGH)
    if det_rgh_path and not _is_const_white(det_rgh_path) and not _is_const_black(det_rgh_path):
        det_rgh = make_tex(det_rgh_path, 'Non-Color', (_EX_CX_TEX, -1400), use_uv2=True)
        if det_rgh:
            det_rgh.label = 'Detail Roughness (UV2)'
            if rgh_out is not None:
                m = nt.nodes.new('ShaderNodeMath')
                m.operation = 'DIVIDE'
                m.location = (_EX_CX_MID, -400)
                m.label = 'Roughness × Detail Rgh'
                links.new(rgh_out,                  m.inputs[0])
                links.new(det_rgh.outputs['Color'], m.inputs[1])
                links.new(m.outputs['Value'], bsdf.inputs['Roughness'])
            else:
                links.new(det_rgh.outputs['Color'], bsdf.inputs['Roughness'])
        elif rgh_out is not None:
            links.new(rgh_out, bsdf.inputs['Roughness'])
    elif rgh_out is not None:
        links.new(rgh_out, bsdf.inputs['Roughness'])


# -- Two-layer layout (14 slots) -----------------------------

_TL_UV2  = -2100
_TL_TEX  = -1700
_TL_A    = -1250
_TL_B    =  -900
_TL_C    =  -550
_TL_D    =  -200
_TL_BSDF =   250
_TL_OUT   =  650


def _setup_nodes_two_layer(mat, hashes: list, tex_root: str, tex_ext: str,
                           tex_index=None) -> None:
    """The real 14-slot layout, transcribed from the MEC base-pass pixel shader.

    Identification note: ``RMI_Surface_Standard_Wide_Detail`` is our current
    best guess for the RMI equivalent of this specific larger texture-slot-count
    (14-slot) Massive Environment material variant. Its enabled static switches
    are Color_, Metallic_, Normal_, Roughness_, Occlusion_, Detail_,
    WideOcclusion_, DetailNormal_, DetailRoughness_, DetailOcclusion_,
    Coordinate0_/1_/2_, and Standard_. That inference comes from the rock
    materials that use this RMI_Surface variant. The shader interpretation and
    flag correspondence remain evidence-based rather than a confirmed one-to-
    one mapping; do not generalize it to other RMI_Surface materials.

    Slots (FMassiveEnvironmentMaterialInfo order). Layer 1 samples with UV1,
    layer 2 with UV2 - the shader takes them from COLOR.xy and COLOR.zw:

        0  layer 1 base colour   (constant white in ~99% of mesh types)
        1  layer 1 metalness     2  layer 1 normal
        3  layer 1 roughness     4  layer 1 AO
        5  alpha                 - never sampled by the shader
        6  mask                  - written straight to a GBuffer channel we
                                   have no equivalent for; left unwired
        7  _CM                   layer 1's actual colour map
        8  reserved              - never even loaded by the shader
        9  layer 2 base colour  10  layer 2 metalness  11 layer 2 normal
       12  layer 2 roughness    13  layer 2 AO

    Combines, exactly as the shader computes them:

        albedo    = sRGB_decode( Overlay( base = sRGB(slot7),
                                          blend = sRGB(sat(slot9 * slot0)) ) )
        metallic  = sat(slot1 + slot10)
        roughness = sqrt(sat(slot3^2 + slot12^2))
        AO        = let m = slot4*slot13, n = min(slot4, slot13),
                        t = m + 1 - n
                    in  (1 - t^5)^5 * (m - n) + n
        normal    = reoriented normal mapping of layer 2 onto layer 1

    Three deliberate divergences, all noted at their node:
      * The shader rotates layer 2's normal into layer 1's tangent frame using
        screen-space UV derivatives. Blender's node graph has no ddx/ddy, so
        the reorientation is done with the frames assumed aligned.
      * The shader keeps AO in its own GBuffer channel; Principled has no AO
        input, so it is multiplied into base colour as the usual stand-in.
      * The gamma round-trip uses a Gamma node (2.2) rather than the exact
        piecewise sRGB curve, which would need ~20 nodes per channel.
    """
    _activate_nodes(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    links = nt.links

    out_node = nt.nodes.new('ShaderNodeOutputMaterial')
    out_node.location = (_TL_OUT, 0)
    bsdf = _make_bsdf(nt, links, out_node, "FF7R Two-Layer", ior=1.2)
    bsdf.location = (_TL_BSDF, 0)

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_TL_UV2, -700)

    def raw(i):
        """Unfiltered slot value, so constant textures stay distinguishable."""
        return hashes[i] if i < len(hashes) else None

    def tex(i, color_space, loc, label, use_uv2=False):
        """Texture node for a slot, or None if the slot is empty/constant."""
        path = _slot(hashes, i)
        if not path:
            return None
        node = _make_tex_node(nt, path, tex_root, tex_ext, color_space, loc,
                              links=links if use_uv2 else None,
                              uv_node=uv2 if use_uv2 else None,
                              tex_index=tex_index)
        if node is not None:
            node.label = label
        return node

    def const_of(i):
        """1.0 / 0.0 for a constant slot, else None."""
        p = raw(i)
        if _is_const_white(p):
            return 1.0
        if _is_const_black(p):
            return 0.0
        return None

    def out_of(node):
        return node.outputs['Color'] if node is not None else None

    def math(op, loc, label, a=None, b=None, clamp=False):
        n = nt.nodes.new('ShaderNodeMath')
        n.operation = op
        n.location  = loc
        n.label     = label
        n.use_clamp = clamp
        for idx, val in ((0, a), (1, b)):
            if val is None:
                continue
            if hasattr(val, 'is_output'):
                links.new(val, n.inputs[idx])
            else:
                n.inputs[idx].default_value = val
        return n.outputs['Value']

    def vmath(op, loc, label, a=None, b=None, c=None, scale=None):
        n = nt.nodes.new('ShaderNodeVectorMath')
        n.operation = op
        n.location  = loc
        n.label     = label
        for idx, val in ((0, a), (1, b), (2, c)):
            if val is None:
                continue
            if hasattr(val, 'is_output'):
                links.new(val, n.inputs[idx])
            else:
                n.inputs[idx].default_value = val
        if scale is not None:
            if hasattr(scale, 'is_output'):
                links.new(scale, n.inputs['Scale'])
            else:
                n.inputs['Scale'].default_value = scale
        return n.outputs['Value'] if op == 'DOT_PRODUCT' else n.outputs['Vector']

    # ---- Base colour -------------------------------------------------
    # Both halves are loaded Non-Color deliberately: the shader re-encodes the
    # sampled values to sRGB before blending, and a Non-Color image node hands
    # back exactly those stored gamma values, so the encode is free and exact.
    cm_node = tex(7,  'Non-Color', (_TL_TEX, 1500), 'Layer 1 Colour (_CM)')
    l2_node = tex(9,  'Non-Color', (_TL_TEX, 1150), 'Layer 2 Colour (UV2)', True)
    l1_node = tex(0,  'Non-Color', (_TL_TEX,  800), 'Layer 1 Base Colour')

    blend_out = out_of(l2_node)
    if l1_node is not None:
        if blend_out is not None:
            # The shader multiplies these in linear space and encodes after;
            # doing it in gamma space diverges slightly. Slot 0 is a constant
            # white in ~99% of mesh types, where this multiply vanishes.
            blend_out = _mix_rgb(nt, links, blend_out, out_of(l1_node),
                                 (_TL_A, 1000), 'Layer2 x Layer1 Colour')
        else:
            blend_out = out_of(l1_node)

    cm_out = out_of(cm_node)
    if cm_out is not None and blend_out is not None:
        # Blender's OVERLAY branches on Color1, matching the shader's branch on
        # the _CM value - so _CM must be Color1.
        albedo_gamma = _mix_rgb(nt, links, cm_out, blend_out,
                                (_TL_B, 1300), 'Overlay(_CM, Layer2)', 'OVERLAY')
    else:
        albedo_gamma = cm_out if cm_out is not None else blend_out

    bc_out = None
    if albedo_gamma is not None:
        gam = nt.nodes.new('ShaderNodeGamma')
        gam.location = (_TL_C, 1300)
        gam.label = 'sRGB -> Linear'
        gam.inputs['Gamma'].default_value = 2.2
        links.new(albedo_gamma, gam.inputs['Color'])
        bc_out = gam.outputs['Color']

    # ---- AO ----------------------------------------------------------
    ao1 = tex(4,  'Non-Color', (_TL_TEX,  450), 'Layer 1 AO')
    ao2 = tex(13, 'Non-Color', (_TL_TEX,  100), 'Layer 2 AO (UV2)', True)
    a_out, b_out = out_of(ao1), out_of(ao2)
    ao_out = None
    if a_out is not None and b_out is not None:
        m = math('MULTIPLY', (_TL_A, 450), 'AO m = a*b', a_out, b_out)
        n = math('MINIMUM',  (_TL_A, 280), 'AO n = min(a,b)', a_out, b_out)
        t = math('SUBTRACT', (_TL_B, 450), 'AO t = m+1-n',
                 math('ADD', (_TL_A, 110), 'm + 1', m, 1.0), n)
        t5 = math('POWER', (_TL_B, 280), 't^5', t, 5.0)
        u5 = math('POWER', (_TL_B, 110), '(1-t^5)^5',
                  math('SUBTRACT', (_TL_B, -60), '1 - t^5', 1.0, t5), 5.0)
        d = math('SUBTRACT', (_TL_C, 280), 'm - n', m, n)
        ao_out = math('ADD', (_TL_C, 110), 'AO combine',
                      math('MULTIPLY', (_TL_C, -60), '(1-t^5)^5 * (m-n)', u5, d),
                      n, clamp=True)
    elif a_out is not None:
        ao_out = a_out          # shader's b would be constant white -> passthrough
    elif b_out is not None:
        ao_out = b_out

    if bc_out is not None and ao_out is not None:
        # Stand-in for the shader's separate AO GBuffer channel (see docstring).
        bc_out = _mix_rgb(nt, links, bc_out, ao_out, (_TL_D, 1300), 'Colour x AO')
    if bc_out is not None:
        links.new(bc_out, bsdf.inputs['Base Color'])

    # ---- Metalness: sat(slot1 + slot10) ------------------------------
    met1 = tex(1,  'Non-Color', (_TL_TEX, -250), 'Layer 1 Metalness')
    met2 = tex(10, 'Non-Color', (_TL_TEX, -600), 'Layer 2 Metalness (UV2)', True)
    m1_out, m2_out = out_of(met1), out_of(met2)
    m1_const, m2_const = const_of(1), const_of(10)
    if m1_out is not None and m2_out is not None:
        links.new(math('ADD', (_TL_A, -400), 'Metal 1 + 2', m1_out, m2_out,
                       clamp=True), bsdf.inputs['Metallic'])
    elif m1_out is not None or m2_out is not None:
        only = m1_out if m1_out is not None else m2_out
        other = m2_const if m1_out is not None else m1_const
        if other:                       # constant white on the other half
            bsdf.inputs['Metallic'].default_value = 1.0
        else:
            links.new(only, bsdf.inputs['Metallic'])
    else:
        total = (m1_const or 0.0) + (m2_const or 0.0)
        bsdf.inputs['Metallic'].default_value = min(1.0, total)

    # ---- Roughness: sqrt(sat(slot3^2 + slot12^2)) --------------------
    rgh1 = tex(3,  'Non-Color', (_TL_TEX, -950),  'Layer 1 Roughness')
    rgh2 = tex(12, 'Non-Color', (_TL_TEX, -1300), 'Layer 2 Roughness (UV2)', True)
    r1_out, r2_out = out_of(rgh1), out_of(rgh2)
    if r1_out is not None and r2_out is not None:
        sq1 = math('MULTIPLY', (_TL_A, -950),  'r1^2', r1_out, r1_out)
        sq2 = math('MULTIPLY', (_TL_A, -1120), 'r2^2', r2_out, r2_out)
        links.new(math('SQRT', (_TL_C, -1000), 'sqrt(r1^2 + r2^2)',
                       math('ADD', (_TL_B, -1000), 'r1^2 + r2^2', sq1, sq2,
                            clamp=True)),
                  bsdf.inputs['Roughness'])
    elif r1_out is not None or r2_out is not None:
        # sqrt(sat(r^2)) == r for r in [0,1], so a lone map passes through.
        links.new(r1_out if r1_out is not None else r2_out,
                  bsdf.inputs['Roughness'])
    else:
        const = const_of(3)
        if const is None:
            const = const_of(12)
        if const is not None:
            bsdf.inputs['Roughness'].default_value = const

    # ---- Normal: reoriented normal mapping ---------------------------
    nrm1 = tex(2,  'Non-Color', (_TL_TEX, -1650), 'Layer 1 Normal')
    nrm2 = tex(11, 'Non-Color', (_TL_TEX, -2100), 'Layer 2 Normal (UV2)', True)

    def unpack_normal(node, y, tag):
        """Texel -> unit tangent-space normal, still DirectX-convention.

        Blender's DDS loader already hands back BC5 normal maps with the
        blue/Z channel reconstructed, so all three channels decode straight
        through as rgb*2-1.  The green flip is not applied here: it belongs
        to the single Normal Map node at the end of the chain.
        """
        signed = vmath('MULTIPLY_ADD', (_TL_A, y), tag + ' *2-1',
                       node.outputs['Color'], (2.0, 2.0, 2.0), (-1.0, -1.0, -1.0))
        return vmath('NORMALIZE', (_TL_B, y), tag + ' normalize', signed)

    n_out = None
    if nrm1 is not None and nrm2 is not None:
        n1 = unpack_normal(nrm1, -1650, 'L1')
        n2 = unpack_normal(nrm2, -2100, 'L2')
        # Reoriented normal mapping: r = normalize(t*dot(t,u) - u*t.z),
        # with t = n1 + (0,0,1) and u = n2 * (-1,-1,1). The shader first
        # rotates n2 from UV2's tangent frame into UV1's using screen-space
        # derivatives; no node-graph equivalent exists, so the frames are
        # assumed aligned here.
        t = vmath('ADD', (_TL_D, -1800), 'RNM t = n1 + Z', n1, (0.0, 0.0, 1.0))
        u = vmath('MULTIPLY', (_TL_D, -1970), 'RNM u = n2 * (-1,-1,1)', n2,
                  (-1.0, -1.0, 1.0))
        dot_tu = vmath('DOT_PRODUCT', (_TL_D, -2140), 'RNM dot(t,u)', t, u)
        t_sep = nt.nodes.new('ShaderNodeSeparateXYZ')
        t_sep.location = (_TL_D, -2310)
        links.new(t, t_sep.inputs['Vector'])
        n_out = vmath('NORMALIZE', (_TL_D, -2650), 'RNM normalize',
                      vmath('SUBTRACT', (_TL_D, -2480), 'RNM t*dot - u*t.z',
                            vmath('SCALE', (_TL_C, -2820), 't * dot', t,
                                  scale=dot_tu),
                            vmath('SCALE', (_TL_C, -2990), 'u * t.z', u,
                                  scale=t_sep.outputs['Z'])))

    if n_out is not None:
        # Back to [0,1] for the Normal Map node.  Y is NOT negated here: the
        # node's own DIRECTX convention (or the < 5.2 curves fallback) does
        # that, exactly as in every other builder in this file.
        packed = vmath('MULTIPLY_ADD', (_TL_BSDF - 120, -2000), 'Pack to [0,1]',
                       n_out, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        nm = _make_normal_map(nt, links, packed, (_TL_BSDF - 120, -1800),
                              legacy_curves_loc=(_TL_BSDF - 120, -2200))
        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])
    else:
        # A single layer needs no vector maths at all: the texture feeds the
        # Normal Map node directly.
        lone = nrm1 if nrm1 is not None else nrm2
        if lone is not None:
            nm = _make_normal_map(nt, links, lone.outputs['Color'],
                                  (_TL_BSDF - 120, -1800),
                                  legacy_curves_loc=(_TL_C, -1800))
            links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])


# ============================================================
#  Dispatcher
# ============================================================

def setup_material_nodes(mat, hashes: list, tex_root: str, tex_ext: str,
                          tex_index=None) -> None:
    """Choose and run the correct node-setup function for *hashes*.

    *hashes* is index-aligned with FMassiveEnvironmentMaterialInfo's texture
    slots, so its length is the mesh type's valid-slot count. Measured over
    43,315 mesh types across both game builds, that count is only ever:

        7  -> single layer            (_setup_nodes_standard)
        14 -> base layer + world-tiling detail layer (_setup_nodes_extended)

    The intermediate counts the older branches keyed on (9/10/11/12) were an
    artefact of de-duplicating the slot list, which also shifted slots onto the
    wrong sockets - 36 of 437 two-layer mesh types were being routed to the
    wrong tree. Those branches are kept below purely as a fallback, in case a
    layout exists outside the sampled corpus.
    """
    n = len(hashes)
    if n == 14:
        _setup_nodes_two_layer(mat, hashes, tex_root, tex_ext, tex_index=tex_index)
    elif n <= 7:
        _setup_nodes_standard(mat, hashes, tex_root, tex_ext, tex_index=tex_index)
    # --- fallbacks for unobserved slot counts ---
    elif n >= 11:
        _setup_nodes_extended(mat, hashes, tex_root, tex_ext, tex_index=tex_index)
    elif n == 10:
        _setup_nodes_10slot(mat, hashes, tex_root, tex_ext, tex_index=tex_index)
    elif n == 9:
        _setup_nodes_9slot(mat, hashes, tex_root, tex_ext, tex_index=tex_index)
    else:
        _setup_nodes_standard(mat, hashes, tex_root, tex_ext, tex_index=tex_index)

    # Done centrally rather than per-builder so every layout gets it, including
    # the legacy fallbacks.
    nt = mat.node_tree
    _set_active_texture(nt, _base_color_texture(nt))
