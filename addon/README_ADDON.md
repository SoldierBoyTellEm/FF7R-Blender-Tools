# FF7R Rebirth Tools

One Blender add-on containing the retained FF7R tools:

- **UMAP importers:** `File > Import > FF7R Rebirth` includes separate Massive Environment entries for loose `.umap`/`.ubulk` files and maps mounted directly from Rebirth's packages. The package UMAP browser imports StaticMesh actors from the mounted game data by default (sharing one Blender mesh datablock across repeated actors); its checkbox can restore the legacy prop-library path when needed.
- **StaticMesh importer:** `File > Import > FF7R Rebirth > Static Mesh (Rebirth Packages)` accepts a mounted `.uasset` virtual path. It supports the update's flattened/meshlet layout and falls back to the earlier conventional layout, including their respective packed-normal formats. It imports geometry, custom normals and tangents, all UV channels, vertex colors, material slots, and package-path metadata. RMI_Surface material instances are built as their researched variant and their texture parameters are packed automatically; Unreal color parameters are represented by RGB constant nodes instead of image-texture nodes. This currently applies only to direct package StaticMesh imports and package-backed UMAP StaticMesh actors. The dialog is prefilled with `End/Content/Environment/Machine/Model/Machine_MagicStore_01A.uasset` as the verified reference asset.
- **KineDriver importer:** select an armature, then use `File > Import > FF7R Rebirth > KineDriver JSON`. It audits the selected KDI file internally and builds the driver layer in one operation.
- **Animation importer (first pass):** select a package-imported armature, then use `Object > Retrilogy tools > Apply Rebirth Animation`. It records the source Skeleton on newly imported package armatures (and can infer it from a bound package SkeletalMesh in older scenes), searches compatible `AnimSequence` assets by Skeleton, and creates a named Blender Action. Source frame timing is converted to the current scene frame rate; UE local transforms retain the package rig's Blender bone-roll convention.
- **Opposite-face fix and KDI removal:** `Object > Retrilogy tools`.

When building the KineDriver layer, the file browser's operator options include independent mappings for translation and scale. The labels state the exact mapping; `X → X, Y → Z, Z → Y` is the default, routing KDI Y into Blender Z and KDI Z into Blender Y. You can still select direct `X → X, Y → Y, Z → Z` if needed.

Implementation is organized by format: `json/` contains the UMAP and cutscene JSON import stack, `kdi/` contains the KineDriver JSON audit and driver generator, `mec/` contains Massive Environment binary `.umap` support, and `bridge/` contains the packaged CUE4Parse runtime used for direct game imports.

Direct package imports use CUE4Parse's package import map for texture-path resolution, so they do not depend on `mec/texture_hashes.csv`. The CSV remains available only for loose exports that have no package metadata.

During batch UMAP imports, decoded package data is released as soon as each map's metadata and actor payload has been handed to Blender. The bridge compacts its large-object heap at that boundary and transparently restarts if more than 2 GiB remains allocated, preventing recursive imports from retaining every previously decoded map.

Package-backed Massive Environment imports can also read the referenced block-compressed textures directly from the mounted game data. DDS payloads retain their native BC/DXT encoding, are packed into Blender image datablocks, and leave no extracted texture library behind.

The package browser supports arbitrary multi-map batches with persistent checkboxes, folder navigation, full-path search, and select/invert/clear controls. It remembers the last browsed folder until Blender is closed. Package mounting, the StaticMesh cache, and the packed-image cache are shared across the complete batch. `AutoGenCollision` paths are omitted because they do not contain usable Massive Environment geometry.

## Installation

Install `ff7r_rebirth_tools.zip` through Blender's **Edit > Preferences > Add-ons > Install from Disk**, then enable **FF7R Rebirth Tools**.

Disable the separate FF7R UMAP importer and the old FF7R Actions add-on first. They register several of the same Blender operator identifiers, so running them together is not supported.

## KineDriver persistence

Keep this add-on enabled for any `.blend` file containing generated KineDriver drivers. The add-on restores the custom driver functions automatically when such a file is opened.

## Attribution

The UMAP importer payload is bundled from the supplied `ff7r_umap.zip`; its original author attribution is retained in the add-on metadata. The opposite-face operator preserves the behavior of the previous FF7R Actions tool.

## RMI_Surface source and provenance

The package StaticMesh RMI integration in `rmi_surface.py` loads the maintained surface builder and variant trimmer from the sibling research checkout at `../material related/blender/ff7r_rmi_surface.py` and `ff7r_rmi_surface_variant.py`. Their exact variant switch sets come from `../material related/scripts/renderer_ground_truth.json`, generated from the current-build Renderer `MaterialInstance` exports' `StaticSwitchParameters` (documented in `../material related/RMI_SURFACE_VARIANTS.md`). The shader-method and RenderDoc provenance are retained in that project’s `README.md`, `BLENDER_PORT_PLAN.md`, `SHADER_FINDINGS.md`, and `DEFERRED_LIGHTING_HANDOFF.md`.

The bridge adds a mapping-independent MaterialInstance parameter read: CUE4Parse and the active `.usmap` provide the cooked `Parent`, texture, vector/color, and scalar parameter arrays. This lets the add-on assign packed DDS images to the appropriate RMI slots and convert matching Unreal vector/color parameters into Blender RGB constants without maintaining a second hand-authored parameter table.

The bundled `assets/Common_Eye_Player_NG_filtered.png` is a lossless 16-bit override for the exact shared game texture `/Game/Character/Common/Eye/Texture/Common_Eye_Player_NG`. It was derived from `O:/Blender/Rebirth DDS/Character/Common/Eye/Texture/Common_Eye_Player_NG.dds` with `tools/generate_common_eye_player_ng_override.py`. Two 3x3 binomial passes (equivalent to a 5x5 Gaussian) suppress BC5's 4x4 block boundaries before the eye shader amplifies them through cornea parallax; no other texture path uses the override.
 
