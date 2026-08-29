# FF7R Rebirth Tools

One Blender add-on containing the retained FF7R tools:

- **UMAP importers:** `File > Import > FF7R Rebirth` includes separate Massive Environment entries for loose `.umap`/`.ubulk` files and maps mounted directly from Rebirth's packages.
- **StaticMesh importer:** `File > Import > FF7R Rebirth > Static Mesh (Rebirth Packages)` accepts a mounted `.uasset` virtual path. It supports the update's flattened/meshlet layout and falls back to the earlier conventional layout, including their respective packed-normal formats. It imports geometry, custom normals and tangents, all UV channels, vertex colors, material slots, and package-path metadata. The dialog is prefilled with `End/Content/Environment/Machine/Model/Machine_MagicStore_01A.uasset` as the verified reference asset.
- **KineDriver importer:** select an armature, then use `File > Import > FF7R Rebirth > KineDriver JSON`. It audits the selected KDI file internally and builds the driver layer in one operation.
- **Opposite-face fix and KDI removal:** `Object > Retrilogy tools`.

When building the KineDriver layer, the file browser's operator options include independent mappings for translation and scale. The labels state the exact mapping; `X → X, Y → Z, Z → Y` is the default, routing KDI Y into Blender Z and KDI Z into Blender Y. You can still select direct `X → X, Y → Y, Z → Z` if needed.

Implementation is organized by format: `ff7r_json/` contains the UMAP and cutscene JSON import stack, `kdi/` contains the KineDriver JSON audit and driver generator, `mec/` contains Massive Environment binary `.umap` support, and `bridge/` contains the packaged CUE4Parse runtime used for direct game imports.

Direct package imports use CUE4Parse's package import map for texture-path resolution, so they do not depend on `mec/texture_hashes.csv`. The CSV remains available only for loose exports that have no package metadata.

During batch UMAP imports, decoded package data is released as soon as each map's metadata and actor payload has been handed to Blender. The bridge compacts its large-object heap at that boundary and transparently restarts if more than 2 GiB remains allocated, preventing recursive imports from retaining every previously decoded map.

Package-backed Massive Environment imports can also read the referenced block-compressed textures directly from the mounted game data. DDS payloads retain their native BC/DXT encoding, are packed into Blender image datablocks, and leave no extracted texture library behind.

The package browser supports arbitrary multi-map batches with persistent checkboxes, folder navigation, full-path search, and select/invert/clear controls. Package mounting and the packed-image cache are shared across the complete batch. `AutoGenCollision` paths are omitted because they do not contain usable Massive Environment geometry.

## Installation

See the [repository README](../README.md) for install steps and required preference paths.

Disable the separate FF7R UMAP importer and the old FF7R Actions add-on first. They register several of the same Blender operator identifiers, so running them together is not supported.

## Preferences reference

- **Prop Library** (select existing or custom): directory of `.blend` files with objects or collections marked as assets that have the names of StaticMesh and SkeletalMesh objects from the game. Needed for prop support.
- **Game content root**: the `Content` folder of your FModel exports. `.umap`/`.ubulk` files also go here, next to the JSON files.
- **Recursive import**: recursively import map JSON files referenced by other JSON files.
- **Import referenced ME UMAPs**: loads the `.umap`/`.ubulk` terrain mentioned in a JSON file instead of requiring you to import it separately.
- **Texture content root**: folder with subfolders like `environment` containing your extracted textures.

## KineDriver persistence

Keep this add-on enabled for any `.blend` file containing generated KineDriver drivers. The add-on restores the custom driver functions automatically when such a file is opened.

## Attribution

The UMAP importer payload is bundled from the supplied `ff7r_umap.zip`; its original author attribution is retained in the add-on metadata. The opposite-face operator preserves the behavior of the previous FF7R Actions tool.
 
