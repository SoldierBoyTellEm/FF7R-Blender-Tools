# Rebirth `AnimSequence` format notes

Why `object.ff7r_rebirth_apply_animation_game_packages` used to fail with

```
[FF7R ERROR] Animation import failed: Specified argument was out of the range of
valid values. (Parameter 'Unsupported compressed data type ')
```

and what the bytes actually contain.

## Root cause

The message comes from CUE4Parse-Conversion, not from this add-on:

```csharp
// CUE4Parse-Conversion/Legacy/AnimConverter.cs:252
throw new ArgumentOutOfRangeException(
    "Unsupported compressed data type " + animSequence.CompressedDataStructure?.GetType().Name);
```

The empty type name after the trailing space is the tell: `CompressedDataStructure`
is **null**, so `skeleton.ConvertAnims(source)` in `ExportAnimationPackage`
(`bridge/Program.cs`) has nothing to convert.

It is null because `UAnimSequence.Deserialize` in CUE4Parse throws part-way
through and the loader swallows it:

```
[Error] Could not read "AnimSequence" correctly ::
        Read size is bigger than remaining archive length.
```

CUE4Parse reads the tagged properties correctly, then reads `FStripDataFlags`
where Rebirth has something else, decides editor data is *not* stripped, and
tries to read `RawAnimationData` with a garbage element count.

CUE4Parse has **no** Rebirth-specific `UAnimSequence` handling — the only
`GAME_FinalFantasy7Rebirth` branches are in mesh/Niagara/build-data readers.

## What is actually in the export

Verified across ten `PC0000_00` clips (cutscene `Motion/` and `Facial/`).
Everything is inline in the `.uasset`; there is no `.uexp`/`.ubulk` sibling.

Layout of an `AnimSequence` export:

| region | contents |
| --- | --- |
| tagged properties | `NumFrames`, `SequenceLength`, `TrackToSkeletonMapTable`, `BoneCompressionSettings`, `CurveCompressionSettings`, `Skeleton`, … — read correctly today |
| `FGuid` | `SkeletonGuid`, read correctly today |
| descriptor | ~48 bytes, Rebirth-specific (see below) |
| padding | to a 16-byte boundary; contents are **uninitialised cooker memory**, not zeros |
| ACL buffer | an ACL 1.x `CompressedClip`, `size` bytes |
| curve block | `CompressedCurveNames` UIDs + curve codec data, ending in `int32 CompressedNumberOfFrames` |

### The descriptor

```
int32   totalSize?      // ~ACL size + 130..380, exact meaning unknown
uint16  == 0
uint16  == 1
uint16  alignment       // always 16
uint16  padCount        // that many bytes of padding follow, to a 16-byte boundary
--- 16-byte aligned ---
int32   flags?          // always 0x61
int32   == 0
int32   aclSize         // == the ACL CompressedClip m_size
int32   aclSize         // repeated
int32   ~2*aclSize + 71..91
int32   == 0
int32   X               // 75 / 252 / 262 / 286 / 305 …
int32   X               // repeated
uint64  == 0x06F4945490_2FAF30   // identical in every clip; codec id/hash
--- pad to 16 ---
ACL CompressedClip
```

The two `X` values and `totalSize` are not needed to extract animation.

### The ACL payload

Tag `0xAC10AC10`, version `5` = ACL **1.x** `CompressedClip`
(`AlgorithmType8::UniformlySampled`, `get_algorithm_version() == 5`).
Note this is *not* ACL 2.x `compressed_tracks` (`0xAC11AC11`), which is the only
thing CUE4Parse's `CUE4Parse.ACL` binding understands — and that binding needs
the native `CUE4Parse-Natives` library, which the bridge does not ship.

```
CompressedClip          ClipHeader (immediately after)
  uint32 m_size           uint16 num_bones
  uint32 m_hash           uint16 num_segments
  uint32 m_tag            uint8  rotation_format      == 4  QuatDropW_Variable
  uint16 m_version        uint8  translation_format   == 3  Vector3_Variable
  uint8  m_type           uint8  scale_format         == 3  Vector3_Variable
  uint8  m_padding        uint8  clip_range_reduction == 0x07 (rot|trn|scl)
                          uint8  segment_range_reduction == 0x07
                          uint8  has_scale
                          uint8  default_scale
                          uint8  padding
                          uint32 num_samples
                          float  sample_rate
                          uint16 segment_start_indices_offset
                          uint16 segment_headers_offset
                          uint16 default_tracks_bitset_offset
                          uint16 constant_tracks_bitset_offset
                          uint16 constant_track_data_offset
                          uint16 clip_range_data_offset
```

All ten samples used the same formats and full range reduction — the UE4 ACL
plugin's default settings — with 2–3 segments and 30 fps.

`ClipHeader.num_bones` matched the `TrackToSkeletonMapTable` property length in
every sample, so that property (already parsed correctly) is the ACL track index
→ skeleton bone index map. `CompressedTrackToSkeletonMapTable` stays empty and
must not be used.

## How the bridge reads it

`bridge/RebirthAnimSequence.cs` registers `URebirthAnimSequence` in CUE4Parse's
`ObjectTypeRegistry` under `"AnimSequence"`, replacing `UAnimSequence` for the whole
process. It derives from `UAnimSequenceBase`, not `UAnimSequence`, so none of
CUE4Parse's compressed-data deserialization runs: it keeps the tagged properties and
stashes the rest of the export as a byte payload.

`bridge/AclDecoder.cs` then finds the clip in that payload by scanning for the
`0xAC10AC10` tag, stepping back 8 bytes to the `{size, hash}` header and checking the
FNV-1a 32 hash over everything after it. Scanning beats trusting the descriptor: the
alignment padding in front of the clip is uninitialised cooker memory (samples contained
UTF-16 fragments like `"cles"` and `"nsta"`), and tag + size + hash is self-validating.

`bridge/AclDecompressor.cs` decompresses it — a scalar port of acl v1.3.5's
`uniformly_sampled` decoder, restricted to the variable bit rate formats Rebirth uses and
throwing on anything else. It samples one key frame at a time rather than interpolating
two, because the bridge exports every frame; that collapses acl's per-key-frame sampling
state to a single set of offsets.

Track index → bone index comes from the `TrackToSkeletonMapTable` property, read through
`FStructFallback`. CUE4Parse's `FTrackToSkeletonMap` is a plain struct with no property-tag
reader, so `GetOrDefault<FTrackToSkeletonMap[]>` silently returns zeroes and every track
lands on bone 0.

## Getting the keys onto a package rig

Decoding is only half of it. `skeleton/importer.py` does not use the UE-converted bind
matrix `B` as a bone's rest matrix: it reframes each edit bone for display, aiming
Blender's +Y down Unreal's local +X, aligning +Z to Unreal's +Z, then adding a 90 degree
roll. That is a single constant change of basis, the same for every bone:

```
bone.matrix_local == B @ D          D = [[ 0, 1, 0],
                                         [ 0, 0,-1],
                                         [-1, 0, 0]]
```

`D` is `animations.DISPLAY_CORRECTION`, derived by replaying that framing on synthetic
bind matrices (identical across bones to within 2e-6). It is a signed permutation, so a
UE scale stays diagonal through the conjugation below rather than smearing into shear.

Writing `L` for a parent-relative UE transform carried into Blender axes (negate Y on the
translation, take the quaternion `(X,Y,Z,W)` to `(W,-X,Y,-Z)`, centimetres to metres), a
bone's parent-relative rest is `D^-1 @ L_bind @ D` and its parent-relative pose must be
`D^-1 @ L_anim @ D`, so

```
matrix_basis = D^-1 @ L_bind^-1 @ L_anim @ D
```

The same expression covers root bones: both rest and pose pick up the importer's
`Y_FORWARD_ROTATION` and it cancels between them.

Conjugating by `D` is the easy step to miss. Without it the UE delta gets applied in the
display frame instead of being carried into it, and every pose comes out rotated by
roughly 100 degrees with metre-scale position error — while still looking superficially
reasonable, because it is exact at the rest pose and the motion magnitudes stay plausible.

### The root track is not a bone

The first bone (`Trans` on the player skeletons) is the exception. Its ref pose is
identity, but every clip sampled carries the *same* rotation on it -- 120 degrees about
`(-1, 1, -1)/sqrt(3)`, byte-identical across unrelated cutscenes -- and a translation
that is the actor's raw world placement, up to hundreds of thousands of centimetres.
It is a component-to-world placement, not a bone-parented transform.

So it does not carry the 90 degree display roll that the importer adds to each *bone's*
frame, and its display correction is `D` without that roll:

```
ROOT_DISPLAY_CORRECTION = D @ Ry(-90 degrees)     # == Rz(-90 degrees)
matrix_basis = D^-1 @ L_bind^-1 @ L_anim @ ROOT_DISPLAY_CORRECTION
```

Applying the full `D` here lays the whole character on its side: hip-to-head points down
world +X instead of +Z. That reads as a plausible animation of a prone character, and it
survives every local check -- parent-relative poses stay exact, and the root still travels
the right distance each frame -- which is why the animation test now also asserts that the
character stands up.

### The forward axis cancels

`face_y_forward` is baked into each *root* bone's armature-space matrix by the skeleton
importer, so it appears in both the rest matrix and the intended pose and divides out of
`matrix_basis` entirely. The animation importer therefore needs no forward-axis term: the
-Y forward result is measurably the +X forward result rotated -90 degrees about Z, to
float precision, on the same keys.

The choice is still recorded on the armature as `ff7r_face_forward` (read it back with
`skeleton.importer.armature_face_forward`), because it cannot be recovered from the rest
matrices afterwards and validating a *root* bone's rest pose needs it -- a root is measured
against the armature origin rather than a parent, so it carries the forward rotation.
Armatures imported before this was recorded report `UNREAL`, the operator default.

The operator warns when a bone's rest pose disagrees with the clip's bind pose by more
than `REST_CONVENTION_TOLERANCE`, which catches an armature that was not built by the
package importer and so does not use this frame.

## Verifying a decode

No second decoder exists for this format, so `bridge/blender_animation_test.py` asserts
structurally instead:

- rotations must stay unit length (measured 1.2e-7 worst case after the add-on's bone-roll
  conversion), and decoded `xyz` must stay inside the unit ball — a wrong range reduction
  leaves it roughly half the time, and 0 of ~18,000 samples across five clips did;
- acl range-reduces per segment, so a mistake there shows up as a spike at a segment seam
  and nowhere else. Across five clips the seam frames measured 0.7x-1.9x the clip's own
  median per-frame rotation delta, never a top-ranked jump.

The axis conversion, by contrast, does have a reference: the track data itself. For a bone
whose parent is also keyed, Blender's parent-relative posed matrix must equal
`D^-1 @ L_anim @ D`, which the test checks across five frames on a motion clip and a
facial clip (rotation, translation and scale all match to float noise).

The root needs three separate checks, because each one alone has a blind spot:

- distance travelled per frame, invariant under both `D` and the forward rotation;
- the character stands up (hip-to-head within 25 degrees of +Z) across three unrelated
  clips -- the only check that catches a wrong root *frame*;
- the -Y forward result equals the +X forward result rotated -90 degrees about Z, over
  sampled bones and frames. Tilt is measured about Z and so cannot tell the two
  conventions apart, so this is what actually exercises the forward axis.
