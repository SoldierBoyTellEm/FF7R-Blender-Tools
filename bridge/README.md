# FF7RGameAssetBridge

The C# helper process the add-on shells out to for reading FINAL FANTASY VII
REBIRTH package data directly from a mounted game install via
[CUE4Parse](https://github.com/FabianFG/CUE4Parse) (Apache-2.0 — see
[../NOTICE.md](../NOTICE.md)).

The compiled binaries already sit in `../addon/bridge/` so the add-on works
without building anything. Rebuild from here only if you're changing the
bridge itself or want to verify the shipped binaries yourself.

## Build

Requires the [.NET SDK](https://dotnet.microsoft.com/download) matching the
`TargetFramework` in `RebirthPackageLister.csproj` (currently `net10.0`).

```
dotnet build -c Release
```

This restores CUE4Parse/CUE4Parse-Conversion from NuGet and produces
`bin/Release/net10.0/FF7RGameAssetBridge.exe` plus its dependency DLLs.

## Deploying a rebuilt bridge

Copy the contents of `bin/Release/net10.0/` (the `.exe`, `.dll`, `.pdb`, and
`runtimes/` folder) into `../addon/bridge/`, replacing what's there.

## Usage

The add-on invokes the bridge as a subprocess in `asset-server` mode (one
JSON request per line on stdin, one JSON response per line on stdout). See
`PackageTextureLoader` in `../addon/game_packages.py` for the calling
convention, and the `Usage:` string at the top of `Program.cs` for the
one-shot CLI form.
