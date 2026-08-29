# Final Fantasy VII Retrilogy Tools (Mostly Rebirth, presently)

A Blender add-on for importing FINAL FANTASY VII REMAKE series content — maps,
static meshes, skeletons, and the game's KineDriver (KDI) procedural rigging
system — either from loose FModel JSON exports or directly from your
mounted game packages via a bundled [CUE4Parse](https://github.com/FabianFG/CUE4Parse)-based
bridge.


This repository ships no game content of any kind. Everything it imports
is read live from a copy of the game you already own; see
[NOTICE.md](NOTICE.md) for what's and isn't included.

## What's here

- [`addon/`](addon/) — the installable Blender add-on (Python), including
  the prebuilt bridge binaries in `addon/bridge/` so it works out of the box.
  See [`addon/README_ADDON.md`](addon/README_ADDON.md) for feature details.
- [`bridge/`](bridge/) — the C# source for `FF7RGameAssetBridge`, the helper
  process that reads package data straight out of a mounted game install.
  See [`bridge/README.md`](bridge/README.md) to rebuild it.

## Installing the add-on

1. Zip the `addon/` folder's *contents* (not the folder itself) into
   `ff7r_rebirth_tools.zip`, or point Blender at the `addon/` folder directly.
2. In Blender: **Edit > Preferences > Add-ons > Install from Disk**, select
   the zip, then enable **FF7R Rebirth Tools**.
3. For reading data directly from game install to function, in the add-on preferences fill in your own paths for the following — none are prefilled:
   - **Rebirth Install Folder** — your REBIRTH install, containing `End/Content/Paks`.
   - **Rebirth Mapping File** — a `.usmap` for the game.
   - **Oodle DLL** — an `oo2core_*.dll` you already have from an installed
     Unreal Engine title (FF7 REMAKE INTERGRADE ships one, but Rebirth doesn't). Not bundled here — see [NOTICE.md](NOTICE.md).

Disable any older standalone FF7R UMAP importer / FF7R Actions add-ons first
— they share several operator IDs with this one.

## Building the bridge yourself

Not required — `addon/bridge/` already has working binaries. If you want to
audit or modify the bridge, see [`bridge/README.md`](bridge/README.md).

## License

Original code in this repository is dedicated to the public domain under
[The Unlicense](LICENSE). Bundled third-party components (CUE4Parse and its
own dependencies) keep their own licenses — see [NOTICE.md](NOTICE.md).
