# Third-party notices

The code in this repository (`addon/` minus `addon/bridge`, and `bridge/`) is
released into the public domain under [The Unlicense](LICENSE). That dedication
does not apply to the third-party components below, which retain their own
licenses.

## CUE4Parse / CUE4Parse-Conversion

The bridge (`bridge/Program.cs`) depends on [CUE4Parse](https://github.com/FabianFG/CUE4Parse)
and CUE4Parse-Conversion via NuGet (`bridge/RebirthPackageLister.csproj`), and
the compiled `addon/bridge/CUE4Parse.dll` / `addon/bridge/CUE4Parse-Conversion.dll`
shipped with the add-on for convenience are built from that dependency.

CUE4Parse is Copyright (c) FabianFG and contributors, licensed under the
Apache License, Version 2.0. A copy of the license is available at
<http://www.apache.org/licenses/LICENSE-2.0> and is not modified here.

## Other bundled dependencies

`addon/bridge/` also ships the transitive NuGet dependencies CUE4Parse pulls in
at build time (e.g. Newtonsoft.Json, Serilog, SharpGLTF, SkiaSharp, K4os.*,
ZstdSharp, and others visible in that folder). Each retains its own license as
published on NuGet.org / its own repository; none of them are modified by this
project. Rebuilding the bridge from `bridge/` via `dotnet build` or
`dotnet publish` will re-fetch the same set from NuGet.

## Oodle

This project does **not** bundle Oodle's proprietary decompression library.
`addon/bridge/OodleSharp.dll` / `addon/bridge/Oodle.NET.dll` are open-source
P/Invoke wrapper libraries only — at runtime you must point the add-on's
"Oodle DLL" preference at a real `oo2core_*.dll` you already legitimately have
(e.g. one shipped inside your own installed copy of FINAL FANTASY VII REMAKE INTERGRADE). No such
file is included here.

## Game assets

This repository contains no assets, data, or code extracted from FINAL
FANTASY VII REBIRTH or any other Square Enix product. The add-on reads such
data live from a game installation you already own; nothing from the game is
redistributed.
