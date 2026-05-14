"""
material.py
Hash-to-path lookup, UE texture helpers, and Principled BSDF node-tree builders.

Sections
--------
1. Hash lookup (texture_hashes.csv fallback table)
2. Texture path constants and helpers
3. Node-tree builders:
     _setup_nodes_standard   — ≤ 9 slots (classic 7-slot layout)
     _setup_nodes_10slot     — 10 slots (fields 1-6 ignored; 7-10 = Color/Nrm/Rgh/AO on UV2)
     _setup_nodes_extended   — 11-12 slots (UV1 base + UV2 detail)
   Dispatcher:
     setup_material_nodes
"""

import os
import csv
import bpy


# ============================================================
#  1. Hash lookup
# ============================================================

_CSV_NAME = "texture_hashes.csv"

# Module-level table; populated by load_hash_table()
_hash_table: dict[str, str] = {}


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


def load_image(game_path: str, tex_root: str, tex_ext: str):
    """Resolve a /Game/... asset path to a local file and return a bpy.types.Image.

    Returns None when tex_root is empty or the file cannot be found.
    """
    if not game_path or not tex_root:
        return None
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
    # curves[0]=Combined, [1]=R, [2]=G, [3]=B
    # Move the Green curve's two default points from (0→0, 1→1) to (0→1, 1→0).
    g = crv.mapping.curves[1]
    g.points[0].location = (0.0, 1.0)
    g.points[1].location = (1.0, 0.0)
    crv.mapping.update()
    return crv


# -- Standard layout (≤ 9 slots) -----------------------------

_COL_TEX  = -900
_COL_MID  = -320
_COL_BSDF =  200
_COL_OUT  =  550


def _setup_nodes_standard(mat, hashes: list, tex_root: str, tex_ext: str) -> None:
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
        return hashes[i] if i < len(hashes) else None

    def make_tex(game_path, color_space, loc):
        img = load_image(game_path, tex_root, tex_ext)
        if img is None:
            return None
        n = nt.nodes.new('ShaderNodeTexImage')
        n.location = loc
        n.image = img
        try:
            img.colorspace_settings.name = color_space
        except Exception:
            pass
        return n

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
                mix = nt.nodes.new('ShaderNodeMixRGB')
                mix.blend_type = 'MULTIPLY'
                mix.inputs['Fac'].default_value = 1.0
                mix.location = (_COL_MID, 280)
                mix.label = 'BC × Occlusion'
                links.new(bc_node.outputs['Color'], mix.inputs['Color1'])
                links.new(occ_node.outputs['Color'], mix.inputs['Color2'])
                links.new(mix.outputs['Color'], bsdf.inputs['Base Color'])
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
            nrm_out = n.outputs['Color']
            if bpy.app.version < (5, 2, 0):
                # DirectX convention toggle absent; invert G with an RGB Curves node
                # positioned in the existing gap between the texture and Normal Map columns.
                crv = _make_y_invert_curves(nt, (_COL_MID - 300, -100))
                links.new(nrm_out, crv.inputs['Color'])
                nrm_out = crv.outputs['Color']
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.location = (_COL_MID, -100)
            _set_normal_convention(nm)
            links.new(nrm_out, nm.inputs['Color'])
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


def _setup_nodes_9slot(mat, hashes: list, tex_root: str, tex_ext: str) -> None:
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
        return hashes[i] if i < len(hashes) else None

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_9_CX_UV2, -200)

    def make_tex(game_path, color_space, loc):
        img = load_image(game_path, tex_root, tex_ext)
        if img is None:
            return None
        n = nt.nodes.new('ShaderNodeTexImage')
        n.location = loc
        n.image = img
        try:
            img.colorspace_settings.name = color_space
        except Exception:
            pass
        return n

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
        m = nt.nodes.new('ShaderNodeMixRGB')
        m.blend_type = 'MULTIPLY'
        m.inputs['Fac'].default_value = 1.0
        m.location = (_COL_MID - 150, 100)
        m.label = 'AO × Detail AO'
        links.new(occ_out,    m.inputs['Color1'])
        links.new(det_ao_out, m.inputs['Color2'])
        final_ao_out = m.outputs['Color']
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
            m = nt.nodes.new('ShaderNodeMixRGB')
            m.blend_type = 'MULTIPLY'
            m.inputs['Fac'].default_value = 1.0
            m.location = (_COL_MID, 280)
            m.label = 'Color × AO'
            links.new(bc_out,        m.inputs['Color1'])
            links.new(final_ao_out,  m.inputs['Color2'])
            links.new(m.outputs['Color'], bsdf.inputs['Base Color'])
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
                m = nt.nodes.new('ShaderNodeMixRGB')
                m.blend_type = 'OVERLAY'
                m.inputs['Fac'].default_value = 1.0
                m.location = (_COL_MID - 300, -100)
                m.label = 'Normal Overlay Detail'
                links.new(nrm_tex_out,              m.inputs['Color1'])
                links.new(det_nrm.outputs['Color'], m.inputs['Color2'])
                nrm_tex_out = m.outputs['Color']
            else:
                nrm_tex_out = det_nrm.outputs['Color']

    if nrm_tex_out is not None:
        if bpy.app.version < (5, 2, 0):
            crv = _make_y_invert_curves(nt, (_COL_MID - 150, -100))
            links.new(nrm_tex_out, crv.inputs['Color'])
            nrm_tex_out = crv.outputs['Color']
            nm_x = _COL_MID + 130
        else:
            nm_x = _COL_MID
        nm = nt.nodes.new('ShaderNodeNormalMap')
        nm.location = (nm_x, -100)
        _set_normal_convention(nm)
        links.new(nrm_tex_out, nm.inputs['Color'])
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


def _is_renderer_texture(path: str) -> bool:
    """Return True when *path* is a /Game/Renderer/Texture/… constant (UV2 color role)."""
    return _strip_ue_suffix(path).lower().startswith('/game/renderer/texture/')


def _setup_nodes_10slot(mat, hashes: list, tex_root: str, tex_ext: str) -> None:
    """10-slot layout: fields 1-5 ignored; 6-9 = Color/Normal/Roughness/AO.

    Slot 6 routing:
      - const white/black path      → BSDF default value (unchanged)
      - /Game/Renderer/Texture/…    → UV2 color texture  (original behaviour)
      - any other path              → UV1 base color override (no UV2 link)

    Index → socket:
      6  Color / Color Override → Base Color (× AO if present)
      7  Normal                 → Normal Map → Normal
      8  Roughness              → Roughness
      9  AO                    → Multiply with Color before Base Color
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
        return hashes[i] if i < len(hashes) else None

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_10_CX_UV2, 0)

    def make_tex_uv1(game_path, color_space, loc):
        """Load a texture on UV1 (default coordinates — no Vector link)."""
        img = load_image(game_path, tex_root, tex_ext)
        if img is None:
            return None
        n = nt.nodes.new('ShaderNodeTexImage')
        n.location = loc
        n.image = img
        try:
            img.colorspace_settings.name = color_space
        except Exception:
            pass
        return n

    def make_tex(game_path, color_space, loc):
        """Load a texture on UV2."""
        n = make_tex_uv1(game_path, color_space, loc)
        if n:
            links.new(uv2.outputs['UV'], n.inputs['Vector'])
        return n

    # Index 9: AO — built first to wire into the Mix node
    ao_node = None
    ao_path = slot(9)
    if ao_path and not _is_const_white(ao_path) and not _is_const_black(ao_path):
        ao_node = make_tex(ao_path, 'Non-Color', (_10_CX_TEX, 100))
        if ao_node:
            ao_node.label = 'AO (UV2)'

    # Index 6: Color — UV2 when the path is a Renderer/Texture constant,
    # UV1 override otherwise (non-renderer game assets are real albedo maps).
    color_path = slot(5)
    if _is_const_white(color_path):
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    elif _is_const_black(color_path):
        bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    elif color_path:
        if _is_renderer_texture(color_path):
            bc_node = make_tex(color_path, 'sRGB', (_10_CX_TEX, 400))
            bc_label = 'Color (UV2)'
        else:
            bc_node = make_tex_uv1(color_path, 'sRGB', (_10_CX_TEX, 400))
            bc_label = 'Color Override (UV1)'
        if bc_node:
            bc_node.label = bc_label
            if ao_node:
                mix = nt.nodes.new('ShaderNodeMixRGB')
                mix.blend_type = 'MULTIPLY'
                mix.inputs['Fac'].default_value = 1.0
                mix.location = (_10_CX_MID, 280)
                mix.label = 'Color × AO'
                links.new(bc_node.outputs['Color'], mix.inputs['Color1'])
                links.new(ao_node.outputs['Color'],  mix.inputs['Color2'])
                links.new(mix.outputs['Color'], bsdf.inputs['Base Color'])
            else:
                links.new(bc_node.outputs['Color'], bsdf.inputs['Base Color'])

    # Index 7: Normal
    nrm_path = slot(7)
    if nrm_path and not _is_const_white(nrm_path) and not _is_const_black(nrm_path):
        n = make_tex(nrm_path, 'Non-Color', (_10_CX_TEX, -100))
        if n:
            n.label = 'Normal (UV2)'
            nrm_out = n.outputs['Color']
            if bpy.app.version < (5, 2, 0):
                crv = _make_y_invert_curves(nt, (_10_CX_MID - 300, -100))
                links.new(nrm_out, crv.inputs['Color'])
                nrm_out = crv.outputs['Color']
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.location = (_10_CX_MID, -100)
            _set_normal_convention(nm)
            links.new(nrm_out, nm.inputs['Color'])
            links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Index 8: Roughness
    rgh_path = slot(8)
    if _is_const_white(rgh_path):
        bsdf.inputs['Roughness'].default_value = 1.0
    elif _is_const_black(rgh_path):
        bsdf.inputs['Roughness'].default_value = 0.0
    elif rgh_path:
        n = make_tex(rgh_path, 'Non-Color', (_10_CX_TEX, -350))
        if n:
            n.label = 'Roughness (UV2)'
            links.new(n.outputs['Color'], bsdf.inputs['Roughness'])


# -- Extended layout (11-12 slots) ---------------------------

_EX_CX_UV2  = -1700
_EX_CX_TEX  = -1300
_EX_CX_MID  =  -650
_EX_CX_BSDF =   200
_EX_CX_OUT  =   600


def _setup_nodes_extended(mat, hashes: list, tex_root: str, tex_ext: str) -> None:
    """11/12-slot layout: UV1 base textures + UV2 detail textures.

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
        return hashes[i] if i < len(hashes) else None

    uv2 = nt.nodes.new('ShaderNodeUVMap')
    uv2.uv_map   = "UVMap2"
    uv2.label    = "UV Channel 2"
    uv2.location = (_EX_CX_UV2, -1300)

    def make_tex(game_path, color_space, loc, use_uv2=False):
        img = load_image(game_path, tex_root, tex_ext)
        if img is None:
            return None
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.location = loc
        tex.image = img
        try:
            img.colorspace_settings.name = color_space
        except Exception:
            pass
        if use_uv2:
            links.new(uv2.outputs['UV'], tex.inputs['Vector'])
        return tex

    if is_12:
        IDX_DET_METAL = 8
        IDX_DET_NRM   = 9
        IDX_DET_RGH   = 10
        IDX_DET_AO    = 11
    else:
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
                m = nt.nodes.new('ShaderNodeMixRGB')
                m.blend_type = 'MULTIPLY'
                m.inputs['Fac'].default_value = 1.0
                m.location = (_EX_CX_MID, -900)
                m.label = 'AO × Detail AO'
                links.new(ao_tex_out,              m.inputs['Color1'])
                links.new(det_ao.outputs['Color'], m.inputs['Color2'])
                final_ao_out = m.outputs['Color']
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

    # Slot 6: Color Override — replaces slot 0
    ov_path = slot(6)
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
            m = nt.nodes.new('ShaderNodeMixRGB')
            m.blend_type = 'MULTIPLY'
            m.inputs['Fac'].default_value = 1.0
            m.location = (_EX_CX_MID, 600)
            m.label = 'Color × AO'
            links.new(bc_out,       m.inputs['Color1'])
            links.new(final_ao_out, m.inputs['Color2'])
            links.new(m.outputs['Color'], bsdf.inputs['Base Color'])
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
                    m = nt.nodes.new('ShaderNodeMixRGB')
                    m.blend_type = 'MULTIPLY'
                    m.inputs['Fac'].default_value = 1.0
                    m.location = (_EX_CX_MID, 100)
                    m.label = 'Metal × Detail Metal'
                    links.new(met_out,                  m.inputs['Color1'])
                    links.new(det_met.outputs['Color'], m.inputs['Color2'])
                    links.new(m.outputs['Color'], bsdf.inputs['Metallic'])
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
                m = nt.nodes.new('ShaderNodeMixRGB')
                m.blend_type = 'OVERLAY'
                m.inputs['Fac'].default_value = 1.0
                m.location = (_EX_CX_MID - 150, 300)
                m.label = 'Normal Overlay Detail'
                links.new(nrm_tex_out,              m.inputs['Color1'])
                links.new(det_nrm.outputs['Color'], m.inputs['Color2'])
                nrm_tex_out = m.outputs['Color']
            else:
                nrm_tex_out = det_nrm.outputs['Color']

    if nrm_tex_out is not None:
        if bpy.app.version < (5, 2, 0):
            # Place the curves node where NM would normally sit, then push NM right.
            crv = _make_y_invert_curves(nt, (_EX_CX_MID + 100, 300))
            links.new(nrm_tex_out, crv.inputs['Color'])
            nrm_tex_out = crv.outputs['Color']
            nm_x = _EX_CX_MID + 380
        else:
            nm_x = _EX_CX_MID + 100
        nm = nt.nodes.new('ShaderNodeNormalMap')
        nm.location = (nm_x, 300)
        _set_normal_convention(nm)
        links.new(nrm_tex_out, nm.inputs['Color'])
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


# ============================================================
#  Dispatcher
# ============================================================

def setup_material_nodes(mat, hashes: list, tex_root: str, tex_ext: str) -> None:
    """Choose and run the correct node-setup function for *hashes*.

    >= 11 → _setup_nodes_extended   (UV1 base + UV2 detail)
    == 10 → _setup_nodes_10slot     (fields 1-6 ignored; 7-10 on UV2)
    ==  9 → _setup_nodes_9slot      (slots 0-5 standard + override/detail on 6,8,9)
    <=  8 → _setup_nodes_standard   (classic layout, slots 0-6)
    """
    if len(hashes) >= 11:
        _setup_nodes_extended(mat, hashes, tex_root, tex_ext)
    elif len(hashes) == 10:
        _setup_nodes_10slot(mat, hashes, tex_root, tex_ext)
    elif len(hashes) == 9:
        _setup_nodes_9slot(mat, hashes, tex_root, tex_ext)
    else:
        _setup_nodes_standard(mat, hashes, tex_root, tex_ext)