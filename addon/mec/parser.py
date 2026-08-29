"""
parser.py
Binary reader, UE4 property skipper, import hash table, and scene builder.

Sections
--------
1. BinReader         - little-endian byte-buffer reader with UE4 helpers
2. skip_properties   - advance past a UE4 tagged property list
3. Export layout     - seek_group_array, decode_tangent_frame
4. Hash table        - build_import_hash_table, import_ref_to_hash
5. Scene builder     - build_objects_from_component

Naming
------
Local names follow Square Enix's own, recovered from shader symbols in a
RenderDoc capture of the running game. Massive Environment is a GPU-driven
meshlet renderer: culling runs in compute, and geometry is drawn by mesh shaders
via ExecuteIndirect with DISPATCH_MESH. Nothing goes through the input assembler,
so the mesh shader pulls every stream itself from bindless SRV arrays:

    ControlPointsSRVs        space1   StructuredBuffer<FMassiveEnvironmentControlPoint>
    PrimitiveInfosSRVs       space2   StructuredBuffer<FMassiveEnvironmentPrimitiveInfo>
    MeshletsSRVs             space3   StructuredBuffer<FMeshlet>
    UniqueVertexIndicesSRVs  space4   Buffer<uint>
    PrimitiveIndicesSRVs     space5   Buffer<uint>
    PositionsSRVs            space6   Buffer<float>
    TexcoordsSRVs            space7   Buffer<half2>
    TangentsSRVs             space8   Buffer<half4>
    DeformContextASRVs       space9   ByteAddressBuffer   wind-sway transforms
    DeformContextBSRVs       space10  ByteAddressBuffer   per-vertex ubyte4 weights
    DeformContextCSRVs       space11  ByteAddressBuffer   per-cluster constants
    DeformContextIndicesSRVs space12  StructuredBuffer<int>

The compute passes use a different space numbering for the same bindless idea
(2=ControlPoints, 3=PrimitiveInfos, 4=MeshInfos, 5=MaterialInfos,
6=CurrentMeshMipIndices, 7=CullData, 8=MeshletLODGroups, 9=MeshletGroupIndices,
10=MeshletLODHierarchyNodes), and the base-pass pixel shader a third
(21=MaterialTextures, 22=MaterialInfos).

    struct FMeshlet { int VertexCount, VertexOffset, PrimitiveCount, PrimitiveOffset; };
    struct FMassiveEnvironmentControlPoint { int; float3 Position; float4 Quaternion; };

Culling runs ahead of that in compute - GenerateMassiveEnvironmentBatchedNodesCS,
ComputeMassiveEnvironmentGenerateBatches, then GenerateMassiveEnvironmentBatchedMeshlets
- which between them consume several per-meshlet and per-mesh buffers. All of
them live in the .ubulk; see FORMAT.md for their offsets:

    struct FMeshletCullData { float4; float4; float; float3; float; float3; int; };
    struct FMeshletLODGroup { int x4; float4 x3; };
    struct FMeshletLODHierarchyNode { float4[8]; float4[8]; float4[8]; int[8]; };
    struct FMassiveEnvironmentMeshInfo { int x6; int[2]; float4; float[16]; };
    struct FMassiveEnvironmentMaterialInfo { int x14; float x14; };
    struct FMassiveEnvironmentPrimitiveInfo { int x16; float3; int; float3; int;
                                              float4; float3; float; float3; float; };

FMeshletCullData is 68 bytes since the 2026 update and was 80 before it. Its
first float4 is a bounding centre and radius, which the culling pass rotates by
the control point's quaternion exactly as the mesh shader rotates a vertex.

What this file calls the "cluster" table is FMassiveEnvironmentPrimitiveInfo,
and what it calls "material refs" are texture refs - no material asset is
referenced anywhere in the file pair.

FMassiveEnvironmentMaterialInfo is 112 bytes and is the table this importer reads
for texture identities - see the notes at its read site below.

What this file calls a "group" is one culling group - one entry in each bindless
array, and one contiguous block of the .ubulk. The shader's per-group constants
live in a MassiveEnvironmentCullingGroups cbuffer of 128 entries: RenderDataCounters,
TextureOffsets, RenderDataPositions and RenderDataLODParameters.

The shader composes a vertex as

    world = MassiveEnvironmentOrigin
          + RenderDataPositions[group]
          + controlPoint.Position
          + rotate(controlPoint.Quaternion, position)

MassiveEnvironmentOrigin is a per-continent offset, not per map, and is
deliberately not applied here - see the README.

Round-trip fields
-----------------
Header and record fields this importer reads but has no Blender concept to
hold - unknown fields, and known ones with no rendering effect - are stashed
as `mec_*` custom properties on the collection/mesh/object that best matches
their scope, rather than discarded, so a future exporter can read a value back
out instead of having to recompute or guess it. Names mostly match FORMAT.md's
own field names. Not covered: per-meshlet clustering (Blender flattens it into
an ordinary face list) and any submesh a LOD_mode/LOD_bias setting caused this
importer to skip reading entirely.
"""

import struct
from collections import defaultdict

import bpy
import numpy as np
from mathutils import Vector, Quaternion

from .material import setup_material_nodes, resolve_hash


OPPOSITE_FACE_OFFSET = 0.0005
DEFAULT_SCALE_FACTOR = 0.01
OPPOSITE_FACE_DOT_THRESHOLD = -0.99
OPPOSITE_FACE_POSITION_DECIMALS = 6


def scaled_opposite_face_offset(scale_factor: float) -> float:
    try:
        scale_ratio = abs(float(scale_factor)) / DEFAULT_SCALE_FACTOR
    except (TypeError, ValueError):
        scale_ratio = 1.0
    return OPPOSITE_FACE_OFFSET * scale_ratio


def offset_opposite_face_geometry(
        verts_arr: np.ndarray,
        normals_arr: np.ndarray,
        faces_arr: np.ndarray,
        *,
        offset: float = OPPOSITE_FACE_OFFSET,
) -> int:
    """Directly offset vertices on overlapping faces with opposite normals."""
    if offset == 0.0 or len(verts_arr) == 0 or len(faces_arr) == 0:
        return 0

    face_groups = defaultdict(list)
    for face_idx, face in enumerate(faces_arr):
        key = tuple(sorted(
            tuple(
                round(float(coord), OPPOSITE_FACE_POSITION_DECIMALS)
                for coord in verts_arr[int(vert_idx)]
            )
            for vert_idx in face
        ))
        face_groups[key].append(face_idx)

    face_vertices = verts_arr[faces_arr]
    face_normals = np.cross(
        face_vertices[:, 1] - face_vertices[:, 0],
        face_vertices[:, 2] - face_vertices[:, 0],
    ).astype(np.float32, copy=False)
    face_normal_lengths = np.sqrt((face_normals * face_normals).sum(axis=1))
    valid_face_normals = face_normal_lengths > 1e-30
    face_normals[valid_face_normals] /= face_normal_lengths[valid_face_normals, None]

    affected_vertices: set[int] = set()
    for grouped_faces in face_groups.values():
        if len(grouped_faces) < 2:
            continue
        for first_pos, face_a in enumerate(grouped_faces):
            if not valid_face_normals[face_a]:
                continue
            normal_a = face_normals[face_a]
            for face_b in grouped_faces[first_pos + 1:]:
                if not valid_face_normals[face_b]:
                    continue
                if float(np.dot(normal_a, face_normals[face_b])) < OPPOSITE_FACE_DOT_THRESHOLD:
                    affected_vertices.update(int(idx) for idx in faces_arr[face_a])
                    affected_vertices.update(int(idx) for idx in faces_arr[face_b])

    if not affected_vertices:
        return 0

    affected_indices = np.fromiter(sorted(affected_vertices), dtype=np.int32)
    offset_dirs = normals_arr[affected_indices].astype(np.float32, copy=True)
    offset_dir_lengths = np.sqrt((offset_dirs * offset_dirs).sum(axis=1))
    valid_offset_dirs = offset_dir_lengths > 1e-30
    if not np.any(valid_offset_dirs):
        return 0

    offset_dirs[valid_offset_dirs] /= offset_dir_lengths[valid_offset_dirs, None]
    valid_indices = affected_indices[valid_offset_dirs]
    verts_arr[valid_indices] += offset_dirs[valid_offset_dirs] * float(offset)
    return int(len(valid_indices))


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
#  3. Export layout
# ============================================================

# Component export data runs: property list, an unknown int32, the constant 4,
# the group count, then the first group header, which always opens with 51.
# A 2026 game update switched exports from UE tagged property serialisation to
# unversioned serialisation, which shortens that property list (37 -> 6 bytes for
# this component) and drops its name indices, so anchor on the trailing signature
# rather than decoding either property encoding.
#
# Group header facts, all confirmed across 213 groups in 80 files from both
# builds. Recorded here because they are what a writer would have to reproduce:
#
#   hdr_a                    always 51
#   hdr_c, hdr_d             always equal to each other, and to this group's
#                            byte size within the .ubulk - that is, the next
#                            group's ubulk offset minus this one's. For the last
#                            group it is the remainder of the file.
#   int32 after ubulk offset  always 0
#   byte after the bbox      always 1
#   bbox                     6 floats, min then max; the centre is their midpoint
#                            and offsets instance positions, not vertices
#   seven[4]                 always repeats the primary material ref count
#   seven[6]                 always repeats hdr_c
#   thirteen[1], [7]         always repeat meshlet_count
#   thirteen[0], [2]         always meshlet_count * 16, the size of the FMeshlet
#                            table this importer reads
#   thirteen[3], [9]         byte sizes of two more per-meshlet buffers that this
#                            importer never reads. The culling compute shader
#                            GenerateMassiveEnvironmentBatchedMeshlets names them:
#                              * 80 pre-2026, * 68 since -> FMeshletCullData
#                              * 4                       -> MeshletGroupIndices
#   thirteen[5]              a third per-meshlet buffer, unidentified and never
#                            read here. Always meshlet_count * 96 pre-2026 and
#                            meshlet_count * 84 since - present in every group of
#                            both builds, just 12 bytes narrower per record, the
#                            same shrink FMeshletCullData took. Likely the same
#                            field cut from both.
#   blk20[0], [1], [3]       restate meshlet_count * 16, the FMeshletCullData
#                            size, and * 4
#   blk60[13]                restates the instance offset
#   thirteen2[0], [4]        restate the FMassiveEnvironmentMaterialInfo table
#                            size (mesh_type_cnt * 112) and meshlet_count
#   material info count      always equals mesh_type_cnt, so there is one
#                            FMassiveEnvironmentMaterialInfo per mesh type
#
# Fields that are zero in every group checked, and so are candidates for a writer
# to leave empty:
#
#   the int32 after the ubulk offset
#   thirteen2[1], [3], [5], [7], [9], [11], [12]   - the odd slots, so that block
#                            reads as (value, padding) pairs
#   blk60[12], blk60[14]
#   blk40[5], blk40[6], blk40[7], blk40[9]
#   submesh record columns 9..14
#   cluster record bytes 16..52, 56..92, 140..144
#
# hdr_b's 32 bytes are mostly structure rather than payload:
#   [0:4]    always zero
#   [4:20]   16 high-entropy bytes, near-uniform byte distribution - almost
#            certainly an FGuid. 123 distinct values over 147 groups, so some
#            groups share one, presumably where they come from the same asset
#   [20]     varies, but only 15 distinct values across every group checked
#   [21:28]  always zero
#   [28:32]  always 01 05 01 00, which looks like a version tag
#
# Still genuinely unexplained: seven[0], [1], [2], [3], [5], mt_pad1, most of
# blk60 and blk40, and thirteen[4], [6], [8], [10], [11], [12].
#   cumsum table length      always mesh_type_cnt * 2 - 1, and its final pair
#                            sums to submesh_rec_cnt
#   secondary material array non-empty in roughly 40% of groups, so it cannot be
#                            assumed away
#
# Buffers are described as (count, offset, byte_size); byte_size / count gives
# the record stride, which is how the tangent-frame and UV strides are read.
GROUP_ARRAY_MARKER = 4
GROUP_HEADER_TAG = 51
MAX_GROUPS = 4096
GROUP_ARRAY_SEARCH_BYTES = 512

# Property lists shorter than this were written with unversioned serialisation,
# which arrived in the same update that packed the vertex tangent frame down to
# four bytes. Used only to break ties - see tangent_frame_stride.
UNVERSIONED_PROPERTY_MAX = 16

PACKED_TANGENT_FRAME_STRIDE = 4
SPLIT_TANGENT_FRAME_STRIDE = 8


def seek_group_array(reader: BinReader, comp_offset: int) -> tuple[int, int]:
    """Skip the component's property list.

    Returns (num_groups, property_bytes) and leaves *reader* on the first group.
    """
    data = reader.data
    limit = min(comp_offset + GROUP_ARRAY_SEARCH_BYTES, len(data) - 12)
    empty_pos = None

    for pos in range(comp_offset, limit):
        if struct.unpack_from('<i', data, pos + 4)[0] != GROUP_ARRAY_MARKER:
            continue
        num_groups = struct.unpack_from('<i', data, pos + 8)[0]
        # Placeholder components hold no geometry and ship without a .ubulk. They
        # have no group header to confirm against, so keep the first one as a
        # fallback and prefer any real group array found later in the window.
        if num_groups == 0:
            if empty_pos is None:
                empty_pos = pos
            continue
        if not 1 <= num_groups <= MAX_GROUPS or pos + 16 > len(data):
            continue
        if struct.unpack_from('<i', data, pos + 12)[0] == GROUP_HEADER_TAG:
            reader.seek(pos + 12)
            return num_groups, pos - comp_offset

    if empty_pos is not None:
        reader.seek(empty_pos + 12)
        return 0, empty_pos - comp_offset

    raise ValueError(
        f"no group array within {GROUP_ARRAY_SEARCH_BYTES} bytes of the "
        f"component at {comp_offset}"
    )


def tangent_frame_stride(tangents_bytes: int, vert_cnt: int,
                         property_bytes: int) -> int:
    """Pick the vertex normal stride for one group.

    The group header states the tangent-frame buffer's byte size, so dividing by
    the vertex count gives the stride directly. That held for all 213 groups
    checked across both builds - 8 before the 2026 update, 4 after.

    The fallback only matters for a malformed or empty group: infer the layout
    from the export's property encoding, since unversioned properties and the
    packed tangent frame arrived together.
    """
    default = (PACKED_TANGENT_FRAME_STRIDE
               if property_bytes < UNVERSIONED_PROPERTY_MAX
               else SPLIT_TANGENT_FRAME_STRIDE)
    if vert_cnt <= 0 or tangents_bytes <= 0:
        return default
    if tangents_bytes % vert_cnt == 0:
        stated = tangents_bytes // vert_cnt
        if stated in (PACKED_TANGENT_FRAME_STRIDE, SPLIT_TANGENT_FRAME_STRIDE):
            return stated
    return default


def decode_tangent_frame(combined: bytes, offset: int, vert_cnt: int,
                         stride: int) -> np.ndarray:
    """Decode the per-vertex normal stream to an unnormalised (N, 3) float32 array.

    Stride 8 is the pre-2026 layout: two FPackedNormals, a tangent followed by
    the normal, whose signed bytes sit at 4..6.

    Stride 4 is the packed tangent frame introduced by the 2026 update, an
    R10G10B10A2_UNORM vertex attribute:
        bits  0-9   u = px + py
        bits 10-19  v = py - px
        bits 20-29  tangent rotation about the normal (unused here)
        bits 30-31  sign bits; bit 0 set means nz >= 0
    px and py are the octahedral projection of the normal onto a square rotated
    45 degrees, which leaves |pz| = 1 - |px| - |py|.

    The game's own mesh shader computes nx = x - y, ny = x + y - 1 from the
    UNORM-decoded channels and takes the sign from round(w * 3) & 1. Substituting
    u = 2x - 1 and v = 2y - 1 turns the form below into exactly that, and for a
    2-bit UNORM alpha, round(w * 3) & 1 is bit 30. The game also ships a half4
    container carrying the same four fields, so the stride can differ while the
    decode does not.
    """
    if stride == PACKED_TANGENT_FRAME_STRIDE:
        packed = np.frombuffer(combined, dtype='<u4', offset=offset, count=vert_cnt)
        u = (packed & 0x3FF).astype(np.float32) * (1.0 / 511.5) - 1.0
        v = ((packed >> 10) & 0x3FF).astype(np.float32) * (1.0 / 511.5) - 1.0
        nx = (u - v) * 0.5
        ny = (u + v) * 0.5
        nz = 1.0 - np.abs(nx) - np.abs(ny)
        nz = np.where((packed >> 30) & 1, nz, -nz).astype(np.float32)
        return np.stack([nx, ny, nz], axis=1)

    raw = np.frombuffer(combined, dtype=np.int8, offset=offset,
                        count=vert_cnt * stride).reshape(-1, stride)
    return raw[:, 4:7].astype(np.float32) * (1.0 / 128.0)


# ============================================================
#  4. Hash table
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

# Texture hashes that fell through to a raw hex/placeholder because neither
# the import metadata JSON nor texture_hashes.csv could name them. Accumulates
# across an entire import batch (import_umap_paths resets it before the file
# loop); the operator reports the final count as a warning once the batch is
# done, since printing per-file would bury the total in per-mesh console spam.
_unresolved_texture_hashes: set = set()


def clear_unresolved_texture_hashes() -> None:
    _unresolved_texture_hashes.clear()


def get_unresolved_texture_hashes() -> set:
    return set(_unresolved_texture_hashes)


DEFORM_BONES = 4

# The sway animation's real source frame rate. Confirmed from DXIL/LLVM-IR
# disassembly of MainInBasePass, MainInPrePass and MainInShadowDepthBatched
# (all three captures): View_EnvironmentTime (real elapsed seconds) is
# multiplied by this exact constant to get the DeformContextA frame index -
# `fmul float %313, 5.994006e+01` in the LLVM IR, i.e. the classic NTSC
# 60 * 1000/1001 rate, not a plain 30 or 60.
DEFORM_SOURCE_FPS = 60000.0 / 1001.0


def read_deform_context(combined, base_ubulk, deform_desc, cluster_arr, vert_cnt):
    """Decode the DeformContext (wind sway) buffers for one group.

    Returns None when the group has no sway data, else a dict with the raw
    buffers plus per-cluster transform tables. Layout, all recovered from the
    base-pass mesh shader (see FORMAT.md):

        A        cluster[16] + frame*64   4 bones x (half4 translation,
                                          half4 quaternion), cluster[48] frames
        B        cluster[24] + idx*4      ubyte4, one 0-255 weight per bone
        C        cluster[32]              4 bones x half3 pivot (8-byte stride)
        Indices  per vertex               int32, -1 means the vertex is rigid
    """
    a_off, b_off = deform_desc[1], deform_desc[4]
    c_off, i_off = deform_desc[7], deform_desc[10]
    if not (deform_desc[2] and deform_desc[5] and deform_desc[8] and deform_desc[11]):
        return None
    if len(cluster_arr) == 0:
        return None

    vert_deform_index = np.frombuffer(combined, dtype='<i4',
                                      offset=base_ubulk + i_off,
                                      count=vert_cnt).astype(np.int32, copy=True)
    if not np.any(vert_deform_index >= 0):
        return None

    clusters = []
    for row in cluster_arr:
        rel_a, rel_b, rel_c = int(row[4]), int(row[6]), int(row[8])
        frames = int(row[12])
        if frames <= 0:
            clusters.append(None)
            continue
        # A: frames x 32 halves -> (frames, 4 bones, 8)
        a = np.frombuffer(combined, dtype='<f2',
                          offset=base_ubulk + a_off + rel_a,
                          count=frames * 32).astype(np.float32).reshape(frames, DEFORM_BONES, 8)
        # C: 4 bones x 4 halves, only xyz used
        c = np.frombuffer(combined, dtype='<f2',
                          offset=base_ubulk + c_off + rel_c,
                          count=DEFORM_BONES * 4).astype(np.float32).reshape(DEFORM_BONES, 4)
        clusters.append({
            'frames':      frames,
            'translation': np.ascontiguousarray(a[:, :, 0:3]),   # (frames, 4, 3)
            'quaternion':  np.ascontiguousarray(a[:, :, 4:8]),   # (frames, 4, xyzw)
            'pivot':       np.ascontiguousarray(c[:, 0:3]),      # (4, 3)
            'weight_base': rel_b,
        })
    if not any(clusters):
        return None
    return {'clusters': clusters, 'vert_deform_index': vert_deform_index,
            'b_off': b_off, 'combined': combined, 'base_ubulk': base_ubulk}


def _quat_rotate(quat, vecs):
    """Rotate (N,3) by a single xyzw quaternion, matching the shader's form."""
    qxyz = quat[0:3]
    t = 2.0 * np.cross(np.broadcast_to(qxyz, vecs.shape), vecs)
    return vecs + quat[3] * t + np.cross(np.broadcast_to(qxyz, t.shape), t)


def deform_frame_positions(deform, cluster_idx, frame, raw_positions,
                           vert_start=0, vert_count=None):
    """One animation frame for [vert_start, vert_start+vert_count), file space.

    Mirrors the mesh shader: each bone rotates the vertex about its pivot, adds
    its translation, and the four results are blended by the ubyte4 weights.
    Whatever weight is left over keeps the vertex at rest, so the four weights
    are amplitudes rather than a normalized skin partition.

    The range matters. A deform index is only meaningful inside the region of
    the cluster that owns the vertex, and the weight arrays of different
    clusters sit back to back in one buffer - feed a cluster vertices it does
    not own and the indices land in a neighbour's weights, which routinely sum
    past 1 and fling the vertex hundreds of units away.
    """
    cluster = deform['clusters'][cluster_idx]
    if cluster is None:
        return None
    if vert_count is None:
        vert_count = len(raw_positions) - vert_start
    idx = deform['vert_deform_index'][vert_start:vert_start + vert_count]
    base = raw_positions[vert_start:vert_start + vert_count]
    moving = idx >= 0
    if not np.any(moving):
        return None

    combined, base_ubulk = deform['combined'], deform['base_ubulk']
    sel = idx[moving]
    byte_off = base_ubulk + deform['b_off'] + cluster['weight_base']
    weight_bytes = np.frombuffer(combined, dtype='<u1',
                                 offset=byte_off,
                                 count=(int(sel.max()) + 1) * 4).reshape(-1, 4)
    weights = weight_bytes[sel].astype(np.float32) / 255.0        # (M, 4)

    moving_pos = base[moving]                                      # (M, 3)
    rest = np.clip(1.0 - weights.sum(axis=1), 0.0, 1.0)[:, None]
    out = moving_pos * rest
    f = frame % cluster['frames']
    for bone in range(DEFORM_BONES):
        w = weights[:, bone:bone + 1]
        if not np.any(w):
            continue
        pivot = cluster['pivot'][bone]
        rotated = _quat_rotate(cluster['quaternion'][f, bone], moving_pos - pivot)
        out += w * (rotated + pivot + cluster['translation'][f, bone])

    result = base.copy()
    result[moving] = out
    return result


def find_eval_time_fcurve(key):
    """Locate the eval_time F-curve on a shape-key datablock's action.

    Blender 5.0 removed the legacy Action.fcurves API in favor of slotted
    actions (fcurves live in a channelbag under action.layers[].strips[],
    keyed by the animation slot); handle both layouts.
    """
    anim = key.animation_data
    if not anim or not anim.action:
        return None
    action = anim.action
    if hasattr(action, "fcurves"):
        return action.fcurves.find("eval_time")
    slot = getattr(anim, "action_slot", None)
    for layer in action.layers:
        for strip in layer.strips:
            channelbag = strip.channelbag(slot) if slot else None
            if channelbag:
                fcurve = channelbag.fcurves.find("eval_time")
                if fcurve:
                    return fcurve
    return None


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
        offset_opposite_faces: bool = False,
        import_sway: bool = True,
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
    num_groups, property_bytes = seek_group_array(reader, comp_offset)
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
        hdr_a = reader.read_int32()
        hdr_b_bytes = reader.read(32)
        hdr_c = reader.read_int32()
        reader.read_int32()   # hdr_d - always equals hdr_c, not stored
        ubulk_data_offset = reader.read_int32()
        base_ubulk = umap_len + ubulk_data_offset
        reader.read_int32()   # unknown, always 0

        n_texture_refs_primary   = reader.read_int32()
        texture_refs_primary = list(struct.unpack_from(f'<{n_texture_refs_primary}i', combined, reader.tell()))
        reader.read(n_texture_refs_primary * 4)
        n_texture_refs_secondary   = reader.read_int32()
        texture_refs_secondary = list(struct.unpack_from(f'<{n_texture_refs_secondary}i', combined, reader.tell()))
        reader.read(n_texture_refs_secondary * 4)
        texture_refs   = texture_refs_primary + texture_refs_secondary

        f0, f1, f2 = reader.read_float(), reader.read_float(), reader.read_float()
        f3, f4, f5 = reader.read_float(), reader.read_float(), reader.read_float()
        bbox_cx = (f0 + f3) * 0.5
        bbox_cy = (f1 + f4) * 0.5
        bbox_cz = (f2 + f5) * 0.5
        print(f"  bbox_center=({bbox_cx:.1f},{bbox_cy:.1f},{bbox_cz:.1f})")

        reader.read_byte()   # always 1, not stored
        seven = [reader.read_int32() for _ in range(7)]

        meshlet_count    = reader.read_int32()
        meshlets_offset = reader.read_int32()
        # Five (count, offset, byte_size) descriptors for the meshlet buffer
        # chain this importer does not read: FMeshlet's own size, then
        # FMeshletCullData, FMeshletLODGroup, MeshletGroupIndices and
        # FMeshletLODHierarchyNode. See FORMAT.md's "meshlet buffer chain".
        thirteen = [reader.read_int32() for _ in range(13)]

        # Buffers are described as (count, offset, byte_size) triplets, and the
        # byte size always works out to count * record stride. Dividing the two
        # therefore states each record's stride outright, which beats inferring
        # it from the gap to the next buffer. Confirmed on 213 groups across both
        # builds; the strides seen are noted against each buffer below.
        unique_vertex_index_count = reader.read_int32()
        unique_vertex_indices_offset = reader.read_int32()
        reader.read_int32()          # byte size = count * 4 (int32 indices)

        primitive_index_count = reader.read_int32()
        primitive_indices_offset = reader.read_int32()
        reader.read_int32()          # byte size = count * 4 (packed int32)

        # The three vertex streams overlap by one field: each descriptor states
        # its own byte size and then the next stream's, so the sizes appear
        # twice. Verified on every group checked in both builds.
        #   positions: (float count, offset, own size, UV size)
        #   uv:        (offset, own size, tangent frame size)
        #   tangent:   (offset, own size)
        # Positions are counted in floats, not vertices, hence the divide by 3.
        position_float_count = reader.read_int32()
        vert_cnt       = position_float_count // 3
        positions_offset = reader.read_int32()
        reader.read_int32()          # own size  = vert_cnt * 12 (3 x float32)
        reader.read_int32()          # restates the UV buffer size below

        texcoords_offset = reader.read_int32()
        texcoords_bytes = reader.read_int32()             # vert_cnt * 4 or * 8
        reader.read_int32()          # restates the tangent frame size below

        tangents_offset = reader.read_int32()
        tangents_bytes = reader.read_int32()   # vert_cnt * 8, or * 4 since 2026

        # DeformContext descriptors: five (count, offset, byte_size) triplets,
        # A / B / C / Indices / unused. Zero in groups with no wind sway.
        deform_desc = list(struct.unpack_from('<15i', combined, reader.tell()))
        reader.read(60)
        reader.read(20)          # blk20 - pure restatement of `thirteen`'s sizes, not stored
        blk40 = list(struct.unpack('<10i', reader.read(40)))

        control_point_count = reader.read_int32()
        control_points_offset = reader.read_int32()
        reader.read_int32()          # byte size = count * 32

        primitive_info_count = reader.read_int32()
        primitive_infos_offset = reader.read_int32()
        reader.read_int32()          # byte size = count * 144

        mesh_type_cnt   = reader.read_int32()
        submesh_rec_cnt = reader.read_int32()
        mesh_infos_offset = reader.read_int32()   # FMassiveEnvironmentMeshInfo table
        reader.read_int32()          # mesh_infos_bytes, always mesh_type_cnt * 112

        material_info_count = reader.read_int32()   # == mesh_type_cnt
        material_infos_offset     = reader.read_int32()
        # thirteen2[6],[8],[10] are the base-mip (coarsest LoD) unique-vertex-index
        # / primitive-index / vertex counts; the rest restate values read above
        # or are always 0. See FORMAT.md's "base-mip subset".
        thirteen2 = [reader.read_int32() for _ in range(13)]
        trailing20 = list(struct.unpack('<5i', reader.read(20)))

        mt_submesh_counts = list(struct.unpack_from(f'<{mesh_type_cnt}i', combined, reader.tell()))
        reader.read(mesh_type_cnt * 4)

        # Submesh records: 64 bytes each, read as 16 int32.
        #   [1] first meshlet          [2] meshlet count
        #   [7] first vertex           [8] vertex count
        # vert_start + vert_count never exceeded vert_cnt in any group checked.
        # Of the rest, columns 9..14 are always zero, and 4, 6 and 8 fall
        # monotonically across a mesh type's LoD chain while 15 rises - so they
        # are per-LoD quantities, but none reads as a plausible float, which
        # rules out LoD screen sizes or distances living here.
        if submesh_rec_cnt:
            sub_arr = np.frombuffer(combined, dtype='<i4',
                                    offset=reader.tell(),
                                    count=submesh_rec_cnt * 16).reshape(-1, 16)
            reader.read(submesh_rec_cnt * 64)
            sub_meshlet_first = sub_arr[:, 1].tolist()
            sub_meshlet_count = sub_arr[:, 2].tolist()
            vert_start = sub_arr[:, 7].tolist()
            vert_count = sub_arr[:, 8].tolist()
        else:
            sub_meshlet_first, sub_meshlet_count, vert_start, vert_count = [], [], [], []

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

        # FMassiveEnvironmentMaterialInfo - 112 bytes, one per mesh type, read as
        # 28 int32 that split into two parallel arrays of 14:
        #
        #   [0:14]   texture indices, unused slots set to -1
        #   [14:28]  the matching texture's world-space size, 0 for unused slots
        #
        # Index i in one half pairs with index i in the other. The streaming pass
        # GenerateMassiveEnvironmentBatchedNodesCS walks the pair and does
        #
        #     if (size[i] > 0)
        #         InterlockedUMax(RequestedTextureSizes[texBase + index[i]],
        #                         screenScale * size[i]);
        #
        # so the float drives the requested streaming resolution, not a draw or
        # cull distance. That earlier reading was wrong: the value does correlate
        # with the mesh's bounding radius (log-log r = 0.86) and is smaller in
        # interiors, but only because a larger object needs a larger texture.
        #
        # This importer only wants the texture identities, so it takes the first
        # half and stops at the -1. The game treats size[i] > 0 as the validity
        # test; the two agreed on every mesh type checked.
        _MT_STRIDE  = 112
        material_infos_base = base_ubulk + material_infos_offset
        mt_mat_indices = []
        if material_info_count:
            # Each record is 112 bytes: 14 int32 texture indices followed by 14
            # floats. Only the first half is indices - scanning the whole 28-wide
            # row for the -1 terminator runs into the float half, which is
            # -1 only if its bits are exactly 0xFFFFFFFF, so a fully-populated
            # 14-slot record would hand back all 28 columns.
            material_info_raw = np.frombuffer(
                combined, dtype='<i4',
                offset=material_infos_base,
                count=material_info_count * 28,
            ).reshape(-1, 28)
            material_info_sizes = np.frombuffer(
                combined, dtype='<f4',
                offset=material_infos_base,
                count=material_info_count * 28,
            ).reshape(-1, 28)[:, 14:]
            # The game's own validity test is size[i] > 0, and the valid slots
            # are always a dense prefix, so the count doubles as the length.
            for row, sizes in zip(material_info_raw[:, :14], material_info_sizes):
                mt_mat_indices.append(row[:int((sizes > 0).sum())].tolist())

        # FMassiveEnvironmentMeshInfo - 112 bytes, one per mesh type. The offset
        # was previously miscatalogued as unidentified and this table was never
        # read at all. Not needed by this importer's own logic; read here purely
        # so the bounding sphere and per-LoD error thresholds are available to
        # stash as mesh properties below instead of being silently skipped.
        meshinfo_base = base_ubulk + mesh_infos_offset
        if mesh_type_cnt:
            meshinfo_ints_arr = np.frombuffer(combined, dtype='<i4', offset=meshinfo_base,
                                              count=mesh_type_cnt * 28).reshape(-1, 28)
            meshinfo_floats_arr = np.frombuffer(combined, dtype='<f4', offset=meshinfo_base,
                                                count=mesh_type_cnt * 28).reshape(-1, 28)
        else:
            meshinfo_ints_arr = np.empty((0, 28), dtype=np.int32)
            meshinfo_floats_arr = np.empty((0, 28), dtype=np.float32)

        # FMassiveEnvironmentControlPoint: 32 bytes = { int; float3 Position;
        # float4 Quaternion }. One control point is one placement of a mesh.
        #   [0:4]   uint32   cluster index, high bit masked off
        #   [4:16]  float3   Position
        #   [16:32] float4   Quaternion, stored (x, y, z, w)
        # The shader rotates by v + 2w(q x v) + 2q x (q x v); the conversion below
        # is that same rotation expressed as a Blender (w, x, y, z) quaternion,
        # with the Y negations folded in for the coordinate flip.
        if control_point_count:
            control_points_base  = base_ubulk + control_points_offset
            control_point_u32  = np.frombuffer(combined, dtype='<u4',
                                      offset=control_points_base,
                                      count=control_point_count * 8).reshape(-1, 8)
            control_point_f32  = np.frombuffer(combined, dtype='<f4',
                                      offset=control_points_base,
                                      count=control_point_count * 8).reshape(-1, 8)
            control_point_cluster = (control_point_u32[:, 0] & 0x7FFFFFFF).tolist()

            # World-space positions with bbox-center offset and Y flip.
            pos_arr      = control_point_f32[:, 1:4].astype(np.float32, copy=True)
            pos_arr[:, 0] += bbox_cx
            pos_arr[:, 1] += bbox_cy
            pos_arr[:, 2] += bbox_cz
            pos_arr[:, 1] = -pos_arr[:, 1]

            # Quaternion conversion: (qx,qy,qz,qw) -> Quaternion(-qw, qx, -qy, qz)
            quat_in  = control_point_f32[:, 4:8]
            quat_arr = np.empty((control_point_count, 4), dtype=np.float32)
            quat_arr[:, 0] = -quat_in[:, 3]   # w = -qw
            quat_arr[:, 1] =  quat_in[:, 0]   # x =  qx
            quat_arr[:, 2] = -quat_in[:, 1]   # y = -qy
            quat_arr[:, 3] =  quat_in[:, 2]   # z =  qz

            inst_positions = [Vector(p) for p in pos_arr.tolist()]
            inst_quats     = [Quaternion(q) for q in quat_arr.tolist()]
        else:
            control_point_cluster, inst_positions, inst_quats = [], [], []

        # Cluster records - 144 bytes each. The record size is not a guess; the
        # header's cluster byte size divided by the count came to 144 in every
        # group checked. This importer needs only the first 8 bytes, but the rest
        # is largely mapped, and a writer would have to fill it in:
        #
        #   [0:8]     int32 x2  inclusive mesh-type range this cluster draws
        #   [8:12]    int32     8-10 distinct values per file, 17..132; unknown
        #   [12:16]   int32     always 7
        #   [52:56]   int32     varies widely, 24..25693; unknown
        #   [92:96]   int32     repeats the first mesh-type index
        #   [96:108]  float x3  bounds centre, exactly (min + max) / 2
        #   [108:112] float     bounding sphere radius, between the longest half
        #                       axis (~1.1x) and the box half diagonal (~0.85x)
        #   [112:124] float x3  bounds min
        #   [124:128] float     always 1.0, so 112..128 reads as a vec4
        #   [128:140] float x3  bounds max
        #   everything else is zero.
        #
        # The bounds are in the group's local space, not instance world space.
        # Centre == (min + max) / 2 held for all 2666 records checked in both
        # builds, as did max >= min.
        if primitive_info_count:
            cl_arr = np.frombuffer(combined, dtype='<i4',
                                   offset=base_ubulk + primitive_infos_offset,
                                   count=primitive_info_count * 36).reshape(-1, 36)
            # Float view of the same bytes, for the bounding-sphere columns.
            cl_arr_f = np.frombuffer(combined, dtype='<f4',
                                     offset=base_ubulk + primitive_infos_offset,
                                     count=primitive_info_count * 36).reshape(-1, 36)
            cl_mt_start = cl_arr[:, 0].tolist()
            cl_mt_end   = cl_arr[:, 1].tolist()
        else:
            cl_arr = np.empty((0, 36), dtype=np.int32)
            cl_arr_f = np.empty((0, 36), dtype=np.float32)
            cl_mt_start, cl_mt_end = [], []

        # Vertex positions - single np.frombuffer; Y flip vectorized.
        positions_arr = np.frombuffer(combined, dtype='<f4',
                                      offset=base_ubulk + positions_offset,
                                      count=vert_cnt * 3).reshape(-1, 3).astype(np.float32, copy=True)
        # The sway bake works in the file's own space, so keep a pre-flip copy.
        raw_positions_arr = positions_arr.copy() if import_sway else None
        positions_arr[:, 1] = -positions_arr[:, 1]

        deform = None
        if import_sway:
            try:
                deform = read_deform_context(combined, base_ubulk, deform_desc,
                                             cl_arr, vert_cnt)
            except Exception as ex:
                print(f"  Group {group_idx}: sway data unreadable, skipping ({ex})")
            if deform:
                frames = max(c['frames'] for c in deform['clusters'] if c)
                moving = int((deform['vert_deform_index'] >= 0).sum())
                print(f"  Group {group_idx}: sway on {moving}/{vert_cnt} vertices, "
                      f"{frames} frames")

        def cluster_for_mesh_type(mt: int):
            for ci, (s, e) in enumerate(zip(cl_mt_start, cl_mt_end)):
                if s <= mt <= e:
                    return ci
            return None

        # Vertex normals - 8 bytes per vertex before the 2026 update, packed into
        # 4 afterwards; see decode_tangent_frame.
        normal_stride = tangent_frame_stride(tangents_bytes, vert_cnt,
                                             property_bytes)
        nxyz = decode_tangent_frame(combined, base_ubulk + tangents_offset,
                                    vert_cnt, normal_stride)
        nxyz[:, 1] = -nxyz[:, 1]
        # Normalize (matches Vector.normalized(); leaves zero vectors at zero)
        lens = np.sqrt((nxyz * nxyz).sum(axis=1, keepdims=True))
        np.maximum(lens, 1e-30, out=lens)  # avoid div-by-zero; tiny values left effectively zero
        normals_arr = nxyz / lens

        # Texcoords - the shader binds these as Buffer<half2>, so one channel is
        # 4 bytes and two are 8. V is flipped for Blender. As with the tangent
        # frame the header states the buffer's byte size, so the channel count
        # comes from that rather than from the gap to the next stream.
        # Vectorized: NumPy's native float16 -> float32 cast (one C call) replaces the per-vertex
        # struct.pack/unpack pair in BinReader.half_to_float (the dominant per-vertex cost).
        uv_stride = (texcoords_bytes // vert_cnt) if vert_cnt else 4
        if uv_stride not in (4, 8):
            uv_stride = (tangents_offset - texcoords_offset) // vert_cnt if vert_cnt else 4
        num_uv_ch = uv_stride // 4
        if vert_cnt and num_uv_ch:
            half_count = vert_cnt * num_uv_ch * 2
            uvs_arr = np.frombuffer(combined, dtype='<f2',
                                    offset=base_ubulk + texcoords_offset,
                                    count=half_count).astype(np.float32).reshape(vert_cnt, num_uv_ch * 2)
            # Flip V on every odd column (1, 3, ...)
            uvs_arr[:, 1::2] = 1.0 - uvs_arr[:, 1::2]
        else:
            uvs_arr = np.empty((vert_cnt, num_uv_ch * 2), dtype=np.float32)
        has_uv2 = num_uv_ch >= 2

        # FMeshlet: 16 bytes = { VertexCount, VertexOffset, PrimitiveCount,
        # PrimitiveOffset }. Column 0 goes unused here because the submesh record
        # already bounds the vertex range, but it is the meshlet's own vertex
        # count - measured at 3..128 (mean 59) against 1..64 primitives, the
        # standard mesh-shader caps.
        if meshlet_count:
            meshlet_arr = np.frombuffer(combined, dtype='<i4',
                                   offset=base_ubulk + meshlets_offset,
                                   count=meshlet_count * 4).reshape(-1, 4)
            meshlet_vertex_offset    = meshlet_arr[:, 1].tolist()
            meshlet_primitive_count    = meshlet_arr[:, 2].tolist()
            meshlet_primitive_offset = meshlet_arr[:, 3].tolist()
        else:
            meshlet_vertex_offset, meshlet_primitive_count, meshlet_primitive_offset = [], [], []

        unique_vertex_indices    = np.frombuffer(combined, dtype='<i4',
                                               count=unique_vertex_index_count,
                                               offset=base_ubulk + unique_vertex_indices_offset)
        primitive_indices = np.frombuffer(combined, dtype='<i4',
                                               count=primitive_index_count,
                                               offset=base_ubulk + primitive_indices_offset)

        def get_triangles(sub_idx):
            """Return an (N, 3) int32 numpy array of remapped triangle indices."""
            vs = vert_start[sub_idx]
            parts = []
            for grp in range(sub_meshlet_first[sub_idx], sub_meshlet_first[sub_idx] + sub_meshlet_count[sub_idx]):
                if grp >= meshlet_count:
                    break
                base  = meshlet_vertex_offset[grp]
                doff  = meshlet_primitive_offset[grp]
                ntri  = meshlet_primitive_count[grp]
                packed = primitive_indices[doff:doff + ntri]
                i0 = unique_vertex_indices[base + (packed & 0x3FF)]         - vs
                i1 = unique_vertex_indices[base + ((packed >> 10) & 0x3FF)] - vs
                i2 = unique_vertex_indices[base + ((packed >> 20) & 0x3FF)] - vs
                parts.append(np.stack([i0, i1, i2], axis=1))
            if not parts:
                return np.empty((0, 3), dtype=np.int32)
            return np.concatenate(parts, axis=0)

        # Gather instances per mesh-type. Each tuple also carries the control
        # point's own index and cluster index: one control point can place
        # several mesh types (every type in its cluster's range) at the same
        # transform, and nothing else records that they came from the same
        # placement - see mec_control_point_index below.
        mesh_instances: dict = {}
        for inst_idx in range(control_point_count):
            cid = control_point_cluster[inst_idx]
            if cid >= primitive_info_count:
                continue
            ipos = inst_positions[inst_idx]
            iquat = inst_quats[inst_idx]
            for mt in range(cl_mt_start[cid], cl_mt_end[cid] + 1):
                selected_submeshes = selected_submeshes_for_mt(mt)
                if not selected_submeshes:
                    continue
                mesh_instances.setdefault(mt, []).append((ipos, iquat, inst_idx, cid))

        # Collections
        inst_coll = bpy.data.collections.new(f"{base_name}_Group{group_idx}_Instances")
        root_coll.children.link(inst_coll)

        # Group-header fields this importer doesn't otherwise use, stashed as
        # custom properties so a future exporter can recover them without
        # guessing - see "Round-trip fields" in the module docstring.
        inst_coll['mec_hdr_a']             = hdr_a
        inst_coll['mec_hdr_c']             = hdr_c
        inst_coll['mec_ubulk_offset']      = ubulk_data_offset
        inst_coll['mec_guid']              = hdr_b_bytes[4:20].hex().upper()
        inst_coll['mec_hdr_b_byte20']      = hdr_b_bytes[20]
        inst_coll['mec_hdr_b_version_tag'] = hdr_b_bytes[28:32].hex().upper()
        inst_coll['mec_bbox_min']          = [f0, f1, f2]
        inst_coll['mec_bbox_max']          = [f3, f4, f5]
        inst_coll['mec_seven']             = seven
        inst_coll['mec_n_tex_primary']     = n_texture_refs_primary
        inst_coll['mec_n_tex_secondary']   = n_texture_refs_secondary
        inst_coll['mec_thirteen']          = thirteen
        inst_coll['mec_deform_desc']       = deform_desc
        inst_coll['mec_blk40']             = blk40
        inst_coll['mec_thirteen2']         = thirteen2
        inst_coll['mec_trailing20']        = trailing20

        orig_coll = None
        if create_originals:
            orig_coll = bpy.data.collections.new(f"{base_name}_Group{group_idx}_Originals")
            root_coll.children.link(orig_coll)

        group_material_map:    dict = {}
        group_material_hashes: dict = {}
        next_mat_id = 1

        def mats_for_mt(mt):
            """Resolve one mesh type's texture slots, keeping them positional.

            FMassiveEnvironmentMaterialInfo's 14 int32s are fixed-meaning slots,
            not an unordered set, so the returned list is index-aligned with
            them and an unusable slot yields None rather than being dropped.
            Collapsing duplicates (two slots pointing at the same constant
            texture is common - slots 1 and 6 are both 000000FF_BC4 in most
            mesh types) would shift every later slot onto the wrong socket, and
            would also make the list length useless as a layout key.
            """
            if mt >= len(mt_mat_indices):
                return []
            result = []
            for idx in mt_mat_indices[mt]:
                h = None
                if 0 <= idx < len(texture_refs):
                    ref = texture_refs[idx]
                    if ref < 0:
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
                                if h not in _unresolved_texture_hashes:
                                    _unresolved_texture_hashes.add(h)
                                    print(f"    Unknown texture hash: {h}")
                    else:
                        # A non-negative ref is a local export index, not an
                        # import - there is no asset path to resolve, but the
                        # slot is populated, not empty. Keep that distinguishable
                        # from a genuinely unset slot rather than collapsing both
                        # to None.
                        h = f"EXPORT#{ref}"
                result.append(h)
            return result

        for mt, inst_list in mesh_instances.items():
            mat_hashes = mats_for_mt(mt)
            # Ordered key: slot position is part of a material's identity now
            # that the list is positional, so two mesh types sharing the same
            # textures in different slots are correctly kept apart.
            hash_key   = tuple(mat_hashes)
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

                # Submesh/cluster/MeshInfo fields this importer doesn't use for
                # geometry, stashed for the same round-trip reason as the
                # group-level properties above.
                mesh['mec_mesh_type']       = mt
                mesh['mec_submesh_index']   = sub_s
                mesh['mec_lod_index']       = lod_index
                mesh['mec_total_lod_count'] = lod_counts[mt] if mt < len(lod_counts) else 0
                mesh['mec_submesh_record']  = sub_arr[sub_s].tolist()

                ci = cluster_for_mesh_type(mt)
                if ci is not None:
                    mesh['mec_cluster_index']          = ci
                    mesh['mec_cluster_unknown_8_12']   = int(cl_arr[ci][2])
                    mesh['mec_cluster_flags']          = int(cl_arr[ci][3])
                    mesh['mec_cluster_second_modulus'] = int(cl_arr[ci][13])
                    mesh['mec_cluster_bounds_radius']  = float(cl_arr_f[ci][27])
                    mesh['mec_cluster_bounds_min']     = cl_arr_f[ci][28:31].tolist()
                    mesh['mec_cluster_bounds_max']     = cl_arr_f[ci][32:35].tolist()

                if mt < len(meshinfo_ints_arr):
                    mesh['mec_meshinfo_ints']           = meshinfo_ints_arr[mt, :8].tolist()
                    mesh['mec_meshinfo_bounds_center']  = meshinfo_floats_arr[mt, 8:11].tolist()
                    mesh['mec_meshinfo_bounds_radius']  = float(meshinfo_floats_arr[mt, 11])
                    mesh['mec_meshinfo_lod_thresholds'] = meshinfo_floats_arr[mt, 12:28].tolist()

                # Per-texture-slot world-space streaming size, aligned by index
                # with the material's "UE import NN" properties. This importer
                # only needs the index half of MaterialInfo; the size half feeds
                # the game's own mip-streaming pass and has no rendering effect
                # here, but costs nothing to keep.
                if mt < len(material_info_sizes):
                    for slot_i, (hash_val, size_val) in enumerate(
                            zip(mat_hashes, material_info_sizes[mt][:len(mat_hashes)])):
                        if hash_val is None:
                            continue
                        mesh[f'mec_tex_size_{slot_i:02d}'] = float(size_val)

                # Per-mesh slices (numpy views/copies)
                verts_slice = positions_arr[vs:vs + vc] * scale_factor   # (vc, 3) float32
                norms_slice = normals_arr[vs:vs + vc]                    # (vc, 3) float32
                uvs_slice   = uvs_arr[vs:vs + vc]                        # (vc, num_uv_ch*2) float32

                faces_arr = get_triangles(sub_s)                         # (N, 3) int32
                if offset_opposite_faces:
                    offset_count = offset_opposite_face_geometry(
                        verts_slice,
                        norms_slice,
                        faces_arr,
                        offset=scaled_opposite_face_offset(scale_factor),
                    )
                    if offset_count:
                        print(f"    {mesh_name}: offset {offset_count} opposite-face vertices")
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
                        # Numbered by MaterialInfo slot (0-based) now that the
                        # list is positional; empty slots are simply omitted,
                        # since Blender custom properties cannot hold None.
                        for hash_idx, hash_val in enumerate(hashes):
                            if hash_val is None:
                                continue
                            mat[f"UE import {hash_idx:02d}"] = hash_val
                        setup_material_nodes(mat, hashes, tex_root, tex_ext,
                                             tex_index=tex_index)
                    mesh.materials.append(mat)
                    npoly = len(mesh.polygons)
                    if npoly:
                        mesh.polygons.foreach_set("material_index", np.zeros(npoly, dtype=np.int32))

                # Wind sway as shape keys, one per animation frame. Shape keys
                # are added through an object, but live on the mesh, so a single
                # temporary object serves however many instances share it.
                sway_cluster = cluster_for_mesh_type(mt) if deform else None
                if sway_cluster is not None:
                    cl_info = deform['clusters'][sway_cluster]
                    if cl_info is not None:
                        key_obj = bpy.data.objects.new(f"{mesh_name}_SwayTmp", mesh)
                        try:
                            key_obj.shape_key_add(name='Basis', from_mix=False)
                            mesh.shape_keys.use_relative = False
                            added = 0
                            for frame in range(cl_info['frames']):
                                slice_ = deform_frame_positions(
                                    deform, sway_cluster, frame, raw_positions_arr,
                                    vert_start=vs, vert_count=vc)
                                if slice_ is None:
                                    break
                                slice_[:, 1] = -slice_[:, 1]
                                slice_ *= scale_factor
                                kb = key_obj.shape_key_add(name=f"Sway_{frame:03d}",
                                                           from_mix=False)
                                kb.data.foreach_set(
                                    "co", np.ascontiguousarray(slice_, dtype=np.float32).ravel())
                                added += 1
                            if added:
                                key = mesh.shape_keys
                                # ShapeKey.frame is read-only via the API;
                                # Blender auto-spaces absolute keys evenly (10
                                # apart), so read the assigned positions and
                                # drive eval_time between them.
                                first_frame = key.key_blocks[1].frame
                                last_frame = key.key_blocks[added].frame
                                key.eval_time = first_frame
                                if added > 1:
                                    # The captured sway loop is authored at
                                    # DEFORM_SOURCE_FPS (~59.94, confirmed from
                                    # shader disassembly - see the constant's
                                    # definition above), further scaled by the
                                    # owning cluster's cluster[124] field -
                                    # confirmed (same disassembly trace) to
                                    # multiply directly into the frame-index
                                    # time term, i.e. a per-primitive
                                    # animation-speed multiplier, not an
                                    # amplitude scale. Key eval_time to ramp
                                    # linearly across the pose range, timed so
                                    # playback at the scene's fps takes the
                                    # same real-world duration as at that
                                    # effective source rate, then let a Cycles
                                    # modifier repeat the ramp indefinitely.
                                    speed_scale = 1.0
                                    if sway_cluster < len(cl_arr_f):
                                        raw_scale = float(cl_arr_f[sway_cluster][31])
                                        if raw_scale > 0:
                                            speed_scale = raw_scale
                                    source_fps = DEFORM_SOURCE_FPS * speed_scale

                                    scene_fps = source_fps
                                    scene = getattr(context, "scene", None)
                                    render = getattr(scene, "render", None) if scene else None
                                    if render is not None and getattr(render, "fps", 0):
                                        scene_fps = float(render.fps)
                                    end_scene_frame = max(
                                        1, round((added - 1) * scene_fps / source_fps))

                                    key.keyframe_insert(data_path="eval_time", frame=0)
                                    key.eval_time = last_frame
                                    key.keyframe_insert(data_path="eval_time",
                                                        frame=end_scene_frame)

                                    fcurve = find_eval_time_fcurve(key)
                                    if fcurve is not None:
                                        for kp in fcurve.keyframe_points:
                                            kp.interpolation = 'LINEAR'
                                        fcurve.modifiers.new('CYCLES')
                        finally:
                            bpy.data.objects.remove(key_obj)

                if orig_coll:
                    orig_obj = bpy.data.objects.new(f"{mesh_name}_Original", mesh)
                    orig_coll.objects.link(orig_obj)
                    orig_obj.display_type = 'WIRE'
                    orig_obj.hide_set(True)

                for local_idx, (ipos, quat, orig_cp_idx, cid) in enumerate(inst_list):
                    inst_obj = bpy.data.objects.new(f"{mesh_name}_Inst{local_idx}", mesh)
                    inst_obj.location            = ipos * scale_factor
                    inst_obj.rotation_mode       = 'QUATERNION'
                    inst_obj.rotation_quaternion = quat
                    # Ties sibling instance objects (other mesh types placed by
                    # the same control point) back together - see the note by
                    # mesh_instances above.
                    inst_obj['mec_control_point_index'] = orig_cp_idx
                    inst_obj['mec_cluster_index']        = cid
                    inst_coll.objects.link(inst_obj)

        print(f"  Group {group_idx}: {sum(len(v) for v in mesh_instances.values())} instances "
              f"across {len(mesh_instances)} mesh types.")

    print("\nImport finished.")
