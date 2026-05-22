"""
parser.py
Binary reader, UE4 property skipper, import hash table, and scene builder.

Sections
--------
1. BinReader         - little-endian byte-buffer reader with UE4 helpers
2. skip_properties   - advance past a UE4 tagged property list
3. Hash table        - build_import_hash_table, import_ref_to_hash
4. Scene builder     - build_objects_from_component
"""

import struct
import bpy
import numpy as np
from mathutils import Vector, Quaternion

from .material import setup_material_nodes, resolve_hash


# ============================================================
#  1. BinReader
# ============================================================

class BinReader:
    """Stateful little-endian byte-buffer reader with UE4 helpers."""

    __slots__ = ('data', 'pos')

    def __init__(self, data: bytes):
        self.data = data
        self.pos  = 0

    def tell(self) -> int:
        return self.pos

    def seek(self, pos: int) -> None:
        self.pos = pos

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def read_byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_sbyte(self) -> int:
        return struct.unpack('<b', self.read(1))[0]

    def read_uint16(self) -> int:
        return struct.unpack('<H', self.read(2))[0]

    def read_int32(self) -> int:
        return struct.unpack('<i', self.read(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack('<I', self.read(4))[0]

    def read_int64(self) -> int:
        return struct.unpack('<q', self.read(8))[0]

    def read_uint64(self) -> int:
        return struct.unpack('<Q', self.read(8))[0]

    def read_float(self) -> float:
        return struct.unpack('<f', self.read(4))[0]

    def read_ue4_string(self) -> str:
        b1 = self.read_byte()
        b2 = self.read_byte()
        if b1 != 0:
            chars = [chr(self.read_byte()) for _ in range(b2) if not self.read_byte()]
        else:
            chars = [chr(self.read_byte()) for _ in range(b2)]
        return ''.join(chars)

    @staticmethod
    def half_to_float(h: int) -> float:
        """Decode a uint16 half-float to a Python float.

        Kept for API compatibility; bulk UV decode now uses NumPy's
        native float16 -> float32 conversion (see build_objects_from_component).
        """
        sign = (h & 0x8000) >> 8
        exp  = h & 0x7C00
        if exp:
            exp = (exp + 0x1C000) >> 3
        mant = (h & 0x03FF) << 5
        bits = (sign << 24) | (exp << 16) | (mant << 8)
        return struct.unpack('>f', struct.pack('>I', bits))[0]


# ============================================================
#  2. skip_properties
# ============================================================

def skip_properties(reader: BinReader, name_table: list) -> None:
    """Advance *reader* past a UE4 tagged property list."""
    while True:
        name_idx = reader.read_int64()
        if name_idx < 0 or name_idx >= len(name_table):
            break
        if name_table[name_idx] == "None":
            break
        type_idx  = reader.read_int64()
        prop_type = name_table[type_idx] if 0 <= type_idx < len(name_table) else "?"
        size      = reader.read_int32()
        reader.read_int32()  # unknown padding

        if prop_type == "IntProperty":
            reader.read_byte(); reader.read_int32()
        elif prop_type == "StructProperty":
            reader.read_int64(); reader.read_byte()
            reader.read_int64(); reader.read_int64()
            reader.read(size)
        elif prop_type == "ObjectProperty":
            reader.read_byte(); reader.read_int32()
        elif prop_type == "FloatProperty":
            reader.read_byte(); reader.read_float()
        elif prop_type == "BoolProperty":
            reader.read_byte(); reader.read_byte()
        elif prop_type == "NameProperty":
            reader.read_byte(); reader.read_int32(); reader.read_int32()
        elif prop_type == "ArrayProperty":
            reader.read_int64(); reader.read_byte(); reader.read(size)
        elif prop_type == "MapProperty":
            reader.read_int64(); reader.read_int64()
            reader.read_byte(); reader.read(size)
        elif prop_type == "ByteProperty":
            reader.read_int64(); reader.read_byte(); reader.read(size)
        elif prop_type == "StrProperty":
            reader.read_byte()
            strlen = reader.read_int32()
            reader.read(strlen)
        elif prop_type == "QWordProperty":
            reader.read_byte(); reader.read_uint64()
        elif prop_type == "EnumProperty":
            reader.read_byte(); reader.read_int64(); reader.read_int64()
        else:
            reader.read_byte(); reader.read(size)


# ============================================================
#  3. Hash table
# ============================================================

def build_import_hash_table(umap_data: bytes, offset3: int, import_table_end: int) -> list:
    """Read the flat uint64 hash table from the umap header.

    Hashes are stored as raw big-endian hex strings matching the literal
    byte ordering in the file.
    """
    hashes = []
    pos = offset3
    while pos + 8 <= import_table_end:
        hashes.append(umap_data[pos:pos + 8].hex().upper())
        pos += 8
    return hashes


def import_ref_to_hash(ref: int, import_hashes: list) -> str | None:
    """Convert a negative import reference to a hash string."""
    if ref >= 0:
        return None
    idx = -ref - 1
    if 0 <= idx < len(import_hashes):
        return import_hashes[idx]
    return None


# ============================================================
#  4. Scene builder
# ============================================================

def get_or_create_scene_collection(context, name: str):
    """Return an existing scene collection by name, or create/link it."""
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if context.scene.collection.children.get(coll.name) is None:
        context.scene.collection.children.link(coll)
    return coll


def build_objects_from_component(
        context,
        base_name: str,
        combined: bytes,
        umap_len: int,
        comp_offset: int,
        reader: BinReader,
        name_table: list,
        import_hashes: list,
        *,
        scale_factor: float = 1.0,
        create_originals: bool = True,
        lod_bias: int = 0,
        lod_mode: str = "LEVEL",
        lod_quality: float = 1.0,
        lod_level: int = 0,
        import_names: list | None = None,
        tex_root: str = "",
        tex_ext: str = "dds",
        tex_index: "dict | None" = None,
) -> None:
    """Parse ONE MassiveEnvironmentComponent0 and populate the Blender scene."""

    lod_bias = max(0, int(lod_bias))
    lod_mode = (lod_mode or "LEVEL").upper()
    if lod_mode not in {"QUALITY", "LEVEL", "ALL"}:
        lod_mode = "LEVEL"
    lod_quality = max(0.0, min(1.0, float(lod_quality)))
    lod_level = min(0, int(lod_level))
    if lod_mode == "LEVEL":
        lod_bias = max(lod_bias, -lod_level)
    import_all_lods = lod_mode == "ALL"

    reader.seek(comp_offset)
    skip_properties(reader, name_table)

    reader.read_int32()   # unknown
    reader.read_int32()   # unknown
    num_groups = reader.read_int32()
    print(f"Groups: {num_groups}")
    print(f"LOD mode: {lod_mode}")
    if lod_mode == "QUALITY":
        print(f"LOD quality: {lod_quality:.3f}")
    elif lod_mode == "LEVEL":
        print(f"LOD level: {-lod_bias}")
    print(f"Import all LODs: {import_all_lods}")

    root_coll = get_or_create_scene_collection(context, base_name)

    for group_idx in range(num_groups):
        print(f"\n--- Group {group_idx} ---")

        # Group header
        reader.read_int32()   # hdr_a
        reader.read(32)       # hdr_b
        reader.read_int32()   # hdr_c
        reader.read_int32()   # hdr_d
        ubulk_data_offset = reader.read_int32()
        reader.read_int32()   # unknown

        mat_refs_primary_count   = reader.read_int32()
        mat_refs19 = list(struct.unpack_from(f'<{mat_refs_primary_count}i', combined, reader.tell()))
        reader.read(mat_refs_primary_count * 4)
        mat_refs_secondary_count   = reader.read_int32()
        mat_refs20 = list(struct.unpack_from(f'<{mat_refs_secondary_count}i', combined, reader.tell()))
        reader.read(mat_refs_secondary_count * 4)
        mat_refs   = mat_refs19 + mat_refs20

        f0, f1, f2 = reader.read_float(), reader.read_float(), reader.read_float()
        f3, f4, f5 = reader.read_float(), reader.read_float(), reader.read_float()
        bbox_cx = (f0 + f3) * 0.5
        bbox_cy = (f1 + f4) * 0.5
        bbox_cz = (f2 + f5) * 0.5
        print(f"  bbox_center=({bbox_cx:.1f},{bbox_cy:.1f},{bbox_cz:.1f})")

        reader.read_byte()
        for _ in range(7):
            reader.read_int32()

        tri_group_cnt    = reader.read_int32()
        off_tri_grp_elem = reader.read_int32()
        for _ in range(13):
            reader.read_int32()

        vertex_index_count = reader.read_int32()
        vertex_index_offset = reader.read_int32()
        reader.read_int32()

        packed_triangle_count = reader.read_int32()
        packed_triangle_offset = reader.read_int32()
        reader.read_int32()

        vertex_data_stride_total  = reader.read_int32()
        vert_cnt       = vertex_data_stride_total // 3
        vertex_positions_offset = reader.read_int32()
        reader.read_int32()
        reader.read_int32()

        off_uv = reader.read_int32()
        reader.read_int32()
        reader.read_int32()

        off_normal = reader.read_int32()
        reader.read_int32()

        reader.read(60)
        reader.read(20)
        reader.read(40)

        instance_cnt = reader.read_int32()
        off_instance = reader.read_int32()
        reader.read_int32()

        cluster_cnt = reader.read_int32()
        off_cluster = reader.read_int32()
        reader.read_int32()

        mesh_type_cnt   = reader.read_int32()
        submesh_rec_cnt = reader.read_int32()
        reader.read_int32()
        reader.read_int32()

        mt_indirection_record_count = reader.read_int32()
        mt_indirection_table_offs     = reader.read_int32()
        for _ in range(13):
            reader.read_int32()
        reader.read(20)

        mt_submesh_counts = list(struct.unpack_from(f'<{mesh_type_cnt}i', combined, reader.tell()))
        reader.read(mesh_type_cnt * 4)

        # Submesh records (length28 x 64 bytes) - vectorized: read all 16-int32 records at once.
        if submesh_rec_cnt:
            sub_arr = np.frombuffer(combined, dtype='<i4',
                                    offset=reader.tell(),
                                    count=submesh_rec_cnt * 16).reshape(-1, 16)
            reader.read(submesh_rec_cnt * 64)
            tris_start = sub_arr[:, 1].tolist()
            tris_count = sub_arr[:, 2].tolist()
            vert_start = sub_arr[:, 7].tolist()
            vert_count = sub_arr[:, 8].tolist()
        else:
            tris_start, tris_count, vert_start, vert_count = [], [], [], []

        # Mesh-type cumulative-sum table
        cumsum_entry_count    = mesh_type_cnt * 2 - 1
        cumsum_table = struct.unpack_from(f'<{cumsum_entry_count}i', combined, reader.tell())
        reader.read(cumsum_entry_count * 4)

        mt_submesh_cumsum = []
        for k in range(0, cumsum_entry_count - 1, 2):
            mt_submesh_cumsum.append(cumsum_table[k + 1])
        mt_submesh_cumsum.append((mt_submesh_cumsum[-1] if mt_submesh_cumsum else 0) + cumsum_table[-1])

        lod_counts = [
            mt_submesh_cumsum[i] - (mt_submesh_cumsum[i - 1] if i > 0 else 0)
            for i in range(len(mt_submesh_cumsum))
        ]
        if lod_counts:
            print(f"  LOD levels available: {max(lod_counts)}")
            #if len(set(lod_counts)) > 1:
                #print(f"  LOD levels by mesh type: {lod_counts}")
        else:
            print("  LOD levels available: 0")

        def mt_sub_range(mt: int):
            s = mt_submesh_cumsum[mt - 1] if mt > 0 else 0
            return s, mt_submesh_cumsum[mt]

        def selected_submesh_for_mt(mt: int) -> int | None:
            if mt < 0 or mt >= len(mt_submesh_cumsum):
                return None
            sub_s, sub_e = mt_sub_range(mt)
            if sub_s >= sub_e:
                return None
            if lod_mode == "QUALITY":
                lod_count = sub_e - sub_s
                lod_offset = round((1.0 - lod_quality) * (lod_count - 1))
                return sub_s + lod_offset
            return min(sub_s + lod_bias, sub_e - 1)

        def selected_submeshes_for_mt(mt: int) -> list[int]:
            if mt < 0 or mt >= len(mt_submesh_cumsum):
                return []
            sub_s, sub_e = mt_sub_range(mt)
            if sub_s >= sub_e:
                return []
            if import_all_lods:
                return list(range(sub_s, sub_e))
            selected = selected_submesh_for_mt(mt)
            return [] if selected is None else [selected]

        # MT -> mat_refs indirection table - vectorized read; truncate each row at first -1.
        _MT_STRIDE  = 112
        mt_indirection_table_base = umap_len + ubulk_data_offset + mt_indirection_table_offs
        mt_mat_indices = []
        if mt_indirection_record_count:
            # Each record is 28 int32s = 112 bytes (matches _MT_STRIDE), so contiguous.
            mt_indir_arr = np.frombuffer(
                combined, dtype='<i4',
                offset=mt_indirection_table_base,
                count=mt_indirection_record_count * 28,
            ).reshape(-1, 28)
            for row in mt_indir_arr:
                neg = np.flatnonzero(row == -1)
                if neg.size:
                    mt_mat_indices.append(row[:neg[0]].tolist())
                else:
                    mt_mat_indices.append(row.tolist())

        base_ubulk = umap_len + ubulk_data_offset

        # Instance transforms - vectorized: read cluster ids, positions, quats with NumPy.
        # Layout per instance (32 bytes):
        #   [0:4]   uint32 cluster_raw  (high bit masked off)
        #   [4:16]  3 x float32 px,py,pz
        #   [16:32] 4 x float32 qx,qy,qz,qw
        if instance_cnt:
            inst_off  = base_ubulk + off_instance
            inst_u32  = np.frombuffer(combined, dtype='<u4',
                                      offset=inst_off,
                                      count=instance_cnt * 8).reshape(-1, 8)
            inst_f32  = np.frombuffer(combined, dtype='<f4',
                                      offset=inst_off,
                                      count=instance_cnt * 8).reshape(-1, 8)
            inst_cluster = (inst_u32[:, 0] & 0x7FFFFFFF).tolist()

            # World-space positions with bbox-center offset and Y flip.
            pos_arr      = inst_f32[:, 1:4].astype(np.float32, copy=True)
            pos_arr[:, 0] += bbox_cx
            pos_arr[:, 1] += bbox_cy
            pos_arr[:, 2] += bbox_cz
            pos_arr[:, 1] = -pos_arr[:, 1]

            # Quaternion conversion: (qx,qy,qz,qw) -> Quaternion(-qw, qx, -qy, qz)
            quat_in  = inst_f32[:, 4:8]
            quat_arr = np.empty((instance_cnt, 4), dtype=np.float32)
            quat_arr[:, 0] = -quat_in[:, 3]   # w = -qw
            quat_arr[:, 1] =  quat_in[:, 0]   # x =  qx
            quat_arr[:, 2] = -quat_in[:, 1]   # y = -qy
            quat_arr[:, 3] =  quat_in[:, 2]   # z =  qz

            inst_positions = [Vector(p) for p in pos_arr.tolist()]
            inst_quats     = [Quaternion(q) for q in quat_arr.tolist()]
        else:
            inst_cluster, inst_positions, inst_quats = [], [], []

        # Cluster records - only first 8 bytes of each 144-byte record are used.
        if cluster_cnt:
            cl_arr = np.frombuffer(combined, dtype='<i4',
                                   offset=base_ubulk + off_cluster,
                                   count=cluster_cnt * 36).reshape(-1, 36)
            cl_mt_start = cl_arr[:, 0].tolist()
            cl_mt_end   = cl_arr[:, 1].tolist()
        else:
            cl_mt_start, cl_mt_end = [], []

        # Vertex positions - single np.frombuffer; Y flip vectorized.
        positions_arr = np.frombuffer(combined, dtype='<f4',
                                      offset=base_ubulk + vertex_positions_offset,
                                      count=vert_cnt * 3).reshape(-1, 3).astype(np.float32, copy=True)
        positions_arr[:, 1] = -positions_arr[:, 1]

        # Vertex normals (8 bytes: 4 tangent | 3 signed-byte normals /128 | 1 sign)
        # Vectorized: read all 8-byte records as int8, slice columns 4..6 for nx,ny,nz.
        normals_raw = np.frombuffer(combined, dtype=np.int8,
                                    offset=base_ubulk + off_normal,
                                    count=vert_cnt * 8).reshape(-1, 8)
        nxyz = normals_raw[:, 4:7].astype(np.float32) * (1.0 / 128.0)
        nxyz[:, 1] = -nxyz[:, 1]
        # Normalize (matches Vector.normalized(); leaves zero vectors at zero)
        lens = np.sqrt((nxyz * nxyz).sum(axis=1, keepdims=True))
        np.maximum(lens, 1e-30, out=lens)  # avoid div-by-zero; tiny values left effectively zero
        normals_arr = nxyz / lens

        # UV channels (half-float, V flipped; stride 4 = 1ch, stride 8 = 2ch)
        # Vectorized: NumPy's native float16 -> float32 cast (one C call) replaces the per-vertex
        # struct.pack/unpack pair in BinReader.half_to_float (the dominant per-vertex cost).
        uv_stride = (off_normal - off_uv) // vert_cnt if vert_cnt else 4
        num_uv_ch = uv_stride // 4
        if vert_cnt and num_uv_ch:
            half_count = vert_cnt * num_uv_ch * 2
            uvs_arr = np.frombuffer(combined, dtype='<f2',
                                    offset=base_ubulk + off_uv,
                                    count=half_count).astype(np.float32).reshape(vert_cnt, num_uv_ch * 2)
            # Flip V on every odd column (1, 3, ...)
            uvs_arr[:, 1::2] = 1.0 - uvs_arr[:, 1::2]
        else:
            uvs_arr = np.empty((vert_cnt, num_uv_ch * 2), dtype=np.float32)
        has_uv2 = num_uv_ch >= 2

        # Triangle group records - 16 bytes each: skip 4, then base/ntri/dataoff (3 x int32).
        if tri_group_cnt:
            tg_arr = np.frombuffer(combined, dtype='<i4',
                                   offset=base_ubulk + off_tri_grp_elem,
                                   count=tri_group_cnt * 4).reshape(-1, 4)
            tg_base    = tg_arr[:, 1].tolist()
            tg_ntri    = tg_arr[:, 2].tolist()
            tg_dataoff = tg_arr[:, 3].tolist()
        else:
            tg_base, tg_ntri, tg_dataoff = [], [], []

        vertex_index_buffer    = np.frombuffer(combined, dtype='<i4',
                                               count=vertex_index_count,
                                               offset=base_ubulk + vertex_index_offset)
        packed_triangle_buffer = np.frombuffer(combined, dtype='<i4',
                                               count=packed_triangle_count,
                                               offset=base_ubulk + packed_triangle_offset)

        def get_triangles(sub_idx):
            """Return an (N, 3) int32 numpy array of remapped triangle indices."""
            vs = vert_start[sub_idx]
            parts = []
            for grp in range(tris_start[sub_idx], tris_start[sub_idx] + tris_count[sub_idx]):
                if grp >= tri_group_cnt:
                    break
                base  = tg_base[grp]
                doff  = tg_dataoff[grp]
                ntri  = tg_ntri[grp]
                packed = packed_triangle_buffer[doff:doff + ntri]
                i0 = vertex_index_buffer[base + (packed & 0x3FF)]         - vs
                i1 = vertex_index_buffer[base + ((packed >> 10) & 0x3FF)] - vs
                i2 = vertex_index_buffer[base + ((packed >> 20) & 0x3FF)] - vs
                parts.append(np.stack([i0, i1, i2], axis=1))
            if not parts:
                return np.empty((0, 3), dtype=np.int32)
            return np.concatenate(parts, axis=0)

        # Gather instances per mesh-type
        mesh_instances: dict = {}
        for inst_idx in range(instance_cnt):
            cid = inst_cluster[inst_idx]
            if cid >= cluster_cnt:
                continue
            ipos = inst_positions[inst_idx]
            iquat = inst_quats[inst_idx]
            for mt in range(cl_mt_start[cid], cl_mt_end[cid] + 1):
                selected_submeshes = selected_submeshes_for_mt(mt)
                if not selected_submeshes:
                    continue
                mesh_instances.setdefault(mt, []).append((ipos, iquat))

        # Collections
        inst_coll = bpy.data.collections.new(f"{base_name}_Group{group_idx}_Instances")
        root_coll.children.link(inst_coll)
        orig_coll = None
        if create_originals:
            orig_coll = bpy.data.collections.new(f"{base_name}_Group{group_idx}_Originals")
            root_coll.children.link(orig_coll)

        group_material_map:    dict = {}
        group_material_hashes: dict = {}
        next_mat_id = 1

        def mats_for_mt(mt):
            if mt >= len(mt_mat_indices):
                return []
            result = []
            for idx in mt_mat_indices[mt]:
                if not (0 <= idx < len(mat_refs)):
                    continue
                ref = mat_refs[idx]
                if ref >= 0:
                    continue
                name_idx = -ref - 1
                # Priority 1: metadata JSON
                if import_names is not None and name_idx < len(import_names) and import_names[name_idx]:
                    h = import_names[name_idx]
                else:
                    raw = import_ref_to_hash(ref, import_hashes)
                    # Priority 2: texture_hashes.csv
                    h = resolve_hash(raw) if raw else None
                    # Priority 3: raw hex hash or decimal placeholder
                    if not h:
                        h = raw or f"{name_idx:04d}"
                if h and h not in result:
                    result.append(h)
            return result

        for mt, inst_list in mesh_instances.items():
            mat_hashes = mats_for_mt(mt)
            hash_key   = frozenset(mat_hashes)
            if hash_key not in group_material_map:
                mat_name = f"{base_name}_Group{group_idx}_mat{next_mat_id:03d}"
                group_material_map[hash_key]    = mat_name
                group_material_hashes[mat_name] = mat_hashes
                next_mat_id += 1
            else:
                mat_name = group_material_map[hash_key]

            selected_submeshes = selected_submeshes_for_mt(mt)
            base_submesh = mt_sub_range(mt)[0]
            for sub_s in selected_submeshes:
                if sub_s >= submesh_rec_cnt:
                    continue
                vs = vert_start[sub_s]
                vc = vert_count[sub_s]
                if vc == 0 or vs + vc > vert_cnt:
                    continue

                mesh_name = f"{base_name}_Group{group_idx}_MeshType{mt}"
                lod_index = sub_s - base_submesh
                if import_all_lods or lod_index > 0:
                    mesh_name += f"_LOD{lod_index}"
                mesh = bpy.data.meshes.new(mesh_name)

                # Per-mesh slices (numpy views/copies)
                verts_slice = positions_arr[vs:vs + vc] * scale_factor   # (vc, 3) float32
                norms_slice = normals_arr[vs:vs + vc]                    # (vc, 3) float32
                uvs_slice   = uvs_arr[vs:vs + vc]                        # (vc, num_uv_ch*2) float32

                faces_arr = get_triangles(sub_s)                         # (N, 3) int32
                mesh.from_pydata(verts_slice.tolist(), [], faces_arr.tolist())

                # UVs via foreach_set - one C call per layer instead of a Python loop over loops.
                loop_count = len(mesh.loops)
                if loop_count:
                    loop_vi = np.empty(loop_count, dtype=np.int32)
                    mesh.loops.foreach_get("vertex_index", loop_vi)

                    uv_layer0 = mesh.uv_layers.new(name="UVMap")
                    uv0 = np.ascontiguousarray(uvs_slice[loop_vi, 0:2], dtype=np.float32).ravel()
                    uv_layer0.data.foreach_set("uv", uv0)

                    if has_uv2:
                        uv_layer1 = mesh.uv_layers.new(name="UVMap2")
                        uv1 = np.ascontiguousarray(uvs_slice[loop_vi, 2:4], dtype=np.float32).ravel()
                        uv_layer1.data.foreach_set("uv", uv1)
                else:
                    # Empty mesh - still create the layers so material setup is consistent.
                    mesh.uv_layers.new(name="UVMap")
                    if has_uv2:
                        mesh.uv_layers.new(name="UVMap2")

                if bpy.app.version < (4, 1, 0):
                    mesh.use_auto_smooth = True
                mesh.normals_split_custom_set_from_vertices(norms_slice.tolist())

                if mat_name:
                    mat = bpy.data.materials.get(mat_name)
                    if mat is None:
                        mat = bpy.data.materials.new(mat_name)
                        hashes = group_material_hashes[mat_name]
                        for hash_idx, hash_val in enumerate(hashes, start=1):
                            mat[f"UE import {hash_idx:02d}"] = hash_val
                        setup_material_nodes(mat, hashes, tex_root, tex_ext,
                                             tex_index=tex_index)
                    mesh.materials.append(mat)
                    npoly = len(mesh.polygons)
                    if npoly:
                        mesh.polygons.foreach_set("material_index", np.zeros(npoly, dtype=np.int32))

                if orig_coll:
                    orig_obj = bpy.data.objects.new(f"{mesh_name}_Original", mesh)
                    orig_coll.objects.link(orig_obj)
                    orig_obj.display_type = 'WIRE'
                    orig_obj.hide_set(True)

                for inst_idx, (ipos, quat) in enumerate(inst_list):
                    inst_obj = bpy.data.objects.new(f"{mesh_name}_Inst{inst_idx}", mesh)
                    inst_obj.location            = ipos * scale_factor
                    inst_obj.rotation_mode       = 'QUATERNION'
                    inst_obj.rotation_quaternion = quat
                    inst_coll.objects.link(inst_obj)

        print(f"  Group {group_idx}: {sum(len(v) for v in mesh_instances.values())} instances "
              f"across {len(mesh_instances)} mesh types.")

    print("\nImport finished.")
