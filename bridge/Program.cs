using System.Text.Json;
using System.Text.RegularExpressions;
using System.Collections;
using System.Reflection;
using System.Buffers.Binary;
using System.Runtime;
using CUE4Parse.FileProvider;
using CUE4Parse.Compression;
using CUE4Parse.MappingsProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Versions;
using CUE4Parse.UE4.Assets.Exports.Animation;
using CUE4Parse.UE4.Assets.Exports;
using CUE4Parse.UE4.Assets.Exports.SkeletalMesh;
using CUE4Parse.UE4.Assets.Exports.StaticMesh;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse.UE4.Assets.Objects;
using CUE4Parse.UE4.Assets.Readers;
using CUE4Parse.UE4.IO.Objects;
using CUE4Parse.UE4.Objects.Engine;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Readers;
using CUE4Parse.UE4.Objects.UObject;
using CUE4Parse_Conversion.Textures;

if (args.Length < 1)
{
    Console.Error.WriteLine("Usage: RebirthPackageLister <game-directory> [output-json|-] [oodle-dll] [path-filter] [asset-path] [usmap] [dds-output] [raw-output] [summary]");
    return 2;
}

var gameDirectory = Path.GetFullPath(args[0]);
if (!Directory.Exists(gameDirectory))
{
    Console.Error.WriteLine($"Game directory does not exist: {gameDirectory}");
    return 2;
}

static object? ConvertValue(object? value, int depth = 0)
{
    if (value is null || depth > 32)
        return value?.ToString();
    if (value is string || value is bool || value is byte || value is sbyte ||
        value is short || value is ushort || value is int || value is uint ||
        value is long || value is ulong || value is float || value is double ||
        value is decimal)
        return value;
    if (value is Enum)
        return value.ToString();
    if (value.GetType().IsGenericType && value.GetType().GetGenericTypeDefinition() == typeof(Lazy<>))
        return ConvertValue(value.GetType().GetProperty("Value")?.GetValue(value), depth + 1);

    var type = value.GetType();
    if (value is FPackageIndex packageIndex)
    {
        var resolved = packageIndex.ResolvedObject;
        var objectPath = resolved?.GetPathName() ?? packageIndex.Name.ToString();
        if (string.IsNullOrWhiteSpace(objectPath))
            return null;
        return new Dictionary<string, object?>
        {
            // The Blender asset linker keys by UE object/datablock name. Keep the
            // full path separately for diagnostics and future pak-mesh loading.
            ["ObjectName"] = $"Object'{packageIndex.Name}'",
            ["ObjectPath"] = objectPath,
        };
    }
    if (type.Name == "StructProperty")
    {
        var genericValue = type.GetProperty("GenericValue", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(value)
            ?? type.GetProperty("Value", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(value);
        if (genericValue is not null && !ReferenceEquals(genericValue, value))
            return ConvertValue(genericValue, depth + 1);
    }
    // FPropertyTag must be matched by exact type name before the FStructFallback
    // heuristic below: FPropertyTag.ToString() embeds its wrapped value's own
    // description, and for a struct-typed tag (e.g. InPortInfo/OutPortInfo) that
    // description contains the literal text "(FStructFallback, StructProperty)".
    // That previously fooled the ToString-based FStructFallback check into treating
    // the *tag itself* as the struct, silently dropping the tag's name/value.
    if (type.Name == "FPropertyTag")
        return ConvertFPropertyTag(value, type, depth);
    if (type.Name == "FStructFallback" || type.FullName?.Contains("FStructFallback", StringComparison.Ordinal) == true)
    {
        var propertiesMember = type.GetMember("Properties", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault() ?? type.GetMembers(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault(member => member switch
            {
                System.Reflection.PropertyInfo property => property.GetIndexParameters().Length == 0 && typeof(IEnumerable).IsAssignableFrom(property.PropertyType),
                FieldInfo field => typeof(IEnumerable).IsAssignableFrom(field.FieldType),
                _ => false
            });
        var tags = propertiesMember switch
        {
            System.Reflection.PropertyInfo property => property.GetValue(value) as IEnumerable,
            FieldInfo field => field.GetValue(value) as IEnumerable,
            _ => null
        };
        var structValues = new Dictionary<string, object?>();
        if (tags is not null)
        {
            foreach (var tag in tags)
            {
                var converted = ConvertValue(tag, depth + 1) as Dictionary<string, object?>;
                if (converted is not null && converted.TryGetValue("name", out var name) && name is not null)
                    structValues[name.ToString()!] = converted.TryGetValue("value", out var item) ? item : converted;
            }
        }
        // CUE4Parse represents an untagged struct-array element as a fallback
        // containing one synthetic StructType property.  The JSON exporter used
        // by the existing KDI importer writes that inner struct directly.
        if (structValues.Count == 1 && structValues.TryGetValue("StructType", out var innerStruct) &&
            innerStruct is Dictionary<string, object?> innerValues)
            return innerValues;
        return structValues;
    }
    if (type.Name is "ArrayProperty" or "FArrayProperty" or "UScriptArray")
    {
        var itemsMember = type.GetMember("Properties", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault();
        var items = itemsMember switch
        {
            System.Reflection.PropertyInfo property => property.GetValue(value) as IEnumerable,
            FieldInfo field => field.GetValue(value) as IEnumerable,
            _ => null
        };
        if (items is null)
            return new List<object?>();
        var convertedItems = new List<object?>();
        foreach (var item in items)
            convertedItems.Add(ConvertValue(item, depth + 1));
        return convertedItems;
    }
    if (type.Name.EndsWith("Property", StringComparison.Ordinal) &&
        type.Name is not "FPropertyTag" and not "StructProperty")
    {
        var genericValue = type.GetProperty("GenericValue", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(value)
            ?? type.GetProperty("Value", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(value);
        if (genericValue is not null && !ReferenceEquals(genericValue, value))
            return ConvertValue(genericValue, depth + 1);
    }
    if (value is IEnumerable sequence && value is not string &&
        !value.ToString()!.Contains("FStructFallback", StringComparison.Ordinal))
    {
        var items = new List<object?>();
        foreach (var item in sequence)
            items.Add(ConvertValue(item, depth + 1));
        return items;
    }

    if (type.Name == "FName")
        return value.ToString();

    // Unreal object references and unsupported reader types are still useful as text.
    var fields = type.GetFields(BindingFlags.Instance | BindingFlags.Public)
        .Where(field => !field.IsStatic)
        .ToArray();
    if (fields.Length == 0)
        return value.ToString();
    var result = new Dictionary<string, object?>();
    foreach (var field in fields)
        result[field.Name] = ConvertValue(field.GetValue(value), depth + 1);
    return result;
}

static object? ReadPublicMember(object? instance, string memberName)
{
    if (instance is null)
        return null;
    var type = instance.GetType();
    return type.GetProperty(memberName, BindingFlags.Instance | BindingFlags.Public)?.GetValue(instance)
        ?? type.GetField(memberName, BindingFlags.Instance | BindingFlags.Public)?.GetValue(instance);
}

static object CollectBatchMemory(long restartThresholdBytes)
{
    // UMAP deserialization produces many large temporary arrays. Compact the
    // large-object heap after a small batch has been handed to Blender. If
    // CUE4Parse still retains package/export state, the caller can recycle this
    // process while preserving everything already created in Blender.
    var workingSetBeforeBytes = Environment.WorkingSet;
    var managedBeforeBytes = GC.GetTotalMemory(forceFullCollection: false);
    GCSettings.LargeObjectHeapCompactionMode = GCLargeObjectHeapCompactionMode.CompactOnce;
    GC.Collect(GC.MaxGeneration, GCCollectionMode.Aggressive, blocking: true, compacting: true);
    GC.WaitForPendingFinalizers();
    GC.Collect(GC.MaxGeneration, GCCollectionMode.Aggressive, blocking: true, compacting: true);
    var workingSetBytes = Environment.WorkingSet;
    var managedBytes = GC.GetTotalMemory(forceFullCollection: false);
    return new
    {
        workingSetBeforeBytes,
        managedBeforeBytes,
        workingSetBytes,
        managedBytes,
        restartRecommended = restartThresholdBytes > 0 &&
            Math.Max(workingSetBytes, managedBytes) > restartThresholdBytes,
    };
}

static List<object?> DescribeMappingProperties(object mapping)
{
    if (ReadPublicMember(mapping, "Properties") is not IEnumerable properties)
        return [];
    var results = new List<object?>();
    foreach (var entry in properties)
    {
        var property = ReadPublicMember(entry, "Value") ?? entry;
        var mappingType = ReadPublicMember(property, "MappingType");
        results.Add(new Dictionary<string, object?>
        {
            ["entry"] = entry?.ToString(),
            ["name"] = ReadPublicMember(property, "Name")?.ToString(),
            ["type"] = mappingType?.ToString(),
            ["mappingType"] = mappingType is null ? null : ConvertValue(mappingType, 1)
        });
    }
    return results;
}

static Dictionary<string, object?> ConvertFPropertyTag(object value, Type type, int depth)
{
    var allInstanceFields = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    var tag = type.GetField("Tag", allInstanceFields)?.GetValue(value)
        ?? type.GetField("TagData", allInstanceFields)?.GetValue(value);
    var generic = tag?.GetType().GetProperty("GenericValue", allInstanceFields)?.GetValue(tag)
        ?? tag?.GetType().GetField("GenericValue", allInstanceFields)?.GetValue(tag)
        ?? tag;
    return new Dictionary<string, object?>
    {
        ["name"] = type.GetField("Name")?.GetValue(value)?.ToString(),
        ["propertyType"] = type.GetField("PropertyType")?.GetValue(value)?.ToString(),
        ["value"] = ConvertValue(generic, depth + 1)
    };
}

static object? NormalizeKdiValue(object? value)
{
    if (value is Dictionary<string, object?> dictionary)
    {
        var normalized = dictionary.ToDictionary(
            entry => entry.Key,
            entry => NormalizeKdiValue(entry.Value));
        if (normalized.Count == 1 && normalized.TryGetValue("StructType", out var inner))
            return inner;
        return normalized;
    }
    if (value is List<object?> list)
        return list.Select(NormalizeKdiValue).ToList();
    return value;
}

static byte[] WrapBlockCompressedAsDds(
    int width,
    int height,
    byte[] blockData,
    EPixelFormat format,
    bool srgb)
{
    const uint ddsMagic = 0x20534444; // "DDS "
    const uint ddsdCapsHeightWidthPixelFormatLinearSize = 0x00081007;
    const uint ddpfFourCc = 0x00000004;
    const uint ddsCapsTexture = 0x00001000;
    uint fourCc;
    uint? dxgiFormat = null;
    switch (format)
    {
        case EPixelFormat.PF_DXT1: fourCc = 0x31545844; break; // DXT1
        case EPixelFormat.PF_DXT3: fourCc = 0x33545844; break; // DXT3
        case EPixelFormat.PF_DXT5: fourCc = 0x35545844; break; // DXT5
        case EPixelFormat.PF_BC4: fourCc = 0x31495441; break;  // ATI1
        case EPixelFormat.PF_BC5: fourCc = 0x32495441; break;  // ATI2
        case EPixelFormat.PF_BC6H:
            fourCc = 0x30315844; // DX10
            dxgiFormat = 95;     // DXGI_FORMAT_BC6H_UF16
            break;
        case EPixelFormat.PF_BC7:
            fourCc = 0x30315844; // DX10
            dxgiFormat = srgb ? 99u : 98u; // BC7_UNORM_SRGB / BC7_UNORM
            break;
        default:
            throw new NotSupportedException($"DDS block wrapper does not support {format}.");
    }

    var headerLength = dxgiFormat.HasValue ? 148 : 128;
    var output = new byte[headerLength + blockData.Length];
    var header = output.AsSpan(0, 128);
    BinaryPrimitives.WriteUInt32LittleEndian(header[0..4], ddsMagic);
    BinaryPrimitives.WriteUInt32LittleEndian(header[4..8], 124);
    BinaryPrimitives.WriteUInt32LittleEndian(header[8..12], ddsdCapsHeightWidthPixelFormatLinearSize);
    BinaryPrimitives.WriteUInt32LittleEndian(header[12..16], (uint)height);
    BinaryPrimitives.WriteUInt32LittleEndian(header[16..20], (uint)width);
    BinaryPrimitives.WriteUInt32LittleEndian(header[20..24], (uint)blockData.Length);
    BinaryPrimitives.WriteUInt32LittleEndian(header[76..80], 32);
    BinaryPrimitives.WriteUInt32LittleEndian(header[80..84], ddpfFourCc);
    BinaryPrimitives.WriteUInt32LittleEndian(header[84..88], fourCc);
    BinaryPrimitives.WriteUInt32LittleEndian(header[108..112], ddsCapsTexture);
    if (dxgiFormat.HasValue)
    {
        var dx10 = output.AsSpan(128, 20);
        BinaryPrimitives.WriteUInt32LittleEndian(dx10[0..4], dxgiFormat.Value);
        BinaryPrimitives.WriteUInt32LittleEndian(dx10[4..8], 3);  // DDS_DIMENSION_TEXTURE2D
        BinaryPrimitives.WriteUInt32LittleEndian(dx10[12..16], 1); // array size
    }
    blockData.CopyTo(output.AsSpan(headerLength));
    return output;
}

static Dictionary<string, object?> ExportTextureDds(
    CUE4Parse.UE4.Assets.IPackage package,
    string? ddsOutput)
{
    var texture = package.GetExports().OfType<UTexture2D>().FirstOrDefault();
    if (texture is null)
        throw new InvalidOperationException("Package does not contain a Texture2D export.");
    var mip = texture.GetFirstMip();
    var mipData = mip.BulkData.Data ?? throw new InvalidOperationException("Texture mip data is unavailable.");
    var blockCompressed = texture.Format is
        EPixelFormat.PF_DXT1 or EPixelFormat.PF_DXT3 or EPixelFormat.PF_DXT5 or
        EPixelFormat.PF_BC4 or EPixelFormat.PF_BC5 or EPixelFormat.PF_BC6H or
        EPixelFormat.PF_BC7;
    var ddsBytes = blockCompressed
        ? WrapBlockCompressedAsDds(mip.SizeX, mip.SizeY, mipData, texture.Format, texture.SRGB)
        : TextureEncoder.Encode(
            new CTexture(mip.SizeX, mip.SizeY, texture.Format, mipData),
            ETextureFormat.Dds,
            texture.SRGB,
            out _);
    if (!string.IsNullOrWhiteSpace(ddsOutput))
        File.WriteAllBytes(Path.GetFullPath(ddsOutput), ddsBytes);
    return new Dictionary<string, object?>
    {
        ["width"] = mip.SizeX,
        ["height"] = mip.SizeY,
        ["pixelFormat"] = texture.Format.ToString(),
        ["srgb"] = texture.SRGB,
        ["fileExtension"] = "dds",
        ["byteLength"] = ddsBytes.Length,
        ["output"] = ddsOutput
    };
}

static Dictionary<string, object?> ExportStaticMeshAsset(UStaticMesh sourceMesh)
{
    var lods = sourceMesh.RenderData?.LODs
        ?? throw new InvalidOperationException(
            "StaticMesh render data is unavailable. This Rebirth layout requires " +
            "CUE4Parse's packed-tangent support from 2026-08-28 or newer.");
    var lodIndex = Array.FindIndex(lods, lod =>
        !lod.SkipLod && lod.PositionVertexBuffer is { NumVertices: > 0 } &&
        lod.VertexBuffer is { NumVertices: > 0 } && lod.IndexBuffer is { Length: > 0 });
    if (lodIndex < 0)
        throw new InvalidOperationException("StaticMesh has no importable conventional LOD.");

    var sourceLod = lods[lodIndex];
    var positions = sourceLod.PositionVertexBuffer!.Verts;
    var vertexItems = sourceLod.VertexBuffer!.UV;
    var indices = sourceLod.IndexBuffer!.Buffer;
    if (positions.Length != vertexItems.Length)
        throw new InvalidOperationException(
            $"StaticMesh position/attribute count mismatch ({positions.Length} vs {vertexItems.Length}).");

    var uvChannels = new List<object?>();
    for (var channelIndex = 0; channelIndex < sourceLod.VertexBuffer.NumTexCoords; channelIndex++)
    {
        var capturedChannel = channelIndex;
        uvChannels.Add(vertexItems.Select(item =>
        {
            var uv = item.UV[capturedChannel];
            return new[] { uv.U, uv.V };
        }).ToArray());
    }

    var sections = sourceLod.Sections.Select(section =>
    {
        var staticMaterial = section.MaterialIndex >= 0 && section.MaterialIndex < sourceMesh.StaticMaterials.Length
            ? sourceMesh.StaticMaterials[section.MaterialIndex]
            : null;
        return (object?)new Dictionary<string, object?>
        {
            ["materialIndex"] = section.MaterialIndex,
            ["materialName"] = staticMaterial?.MaterialSlotName.ToString(),
            ["materialPath"] = staticMaterial?.MaterialInterface?.GetPathName(),
            ["firstIndex"] = section.FirstIndex,
            ["triangleCount"] = section.NumTriangles,
            ["castShadow"] = section.bCastShadow,
        };
    }).ToList();

    return new Dictionary<string, object?>
    {
        ["sourceType"] = "UStaticMesh",
        ["name"] = sourceMesh.Name.ToString(),
        ["renderData"] = "ConventionalLOD",
        ["lodIndex"] = lodIndex,
        ["vertexCount"] = positions.Length,
        ["triangleCount"] = indices.Length / 3,
        ["positions"] = positions.Select(position =>
            new[] { position.X, position.Y, position.Z }).ToArray(),
        ["normals"] = vertexItems.Select(item =>
            new[] { item.Normal[2].X, item.Normal[2].Y, item.Normal[2].Z }).ToArray(),
        ["tangents"] = vertexItems.Select(item =>
            new[] { item.Normal[0].X, item.Normal[0].Y, item.Normal[0].Z, item.Normal[2].W }).ToArray(),
        ["uvChannels"] = uvChannels,
        ["colors"] = sourceLod.ColorVertexBuffer?.Data.Select(color =>
            new[] { (int) color.R, (int) color.G, (int) color.B, (int) color.A }).ToArray(),
        ["indices"] = indices,
        ["sections"] = sections,
    };
}

static Dictionary<string, object?> ExportRebirthFlattenedStaticMesh(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IoPackage package)
{
    // Rebirth 1.005 moved the static-mesh GPU streams into one flat bulk payload.
    // Unlike UE's normal cooked layout, that payload has no per-buffer headers;
    // the sizes and FF7 meshlet-table counts live in the uasset export instead.
    var meshName = Path.GetFileNameWithoutExtension(assetPath);
    var meshIndex = Array.FindIndex(package.ExportMap,
        entry => package.CreateFNameFromMappedName(entry.ObjectName).Text == meshName);
    if (meshIndex < 0)
        throw new InvalidOperationException($"Could not locate StaticMesh export '{meshName}'.");

    var rawPackage = provider.Files[assetPath].Read(null);
    using var archive = new FAssetArchive(
        new FByteArchive(assetPath, rawPackage, provider.Versions), package);
    var summary = archive.Read<FPackageSummary>();
    archive.Position = summary.ExportBundlesOffset;
    var remainingBundleEntryCount =
        (summary.GraphDataOffset - summary.ExportBundlesOffset) / (sizeof(int) * 2);
    var foundBundleEntryCount = 0;
    var bundleHeaders = new List<FExportBundleHeader>();
    while (foundBundleEntryCount < remainingBundleEntryCount)
    {
        remainingBundleEntryCount--;
        var header = new FExportBundleHeader(archive);
        foundBundleEntryCount += (int) header.EntryCount;
        bundleHeaders.Add(header);
    }
    if (foundBundleEntryCount != remainingBundleEntryCount)
        throw new InvalidOperationException("Could not decode the IoStore export-bundle table.");
    var bundleEntries = archive.ReadArray<FExportBundleEntry>(foundBundleEntryCount);
    var exportPosition = summary.GraphDataOffset + summary.GraphDataSize;
    var foundPosition = -1;
    foreach (var bundle in bundleHeaders)
    {
        for (var index = 0u; index < bundle.EntryCount; index++)
        {
            var entry = bundleEntries[bundle.FirstEntryIndex + index];
            if (entry.CommandType != EExportCommandType.ExportCommandType_Serialize)
                continue;
            if (entry.LocalExportIndex == meshIndex)
                foundPosition = exportPosition;
            exportPosition += (int) package.ExportMap[entry.LocalExportIndex].CookedSerialSize;
        }
    }
    if (foundPosition < 0)
        throw new InvalidOperationException($"No serialized export-bundle entry was found for '{meshName}'.");

    var export = package.ExportMap[meshIndex];
    archive.AbsoluteOffset = (int) export.CookedSerialOffset - foundPosition;
    archive.Position = foundPosition;
    var validPosition = archive.Position + (long) export.CookedSerialSize;

    // Deserialize only UObject's property header. UStaticMesh's stock reader
    // cannot cross the flat 1.005 render-data payload yet.
    var propertyReader = new UObject
    {
        Name = meshName,
        Class = package.ResolveObjectIndex(export.ClassIndex),
        Outer = package.ResolveObjectIndex(export.OuterIndex),
        Super = package.ResolveObjectIndex(export.SuperIndex),
        Template = package.ResolveObjectIndex(export.TemplateIndex),
        Flags = export.ObjectFlags,
    };
    propertyReader.Deserialize(archive, validPosition);

    _ = new FStripDataFlags(archive);
    var cooked = archive.ReadBoolean();
    _ = new FPackageIndex(archive); // BodySetup
    if (archive.Versions["StaticMesh.HasNavCollision"])
        _ = new FPackageIndex(archive);
    archive.Position += 16; // LightingGuid
    _ = archive.ReadArray(() => new FPackageIndex(archive)); // Sockets
    if (!cooked)
        throw new InvalidOperationException("StaticMesh has no cooked render data.");

    var lodCount = archive.Read<int>();
    if (lodCount is < 1 or > 8)
        throw new InvalidOperationException($"Unexpected Rebirth static-mesh LOD count {lodCount}.");

    // The first conventional LOD is the import target. This is also what FModel
    // exported for the reference asset before the 1.005 layout change.
    _ = new FStripDataFlags(archive);
    var sourceSections = archive.ReadArray(() => new FStaticMeshSection(archive));
    _ = archive.Read<float>(); // MaxDeviation
    var cookedOut = archive.ReadBoolean();
    var inlined = archive.ReadBoolean();
    if (cookedOut || inlined)
        throw new InvalidOperationException(
            $"Unsupported Rebirth LOD flags (cookedOut={cookedOut}, inlined={inlined}).");

    var bulk = new FByteBulkData(archive);
    var bulkData = bulk.Data;
    if (bulkData is null)
    {
        // A hand-opened IoStore export archive does not inherit the package
        // reader's payload callback. Resolve its virtual ubulk directly.
        var bulkPath = Path.ChangeExtension(assetPath, ".ubulk").Replace('\\', '/');
        if (!provider.Files.TryGetValue(bulkPath, out var bulkFile))
            throw new InvalidOperationException($"StaticMesh bulk payload '{bulkPath}' is unavailable.");
        var completeBulk = bulkFile.Read(null);
        var size = checked((int) bulk.Header.SizeOnDisk);
        var offset = completeBulk.Length == size ? 0 : checked((int) bulk.Header.OffsetInFile);
        if (offset < 0 || size <= 0 || offset + size > completeBulk.Length)
            throw new InvalidOperationException("StaticMesh bulk payload range is invalid.");
        bulkData = completeBulk.AsSpan(offset, size).ToArray();
    }

    _ = archive.Read<int>(); // DepthOnlyNumTriangles
    _ = archive.Read<int>(); // packed index-buffer flags
    var uvChannelCount = archive.Read<int>();
    var vertexCount = archive.Read<int>();
    var fullPrecisionUvs = archive.ReadBoolean();
    var highPrecisionTangents = archive.ReadBoolean();
    var positionStride = archive.Read<int>();
    var positionCount = archive.Read<int>();
    var colorStride = archive.Read<int>();
    var colorCount = archive.Read<int>();
    _ = archive.Read<int>(); // legacy index-buffer field
    _ = archive.ReadBoolean(); // 32-bit indices

    var batchCount = archive.Read<int>();
    var meshletIndexCount = archive.Read<int>();
    var packedTriangleCount = archive.Read<int>();
    _ = archive.Read<int>(); // batch-info count
    _ = archive.Read<int>(); // internal LOD table count
    var batchIndexCount = archive.Read<int>();
    var auxiliaryStructCount = archive.Read<int>();
    var zeroIndex = archive.Read<int>();

    if (vertexCount <= 0 || positionCount != vertexCount || positionStride != 12 ||
        uvChannelCount is < 1 or > 8 || fullPrecisionUvs || highPrecisionTangents ||
        colorStride is not (0 or 4) || colorCount is not (0) && colorCount != vertexCount)
        throw new InvalidOperationException(
            "Unsupported flattened StaticMesh vertex-buffer metadata " +
            $"(vertices={vertexCount}, positions={positionCount}x{positionStride}, " +
            $"uvs={uvChannelCount}, fullUV={fullPrecisionUvs}, highTangent={highPrecisionTangents}, " +
            $"colors={colorCount}x{colorStride}).");
    if (batchCount <= 0 || meshletIndexCount <= 0 || packedTriangleCount <= 0 ||
        batchIndexCount < 0 || auxiliaryStructCount != 0 || zeroIndex != 0)
        throw new InvalidOperationException("Unsupported flattened StaticMesh meshlet metadata.");

    // The updated FF7LodInfo tail uses 2/5/6 elements for its ushort/float
    // arrays (76 bytes total), rather than the older 4/5/14 shape.
    archive.Position += 24; // retail-only opaque render-data header
    var sectionLodIndices = archive.ReadArray<int>();
    var lodInfoCount = archive.Read<int>();
    if (lodInfoCount is < 1 or > 4096)
        throw new InvalidOperationException($"Unexpected FF7 LOD-info count {lodInfoCount}.");
    var lodInfos = new (int BatchesOffset, int BatchesCount, int VerticesOffset, int VerticesCount)[lodInfoCount];
    for (var index = 0; index < lodInfoCount; index++)
    {
        _ = archive.Read<int>(); // Index
        _ = archive.Read<int>(); // unknown
        _ = archive.Read<int>(); // Offset
        _ = archive.Read<int>(); // Count
        _ = archive.Read<int>(); // IndicesOffset
        _ = archive.Read<int>(); // IndicesCount
        var batchesOffset = archive.Read<int>();
        var batchesCount = archive.Read<int>();
        var verticesOffset = archive.Read<int>();
        var verticesCount = archive.Read<int>();
        archive.Position += 36; // ushort[2], float[5], ushort[6]
        lodInfos[index] = (batchesOffset, batchesCount, verticesOffset, verticesCount);
    }
    if (sectionLodIndices.Length != sourceSections.Length ||
        sectionLodIndices.Any(index => index < 0 || index >= lodInfos.Length))
        throw new InvalidOperationException("FF7 section-to-LOD mapping is invalid.");

    _ = archive.ReadArray<int>(); // secondary section indices
    var secondaryLodInfoCount = archive.Read<int>();
    if (secondaryLodInfoCount is < 0 or > 4096)
        throw new InvalidOperationException($"Unexpected secondary FF7 LOD-info count {secondaryLodInfoCount}.");
    archive.Position += secondaryLodInfoCount * 76L;
    var trailingIndexCount = archive.Read<int>();
    var flattenedVersion = archive.Read<int>();
    var flattenedFlags = archive.Read<int>();
    if (trailingIndexCount != 0 || flattenedVersion != 1 || flattenedFlags != 0)
        throw new InvalidOperationException("Unsupported flattened StaticMesh buffer-table header.");

    // The first entries describe the conventional vertex buffers and absent
    // legacy index buffers. The GPU/meshlet ranges begin 44 bytes later.
    archive.Position += 44;
    (int Offset, int Size) ReadRange()
    {
        return (archive.Read<int>(), archive.Read<int>());
    }
    var batchRange = ReadRange();
    _ = ReadRange(); // batch-info/internal-LOD data
    var meshletIndexRange = ReadRange();
    var packedTriangleRange = ReadRange();
    _ = ReadRange(); // auxiliary meshlet data A
    _ = ReadRange(); // auxiliary meshlet data B
    var batchIndexRange = ReadRange();

    var tangentStride = 4; // 1.005 R10G10B10A2_UNORM tangent frame
    var uvStride = 4; // two half floats
    var positionOffset = 0;
    var tangentOffset = checked(positionOffset + vertexCount * positionStride);
    var uvOffset = checked(tangentOffset + vertexCount * tangentStride);
    var colorOffset = checked(uvOffset + vertexCount * uvChannelCount * uvStride);
    var batchOffset = batchRange.Offset;
    var meshletIndexOffset = meshletIndexRange.Offset;
    var packedTriangleOffset = packedTriangleRange.Offset;
    if (batchOffset != checked(colorOffset + colorCount * colorStride) ||
        batchRange.Size != checked(batchCount * 16) ||
        meshletIndexRange.Size != checked(meshletIndexCount * sizeof(uint)) ||
        packedTriangleRange.Size != checked(packedTriangleCount * sizeof(uint)) ||
        batchIndexRange.Size != checked(batchIndexCount * sizeof(uint)) ||
        batchOffset < 0 || meshletIndexOffset < batchOffset + batchRange.Size ||
        packedTriangleOffset < meshletIndexOffset + meshletIndexRange.Size ||
        packedTriangleOffset + packedTriangleRange.Size > bulkData.Length)
        throw new InvalidOperationException("Flattened StaticMesh bulk-buffer ranges overlap or exceed the payload.");

    static uint ReadU32(byte[] data, int offset) =>
        BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, sizeof(uint)));
    static int ReadI32(byte[] data, int offset) => unchecked((int) ReadU32(data, offset));
    static float ReadF32(byte[] data, int offset) =>
        BitConverter.Int32BitsToSingle(ReadI32(data, offset));

    var positions = new float[vertexCount][];
    var normals = new float[vertexCount][];
    var tangents = new float[vertexCount][];
    var uvChannels = Enumerable.Range(0, uvChannelCount)
        .Select(_ => new float[vertexCount][]).ToArray();
    byte[][]? colors = colorCount > 0 ? new byte[vertexCount][] : null;
    for (var vertex = 0; vertex < vertexCount; vertex++)
    {
        var position = positionOffset + vertex * positionStride;
        positions[vertex] = [ReadF32(bulkData, position), ReadF32(bulkData, position + 4), ReadF32(bulkData, position + 8)];

        var packedFrame = ReadU32(bulkData, tangentOffset + vertex * tangentStride);
        var u = (packedFrame & 1023) / 1023.0;
        var v = ((packedFrame >> 10) & 1023) / 1023.0;
        var nx = u - v;
        var ny = u + v - 1.0;
        var nz = 1.0 - Math.Abs(nx) - Math.Abs(ny);
        if ((packedFrame & (1u << 30)) == 0) nz = -nz;
        var normalLength = Math.Sqrt(nx * nx + ny * ny + nz * nz);
        nx /= normalLength;
        ny /= normalLength;
        nz /= normalLength;
        normals[vertex] = [(float) nx, (float) ny, (float) nz];

        var sign = nz >= 0.0 ? -1.0 : 1.0;
        var a = 1.0 / (nz - sign);
        var e1x = 1.0 + sign * nx * nx * a;
        var e1y = sign * nx * ny * a;
        var e1z = sign * nx;
        var e2x = nx * ny * a;
        var e2y = sign + ny * ny * a;
        var e2z = ny;
        var angle = (packedFrame >> 20) & 1023;
        var t = (angle & 255) / 255.0;
        var cx = (angle & 256) != 0 ? t : -t;
        var cy = (angle & 512) != 0 ? 1.0 - t : -(1.0 - t);
        var circleLength = Math.Sqrt(cx * cx + cy * cy);
        cx /= circleLength;
        cy /= circleLength;
        tangents[vertex] =
        [
            (float) (e1x * cx + e2x * cy),
            (float) (e1y * cx + e2y * cy),
            (float) (e1z * cx + e2z * cy),
            (packedFrame & (1u << 31)) != 0 ? 1.0f : -1.0f,
        ];

        for (var channel = 0; channel < uvChannelCount; channel++)
        {
            var uv = uvOffset + (vertex * uvChannelCount + channel) * uvStride;
            uvChannels[channel][vertex] =
            [
                (float) BitConverter.UInt16BitsToHalf(BinaryPrimitives.ReadUInt16LittleEndian(bulkData.AsSpan(uv, 2))),
                (float) BitConverter.UInt16BitsToHalf(BinaryPrimitives.ReadUInt16LittleEndian(bulkData.AsSpan(uv + 2, 2))),
            ];
        }
        if (colors is not null)
        {
            var color = colorOffset + vertex * colorStride;
            colors[vertex] = [bulkData[color], bulkData[color + 1], bulkData[color + 2], bulkData[color + 3]];
        }
    }

    var fullIndices = new List<uint>(packedTriangleCount * 3);
    for (var batch = 0; batch < batchCount; batch++)
    {
        var offset = batchOffset + batch * 16;
        var totalVertices = ReadI32(bulkData, offset + 4);
        var triangleCount = ReadI32(bulkData, offset + 8);
        var totalTriangles = ReadI32(bulkData, offset + 12);
        if (totalVertices < 0 || triangleCount < 0 || totalTriangles < 0 ||
            totalTriangles + triangleCount > packedTriangleCount)
            throw new InvalidOperationException($"Invalid FF7 meshlet batch {batch}.");
        for (var triangle = 0; triangle < triangleCount; triangle++)
        {
            var packed = ReadU32(bulkData, packedTriangleOffset + (totalTriangles + triangle) * 4);
            var local0 = (int) (packed & 0x3ff);
            var local1 = (int) ((packed >> 10) & 0x3ff);
            var local2 = (int) ((packed >> 20) & 0x3ff);
            foreach (var local in new[] { local0, local1, local2 })
            {
                var meshletIndex = totalVertices + local;
                if (meshletIndex < 0 || meshletIndex >= meshletIndexCount)
                    throw new InvalidOperationException($"FF7 meshlet {batch} has an invalid local vertex index.");
                var vertexIndex = ReadU32(bulkData, meshletIndexOffset + meshletIndex * 4);
                if (vertexIndex >= vertexCount)
                    throw new InvalidOperationException($"FF7 meshlet {batch} references vertex {vertexIndex}/{vertexCount}.");
                fullIndices.Add(vertexIndex);
            }
        }
    }

    var selectedIndices = new List<uint>();
    var outputSections = new List<object?>();
    var staticMaterials = propertyReader.GetOrDefault<FStaticMaterial[]>("StaticMaterials", []);
    for (var sectionIndex = 0; sectionIndex < sourceSections.Length; sectionIndex++)
    {
        var sourceSection = sourceSections[sectionIndex];
        var lodInfo = lodInfos[sectionLodIndices[sectionIndex]];
        var firstIndex = selectedIndices.Count;
        var sourceFirstIndex = checked(lodInfo.BatchesOffset * 3);
        var sourceIndexCount = checked(lodInfo.BatchesCount * 3);
        if (sourceFirstIndex < 0 || sourceIndexCount < 0 ||
            sourceFirstIndex + sourceIndexCount > fullIndices.Count)
            throw new InvalidOperationException($"FF7 section {sectionIndex} index range is invalid.");
        selectedIndices.AddRange(fullIndices.GetRange(sourceFirstIndex, sourceIndexCount));

        var staticMaterial = sourceSection.MaterialIndex >= 0 && sourceSection.MaterialIndex < staticMaterials.Length
            ? staticMaterials[sourceSection.MaterialIndex]
            : null;
        outputSections.Add(new Dictionary<string, object?>
        {
            ["materialIndex"] = sourceSection.MaterialIndex,
            ["materialName"] = staticMaterial?.MaterialSlotName.ToString(),
            ["materialPath"] = staticMaterial?.MaterialInterface?.GetPathName(),
            ["firstIndex"] = firstIndex,
            ["triangleCount"] = lodInfo.BatchesCount,
            ["castShadow"] = sourceSection.bCastShadow,
        });
    }

    // The flat payload contains vertices for every internal FF7 LOD. Remove
    // vertices unused by the selected conventional LOD so Blender gets no loose
    // geometry and the JSON remains compact.
    var usedVertices = selectedIndices.Distinct().OrderBy(index => index).ToArray();
    var remap = new Dictionary<uint, uint>(usedVertices.Length);
    for (var index = 0; index < usedVertices.Length; index++)
        remap[usedVertices[index]] = (uint) index;
    var remappedIndices = selectedIndices.Select(index => remap[index]).ToArray();
    var compactPositions = usedVertices.Select(index => positions[index]).ToArray();
    var compactNormals = usedVertices.Select(index => normals[index]).ToArray();
    var compactTangents = usedVertices.Select(index => tangents[index]).ToArray();
    var compactUvs = uvChannels.Select(channel =>
        usedVertices.Select(index => channel[index]).ToArray()).ToArray();
    // System.Text.Json serializes byte[] as base64 strings. Promote the four
    // channels so Blender receives the intended numeric RGBA arrays.
    var compactColors = colors is null
        ? null
        : usedVertices.Select(index =>
            colors[index].Select(component => (int) component).ToArray()).ToArray();

    return new Dictionary<string, object?>
    {
        ["sourceType"] = "FF7 Rebirth flattened UStaticMesh",
        ["name"] = meshName,
        ["renderData"] = "FlattenedMeshlets",
        ["lodIndex"] = 0,
        ["vertexCount"] = compactPositions.Length,
        ["triangleCount"] = remappedIndices.Length / 3,
        ["positions"] = compactPositions,
        ["normals"] = compactNormals,
        ["tangents"] = compactTangents,
        ["uvChannels"] = compactUvs,
        ["colors"] = compactColors,
        ["indices"] = remappedIndices,
        ["sections"] = outputSections,
    };
}

static Dictionary<string, object?> ExportRebirthStaticMesh(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IoPackage package)
{
    try
    {
        return ExportRebirthFlattenedStaticMesh(provider, assetPath, package);
    }
    catch (Exception flattenedError)
    {
        // Before the flattened update, Rebirth used CUE4Parse's conventional
        // buffer layout (including the older 8-byte packed tangent frame).
        // Retain that route as a fallback so both generations remain usable.
        try
        {
            var sourceMesh = ((CUE4Parse.UE4.Assets.IPackage) package)
                .GetExports().OfType<UStaticMesh>().FirstOrDefault()
                ?? throw new InvalidOperationException("The package contains no UStaticMesh export.");
            return ExportStaticMeshAsset(sourceMesh);
        }
        catch (Exception conventionalError)
        {
            throw new InvalidOperationException(
                "StaticMesh could not be decoded as either Rebirth's updated flattened " +
                $"layout ({flattenedError.Message}) or its earlier conventional layout " +
                $"({conventionalError.Message}).",
                new AggregateException(flattenedError, conventionalError));
        }
    }
}

static bool TryDecodeRebirthSkeletalPackedFrames(
    DefaultFileProvider provider,
    string assetPath,
    IReadOnlyList<float[]> positions,
    float expectedFirstU,
    float expectedFirstV,
    out float[][] normals,
    out float[][] tangents)
{
    normals = [];
    tangents = [];
    if (positions.Count == 0)
        return false;
    var bulkData = provider.SavePackage(assetPath)
        .FirstOrDefault(entry => entry.Key.EndsWith(".ubulk", StringComparison.OrdinalIgnoreCase)).Value;
    if (bulkData is null || bulkData.Length == 0)
        return false;

    // The position stream starts at an arbitrary place in the bulk file. Find
    // two consecutive vertices instead of assuming offset zero, then verify
    // that the following 4-byte stream lands on the UV CUE4Parse already read.
    var prefixCount = Math.Min(2, positions.Count);
    var positionPrefix = new byte[prefixCount * 12];
    for (var vertexIndex = 0; vertexIndex < prefixCount; vertexIndex++)
    {
        for (var component = 0; component < 3; component++)
            BitConverter.GetBytes(positions[vertexIndex][component])
                .CopyTo(positionPrefix, vertexIndex * 12 + component * 4);
    }
    var positionOffset = -1;
    for (var offset = 0; offset <= bulkData.Length - positionPrefix.Length; offset++)
    {
        if (bulkData.AsSpan(offset, positionPrefix.Length).SequenceEqual(positionPrefix))
        {
            positionOffset = offset;
            break;
        }
    }
    if (positionOffset < 0)
        return false;
    var tangentOffset = checked(positionOffset + positions.Count * 12);
    var uvOffset = checked(tangentOffset + positions.Count * 4);
    if (uvOffset + 4 > bulkData.Length)
        return false;
    var packedFirstU = (float) BitConverter.UInt16BitsToHalf(
        BinaryPrimitives.ReadUInt16LittleEndian(bulkData.AsSpan(uvOffset, 2)));
    var packedFirstV = (float) BitConverter.UInt16BitsToHalf(
        BinaryPrimitives.ReadUInt16LittleEndian(bulkData.AsSpan(uvOffset + 2, 2)));
    if (MathF.Abs(packedFirstU - expectedFirstU) > 0.001f ||
        MathF.Abs(packedFirstV - expectedFirstV) > 0.001f)
        return false; // Earlier builds retain the conventional 8-byte frame.

    normals = new float[positions.Count][];
    tangents = new float[positions.Count][];
    for (var vertexIndex = 0; vertexIndex < positions.Count; vertexIndex++)
    {
        var packedFrame = BinaryPrimitives.ReadUInt32LittleEndian(
            bulkData.AsSpan(tangentOffset + vertexIndex * 4, 4));
        var u = (packedFrame & 1023) / 1023.0f;
        var v = ((packedFrame >> 10) & 1023) / 1023.0f;
        var nx = u - v;
        var ny = u + v - 1.0f;
        var nz = 1.0f - MathF.Abs(nx) - MathF.Abs(ny);
        if ((packedFrame & (1u << 30)) == 0)
            nz = -nz;
        var normalLength = MathF.Sqrt(nx * nx + ny * ny + nz * nz);
        if (normalLength <= float.Epsilon)
            return false;
        nx /= normalLength;
        ny /= normalLength;
        nz /= normalLength;
        normals[vertexIndex] = [nx, ny, nz];

        var sign = nz >= 0.0f ? -1.0f : 1.0f;
        var a = 1.0f / (nz - sign);
        var e1x = 1.0f + sign * nx * nx * a;
        var e1y = sign * nx * ny * a;
        var e1z = sign * nx;
        var e2x = nx * ny * a;
        var e2y = sign + ny * ny * a;
        var e2z = ny;
        var angle = (packedFrame >> 20) & 1023;
        var t = (angle & 255) / 255.0f;
        var cx = (angle & 256) != 0 ? t : -t;
        var cy = (angle & 512) != 0 ? 1.0f - t : -(1.0f - t);
        var circleLength = MathF.Sqrt(cx * cx + cy * cy);
        cx /= circleLength;
        cy /= circleLength;
        tangents[vertexIndex] =
        [
            e1x * cx + e2x * cy,
            e1y * cx + e2y * cy,
            e1z * cx + e2z * cy,
            (packedFrame & (1u << 31)) != 0 ? 1.0f : -1.0f,
        ];
    }
    return true;
}

static Dictionary<string, object?> ExportRebirthSkeletalMesh(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IPackage package)
{
    var sourceMesh = package.GetExports().OfType<USkeletalMesh>().FirstOrDefault()
        ?? throw new InvalidOperationException("Package does not contain a SkeletalMesh export.");
    var lods = sourceMesh.LODModels ?? [];
    var lodIndex = Array.FindIndex(lods, lod =>
        !lod.SkipLod && lod.VertexBufferGPUSkin.VertsFloat.Length > 0 &&
        lod.Indices?.Buffer is { Length: > 0 });
    if (lodIndex < 0)
        throw new InvalidOperationException("SkeletalMesh has no importable Rebirth LOD.");

    var sourceLod = lods[lodIndex];
    var vertices = sourceLod.VertexBufferGPUSkin.VertsFloat;
    var indices = sourceLod.Indices!.Buffer!;
    var positions = vertices.Select(vertex =>
        new[] { vertex.Pos.X, vertex.Pos.Y, vertex.Pos.Z }).ToArray();
    var hasPackedFrames = TryDecodeRebirthSkeletalPackedFrames(
        provider,
        assetPath,
        positions,
        vertices[0].UV[0].U,
        vertices[0].UV[0].V,
        out var packedNormals,
        out var packedTangents);
    var referenceBones = sourceMesh.ReferenceSkeleton.FinalRefBoneInfo;
    var vertexSections = Enumerable.Repeat(-1, vertices.Length).ToArray();
    for (var sectionIndex = 0; sectionIndex < sourceLod.Sections.Length; sectionIndex++)
    {
        var section = sourceLod.Sections[sectionIndex];
        var firstVertex = checked((int)section.BaseVertexIndex);
        var lastVertex = checked(firstVertex + section.NumVertices);
        if (firstVertex < 0 || lastVertex > vertices.Length)
            throw new InvalidOperationException($"SkeletalMesh section {sectionIndex} vertex range is invalid.");
        for (var vertexIndex = firstVertex; vertexIndex < lastVertex; vertexIndex++)
            vertexSections[vertexIndex] = sectionIndex;
    }
    if (vertexSections.Any(sectionIndex => sectionIndex < 0))
        throw new InvalidOperationException("SkeletalMesh has vertices outside all render sections.");

    var uvChannelCount = sourceLod.VertexBufferGPUSkin.NumTexCoords;
    var uvChannels = new List<object?>();
    for (var channelIndex = 0; channelIndex < uvChannelCount; channelIndex++)
    {
        var capturedChannel = channelIndex;
        uvChannels.Add(vertices.Select(vertex =>
        {
            var uv = vertex.UV[capturedChannel];
            return new[] { uv.U, uv.V };
        }).ToArray());
    }

    var weights = new List<object?>(vertices.Length);
    for (var vertexIndex = 0; vertexIndex < vertices.Length; vertexIndex++)
    {
        var influence = vertices[vertexIndex].Infs;
        var section = sourceLod.Sections[vertexSections[vertexIndex]];
        var vertexWeights = new List<object?>();
        if (influence is not null)
        {
            var divisor = influence.bUse16BitBoneWeight ? 65535.0f : 255.0f;
            for (var influenceIndex = 0; influenceIndex < influence.BoneIndex.Length; influenceIndex++)
            {
                var weight = influence.BoneWeight[influenceIndex];
                if (weight == 0)
                    continue;
                var localBoneIndex = influence.BoneIndex[influenceIndex];
                if (localBoneIndex >= section.BoneMap.Length)
                    throw new InvalidOperationException(
                        $"SkeletalMesh vertex {vertexIndex} has an invalid section-local bone index.");
                var skeletonBoneIndex = section.BoneMap[localBoneIndex];
                if (skeletonBoneIndex >= referenceBones.Length)
                    throw new InvalidOperationException(
                        $"SkeletalMesh vertex {vertexIndex} references an invalid skeleton bone.");
                vertexWeights.Add(new[] { (float)skeletonBoneIndex, weight / divisor });
            }
        }
        weights.Add(vertexWeights);
    }

    var materials = sourceMesh.SkeletalMaterials;
    var sections = sourceLod.Sections.Select(section =>
    {
        var material = section.MaterialIndex >= 0 && section.MaterialIndex < materials.Length
            ? materials[section.MaterialIndex]
            : null;
        return (object?)new Dictionary<string, object?>
        {
            ["materialIndex"] = section.MaterialIndex,
            ["materialName"] = material?.MaterialSlotName.ToString(),
            ["materialPath"] = material?.Material?.GetPathName(),
            ["firstIndex"] = section.BaseIndex,
            ["triangleCount"] = section.NumTriangles,
            ["castShadow"] = section.bCastShadow,
        };
    }).ToList();

    var colors = sourceLod.ColorVertexBuffer.Data;
    if (colors.Length != 0 && colors.Length != vertices.Length)
        throw new InvalidOperationException("SkeletalMesh color stream does not match its vertex stream.");
    return new Dictionary<string, object?>
    {
        ["sourceType"] = "FF7 Rebirth USkeletalMesh",
        ["name"] = sourceMesh.Name.ToString(),
        ["lodIndex"] = lodIndex,
        ["vertexCount"] = vertices.Length,
        ["triangleCount"] = indices.Length / 3,
        ["positions"] = positions,
        ["normals"] = hasPackedFrames ? packedNormals : vertices.Select(vertex =>
            new[] { vertex.Normal[2].X, vertex.Normal[2].Y, vertex.Normal[2].Z }).ToArray(),
        ["tangents"] = hasPackedFrames ? packedTangents : vertices.Select(vertex =>
            new[] { vertex.Normal[0].X, vertex.Normal[0].Y, vertex.Normal[0].Z, vertex.Normal[2].W }).ToArray(),
        ["normalFormat"] = hasPackedFrames ? "R10G10B10A2 packed tangent frame" : "legacy FPackedNormal frame",
        ["uvChannels"] = uvChannels,
        ["colors"] = colors.Length == 0 ? null : colors.Select(color =>
            new[] { (int)color.R, (int)color.G, (int)color.B, (int)color.A }).ToArray(),
        ["indices"] = indices,
        ["sections"] = sections,
        ["boneNames"] = referenceBones.Select(bone => bone.Name.ToString()).ToArray(),
        ["weights"] = weights,
        ["linkedSkeletonPath"] = GetLinkedSkeletonPath(sourceMesh),
        ["skeletonPath"] = ObjectPathToVirtualAssetPath(GetLinkedSkeletonPath(sourceMesh)),
    };
}

static Dictionary<string, object?> ExportKdiAsset(CUE4Parse.UE4.Assets.IPackage package)
{
    var export = package.GetExports().FirstOrDefault()
        ?? throw new InvalidOperationException("KDI package has no exports.");
    var properties = new Dictionary<string, object?>();
    foreach (var property in export.Properties)
    {
        if (ConvertValue(property) is not Dictionary<string, object?> converted)
            continue;
        if (!converted.TryGetValue("name", out var name) || name is null)
            continue;
        properties[name.ToString()!] = converted.TryGetValue("value", out var value)
            ? NormalizeKdiValue(value)
            : null;
    }
    return new Dictionary<string, object?>
    {
        ["Type"] = export.ExportType,
        ["Name"] = export.Name.ToString(),
        ["Class"] = $"UScriptClass'{export.ExportType}'",
        ["Package"] = package.Name,
        ["Properties"] = properties
    };
}

static Dictionary<string, object?> ExportUmapActors(CUE4Parse.UE4.Assets.IPackage package)
{
    var supportedTypes = new HashSet<string>(StringComparer.Ordinal)
    {
        "EndEnvironmentStaticMeshComponent",
        "StaticMeshComponent",
        "EndSkeletalMeshComponent",
        "PointLightComponent",
        "SpotLightComponent",
        "LevelStreamingAlwaysLoaded",
        "LevelStreamingDynamic",
        "EndStreamingVolume",
    };
    var actors = new List<object?>();
    foreach (var export in package.GetExports().Where(export => supportedTypes.Contains(export.ExportType)))
    {
        var properties = new Dictionary<string, object?>();
        foreach (var property in export.Properties)
        {
            if (ConvertValue(property) is not Dictionary<string, object?> converted ||
                !converted.TryGetValue("name", out var name) || name is null)
                continue;
            properties[name.ToString()!] = converted.TryGetValue("value", out var value)
                ? NormalizeKdiValue(value)
                : null;
        }

        // The existing JSON map importer uses Outer only to give component-derived
        // objects readable actor names.  A best-effort name is enough; transforms
        // and asset references come from the component properties themselves.
        var outerName = export.Outer?.Name.ToString() ?? string.Empty;
        actors.Add(new Dictionary<string, object?>
        {
            ["Type"] = export.ExportType,
            ["Name"] = export.Name.ToString(),
            ["Outer"] = outerName,
            ["Properties"] = properties,
        });
    }
    return new Dictionary<string, object?>
    {
        ["sourceType"] = "UMAP package actor export",
        ["actorCount"] = actors.Count,
        ["actors"] = actors,
    };
}

static string?[] ExportImportNames(CUE4Parse.UE4.Assets.IPackage package)
{
    return Enumerable.Range(0, package.ImportMapLength)
        .Select(index => package.ResolvePackageIndex(new FPackageIndex(package, -index - 1)))
        .Select(resolved => resolved?.GetPathName() ?? resolved?.Name.ToString())
        .ToArray();
}

static Dictionary<string, object?> ExportBoneTransform(CUE4Parse.UE4.Objects.Core.Math.FTransform transform)
{
    return new Dictionary<string, object?>
    {
        ["translation"] = new[] { transform.Translation.X, transform.Translation.Y, transform.Translation.Z },
        ["rotation"] = new[] { transform.Rotation.X, transform.Rotation.Y, transform.Rotation.Z, transform.Rotation.W },
        ["scale"] = new[] { transform.Scale3D.X, transform.Scale3D.Y, transform.Scale3D.Z },
    };
}

static Dictionary<string, object?> ExportReferenceSkeleton(FReferenceSkeleton referenceSkeleton)
{
    var bones = new List<object?>();
    var boneInfo = referenceSkeleton.FinalRefBoneInfo;
    var bonePose = referenceSkeleton.FinalRefBonePose;
    for (var index = 0; index < boneInfo.Length; index++)
    {
        var bone = index < bonePose.Length
            ? ExportBoneTransform(bonePose[index])
            : ExportBoneTransform(default);
        bone["name"] = boneInfo[index].Name.ToString();
        bone["parentIndex"] = boneInfo[index].ParentIndex;
        bones.Add(bone);
    }
    return new Dictionary<string, object?>
    {
        ["boneCount"] = bones.Count,
        ["bones"] = bones,
    };
}

static List<object?> ExportSockets(FPackageIndex[]? socketRefs)
{
    var sockets = new List<object?>();
    if (socketRefs is null)
        return sockets;
    foreach (var socketRef in socketRefs)
    {
        USkeletalMeshSocket? socket;
        try
        {
            socket = socketRef.Load<USkeletalMeshSocket>();
        }
        catch
        {
            continue;
        }
        if (socket is null)
            continue;
        var rotation = socket.RelativeRotation.Quaternion();
        sockets.Add(new Dictionary<string, object?>
        {
            ["name"] = socket.SocketName.ToString(),
            ["boneName"] = socket.BoneName.ToString(),
            ["translation"] = new[] { socket.RelativeLocation.X, socket.RelativeLocation.Y, socket.RelativeLocation.Z },
            ["rotation"] = new[] { rotation.X, rotation.Y, rotation.Z, rotation.W },
            ["scale"] = new[] { socket.RelativeScale.X, socket.RelativeScale.Y, socket.RelativeScale.Z },
        });
    }
    return sockets;
}

static Dictionary<string, object?> ExportSkeletonPackage(CUE4Parse.UE4.Assets.IPackage package)
{
    var skeleton = package.GetExports().OfType<USkeleton>().FirstOrDefault();
    if (skeleton is not null)
    {
        var result = ExportReferenceSkeleton(skeleton.ReferenceSkeleton);
        result["sourceType"] = "USkeleton";
        result["sockets"] = ExportSockets(skeleton.Sockets);
        return result;
    }

    var mesh = package.GetExports().OfType<USkeletalMesh>().FirstOrDefault()
        ?? throw new InvalidOperationException("Package does not contain a Skeleton or SkeletalMesh export.");
    if (mesh.ReferenceSkeleton is null)
        throw new InvalidOperationException(
            "This SkeletalMesh's own ReferenceSkeleton is not populated (its bind pose data may live outside " +
            "the mounted package). Import its linked Skeleton asset instead.");
    var meshResult = ExportReferenceSkeleton(mesh.ReferenceSkeleton);
    meshResult["sourceType"] = "USkeletalMesh";
    meshResult["sockets"] = ExportSockets(mesh.Sockets);
    if (!mesh.Skeleton.IsNull)
    {
        try
        {
            meshResult["linkedSkeletonPath"] = mesh.Skeleton.ResolvedObject?.GetPathName();
        }
        catch
        {
            // Best-effort only; the mesh's own ReferenceSkeleton above is already complete.
        }
    }
    return meshResult;
}

static string? GetLinkedSkeletonPath(USkeletalMesh mesh)
{
    if (mesh.Skeleton is null || mesh.Skeleton.IsNull)
        return null;
    try
    {
        return mesh.Skeleton.ResolvedObject?.GetPathName();
    }
    catch
    {
        return null;
    }
}

static string? ObjectPathToVirtualAssetPath(string? objectPath)
{
    if (string.IsNullOrWhiteSpace(objectPath) ||
        !objectPath.StartsWith("/Game/", StringComparison.OrdinalIgnoreCase))
        return null;
    var packagePath = objectPath[6..];
    var objectSeparator = packagePath.LastIndexOf('.');
    if (objectSeparator >= 0)
        packagePath = packagePath[..objectSeparator];
    return "End/Content/" + packagePath + ".uasset";
}

static List<int> ExportBoneIndices(IEnumerable<short>? indices)
{
    return indices?.Select(index => (int)index).ToList() ?? new List<int>();
}

static List<int> ExportUnsignedBoneIndices(IEnumerable<ushort>? indices)
{
    return indices?.Select(index => (int)index).ToList() ?? new List<int>();
}

static Dictionary<string, object?> ExportMeshUsagePackage(CUE4Parse.UE4.Assets.IPackage package)
{
    var mesh = package.GetExports().OfType<USkeletalMesh>().FirstOrDefault()
        ?? throw new InvalidOperationException("Package does not contain a SkeletalMesh export.");

    var lods = new List<object?>();
    foreach (var lod in mesh.LODModels ?? Array.Empty<FStaticLODModel>())
    {
        if (lod is null)
            continue;
        var sections = new List<object?>();
        foreach (var section in lod.Sections ?? Array.Empty<FSkelMeshSection>())
        {
            if (section is null)
                continue;
            sections.Add(new Dictionary<string, object?>
            {
                ["boneMap"] = ExportUnsignedBoneIndices(section.BoneMap),
                ["baseVertexIndex"] = section.BaseVertexIndex,
                ["vertexCount"] = section.NumVertices,
                ["maxBoneInfluences"] = section.MaxBoneInfluences,
            });
        }
        lods.Add(new Dictionary<string, object?>
        {
            ["activeBoneIndices"] = ExportBoneIndices(lod.ActiveBoneIndices),
            ["requiredBones"] = ExportBoneIndices(lod.RequiredBones),
            ["sections"] = sections,
            ["vertexCount"] = lod.NumVertices,
        });
    }

    var result = new Dictionary<string, object?>
    {
        ["sourceType"] = "USkeletalMesh",
        ["meshReferenceSkeletonPresent"] = mesh.ReferenceSkeleton is not null,
        ["linkedSkeletonPath"] = GetLinkedSkeletonPath(mesh),
        ["lods"] = lods,
    };
    if (mesh.ReferenceSkeleton is not null)
        result["meshReferenceSkeleton"] = ExportReferenceSkeleton(mesh.ReferenceSkeleton);
    return result;
}

static CUE4Parse.UE4.Assets.IPackage LoadPackageWithoutEditorFiltering(
    DefaultFileProvider provider,
    string assetPath)
{
    var files = provider.SavePackage(assetPath);
    byte[]? GetPart(string extension) => files
        .FirstOrDefault(entry => entry.Key.EndsWith(extension, StringComparison.OrdinalIgnoreCase)).Value;
    var uasset = GetPart(".uasset")
        ?? throw new FileNotFoundException("The package has no UAsset payload.", assetPath);
    return new CUE4Parse.UE4.Assets.Package(
        assetPath,
        uasset,
        GetPart(".uexp"),
        GetPart(".ubulk"),
        GetPart(".uptnl"),
        provider,
        false);
}

static (RebirthSkeletalMeshUsageReader Reader, string MeshName, long ExportOffset, long ExportSize) ReadRebirthMeshUsage(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IoPackage package)
{
    var parsedMesh = ((CUE4Parse.UE4.Assets.IPackage)package)
        .GetExports().OfType<USkeletalMesh>().FirstOrDefault()
        ?? throw new InvalidOperationException("Package has no SkeletalMesh export.");
    var meshName = parsedMesh.Name;
    var meshIndex = Array.FindIndex(package.ExportMap,
        entry => package.CreateFNameFromMappedName(entry.ObjectName).Text == meshName);
    if (meshIndex < 0)
        throw new InvalidOperationException($"Could not locate SkeletalMesh export '{meshName}'.");

    var rawPackage = provider.Files[assetPath].Read(null);
    var archive = new FAssetArchive(
        new FByteArchive(assetPath, rawPackage, provider.Versions), package);
    var summary = archive.Read<FPackageSummary>();
    archive.Position = summary.ExportBundlesOffset;
    var remainingBundleEntryCount =
        (summary.GraphDataOffset - summary.ExportBundlesOffset) / (sizeof(int) * 2);
    var foundBundleEntryCount = 0;
    var bundleHeaders = new List<FExportBundleHeader>();
    while (foundBundleEntryCount < remainingBundleEntryCount)
    {
        remainingBundleEntryCount--;
        var header = new FExportBundleHeader(archive);
        foundBundleEntryCount += (int)header.EntryCount;
        bundleHeaders.Add(header);
    }
    if (foundBundleEntryCount != remainingBundleEntryCount)
        throw new InvalidOperationException("Could not decode the IoStore export-bundle table.");
    var bundleEntries = archive.ReadArray<FExportBundleEntry>(foundBundleEntryCount);
    var exportPosition = summary.GraphDataOffset + summary.GraphDataSize;
    var foundPosition = -1;
    foreach (var bundle in bundleHeaders)
    {
        for (var index = 0u; index < bundle.EntryCount; index++)
        {
            var entry = bundleEntries[bundle.FirstEntryIndex + index];
            if (entry.CommandType == EExportCommandType.ExportCommandType_Serialize)
            {
                if (entry.LocalExportIndex == meshIndex)
                    foundPosition = exportPosition;
                exportPosition += (int)package.ExportMap[entry.LocalExportIndex].CookedSerialSize;
            }
        }
    }
    if (foundPosition < 0)
        throw new InvalidOperationException($"No serialized export-bundle entry was found for '{meshName}'.");

    var export = package.ExportMap[meshIndex];
    archive.AbsoluteOffset = (int)export.CookedSerialOffset - foundPosition;
    archive.Position = foundPosition;
    var reader = new RebirthSkeletalMeshUsageReader();
    reader.Name = meshName;
    reader.Class = parsedMesh.Class;
    reader.Outer = parsedMesh.Outer;
    reader.Super = parsedMesh.Super;
    reader.Template = parsedMesh.Template;
    reader.Flags = parsedMesh.Flags;
    reader.Deserialize(archive, archive.Position + (long)export.CookedSerialSize);

    return (reader, meshName, foundPosition, (long)export.CookedSerialSize);
}

static Dictionary<string, object?> ExportRebirthMeshUsage(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IoPackage package)
{
    var usage = ReadRebirthMeshUsage(provider, assetPath, package);
    var reader = usage.Reader;
    var result = ExportReferenceSkeleton(reader.ReferenceSkeleton);
    result["sourceType"] = "USkeletalMesh (Rebirth narrow reader)";
    result["nativeExportOffset"] = usage.ExportOffset;
    result["nativeExportSize"] = usage.ExportSize;
    result["lods"] = reader.Lods.Select(lod => (object?)new Dictionary<string, object?>
    {
        ["requiredBones"] = ExportBoneIndices(lod.RequiredBones),
        ["activeBoneIndices"] = ExportBoneIndices(lod.ActiveBoneIndices),
        ["sections"] = lod.Sections.Select(section => (object?)new Dictionary<string, object?>
        {
            ["boneMap"] = ExportUnsignedBoneIndices(section.BoneMap),
            ["baseVertexIndex"] = section.BaseVertexIndex,
            ["vertexCount"] = section.NumVertices,
        }).ToList(),
    }).ToList();
    return result;
}

static List<string> BoneNamesForIndices(FReferenceSkeleton skeleton, IEnumerable<int> indices)
{
    var boneInfo = skeleton.FinalRefBoneInfo;
    return indices
        .Where(index => index >= 0 && index < boneInfo.Length)
        .Select(index => boneInfo[index].Name.ToString())
        .Distinct(StringComparer.OrdinalIgnoreCase)
        .OrderBy(name => name, StringComparer.OrdinalIgnoreCase)
        .ToList();
}

static string VariantIdForAssetPath(string assetPath)
{
    var match = Regex.Match(assetPath, @"PC\d{4}_\d{2}", RegexOptions.IgnoreCase);
    return match.Success ? match.Value.ToUpperInvariant() : "Unknown";
}

static Dictionary<string, object?> ExportRebirthMeshBoneUsage(
    DefaultFileProvider provider,
    string assetPath,
    CUE4Parse.UE4.Assets.IoPackage package)
{
    var usage = ReadRebirthMeshUsage(provider, assetPath, package);
    var reader = usage.Reader;
    var lod = reader.Lods.FirstOrDefault();
    var weightedIndices = lod?.Sections
        .SelectMany(section => section.BoneMap ?? Array.Empty<ushort>())
        .Select(index => (int)index)
        .Distinct()
        .ToList() ?? new List<int>();
    var activeIndices = lod?.ActiveBoneIndices
        .Select(index => (int)index)
        .Distinct()
        .ToList() ?? new List<int>();
    var requiredIndices = lod?.RequiredBones
        .Select(index => (int)index)
        .Distinct()
        .ToList() ?? new List<int>();
    var variantFolderEnd = assetPath.IndexOf("/Model/", StringComparison.OrdinalIgnoreCase);
    return new Dictionary<string, object?>
    {
        ["assetPath"] = assetPath,
        ["meshName"] = usage.MeshName,
        ["variantFolder"] = variantFolderEnd >= 0 ? assetPath[..variantFolderEnd] : null,
        ["variantId"] = VariantIdForAssetPath(assetPath),
        ["meshReferenceBoneCount"] = reader.ReferenceSkeleton.FinalRefBoneInfo.Length,
        // This is the authoritative mesh-specific skeleton subset used by FModel
        // and umodel. It deliberately contains unweighted KDI/helper bones.
        ["referenceBoneNames"] = BoneNamesForIndices(
            reader.ReferenceSkeleton,
            Enumerable.Range(0, reader.ReferenceSkeleton.FinalRefBoneInfo.Length)
        ),
        // Section BoneMaps identify bones directly referenced by vertex influences.
        ["weightedBoneNames"] = BoneNamesForIndices(reader.ReferenceSkeleton, weightedIndices),
        // Active/required also include evaluation dependencies such as ancestor chains.
        ["activeBoneNames"] = BoneNamesForIndices(reader.ReferenceSkeleton, activeIndices),
        ["requiredBoneNames"] = BoneNamesForIndices(reader.ReferenceSkeleton, requiredIndices),
    };
}

static string VirtualAssetToObjectPath(string virtualPath)
{
    var normalized = virtualPath.Replace('\\', '/').TrimStart('/');
    const string contentPrefix = "End/Content/";
    if (!normalized.StartsWith(contentPrefix, StringComparison.OrdinalIgnoreCase))
        throw new ArgumentException("Expected an End/Content asset path.", nameof(virtualPath));
    var packagePath = normalized[contentPrefix.Length..];
    if (packagePath.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase))
        packagePath = packagePath[..^".uasset".Length];
    var objectName = packagePath[(packagePath.LastIndexOf('/') + 1)..];
    return $"/Game/{packagePath}.{objectName}";
}

static Dictionary<string, object?> FindMeshAssetsByExportType(
    DefaultFileProvider provider,
    string? pathFilter = null)
{
    // Loading an IoPackage reads its package tables but does not deserialize the
    // exports themselves. That lets the picker identify actual object classes,
    // including the updated static meshes whose full legacy reader desyncs.
    var staticMeshes = new List<string>();
    var skeletalMeshes = new List<string>();
    var failures = 0;
    foreach (var path in provider.Files.Keys
        .Where(path => path.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase))
        .Where(path => string.IsNullOrWhiteSpace(pathFilter) ||
            path.Contains(pathFilter, StringComparison.OrdinalIgnoreCase))
        .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
    {
        try
        {
            if (provider.LoadPackage(path) is not CUE4Parse.UE4.Assets.IoPackage package)
                continue;
            var exportTypes = package.ExportMap
                .Select(export => package.ResolveObjectIndex(export.ClassIndex)?.Name.ToString())
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
            if (exportTypes.Contains("StaticMesh"))
                staticMeshes.Add(path);
            if (exportTypes.Contains("SkeletalMesh"))
                skeletalMeshes.Add(path);
        }
        catch
        {
            failures++;
        }
    }
    return new Dictionary<string, object?>
    {
        ["staticMeshes"] = staticMeshes,
        ["skeletalMeshes"] = skeletalMeshes,
        ["failures"] = failures,
    };
}

static List<object?> FindSkeletonUsers(
    DefaultFileProvider provider,
    string skeletonAssetPath,
    string searchPath)
{
    if (string.IsNullOrWhiteSpace(searchPath))
        throw new ArgumentException("A character-family searchPath is required to avoid scanning every game asset.");

    var targetObjectPath = VirtualAssetToObjectPath(skeletonAssetPath);
    var results = new List<object?>();
    foreach (var path in provider.Files.Keys
        .Where(path => path.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase))
        .Where(path => path.Contains(searchPath, StringComparison.OrdinalIgnoreCase))
        .Where(path => path.Contains("/Model/", StringComparison.OrdinalIgnoreCase))
        .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
    {
        try
        {
            var package = provider.LoadPackage(path);
            var mesh = package.GetExports().OfType<USkeletalMesh>().FirstOrDefault();
            if (mesh is null)
                continue;
            var imports = Enumerable.Range(0, package.ImportMapLength)
                .Select(index => package.ResolvePackageIndex(new FPackageIndex(package, -index - 1)))
                .Select(resolved => resolved?.GetPathName())
                .Where(importPath => !string.IsNullOrWhiteSpace(importPath));
            if (!imports.Any(importPath => string.Equals(
                    importPath, targetObjectPath, StringComparison.OrdinalIgnoreCase)))
                continue;
            results.Add(new Dictionary<string, object?>
            {
                ["assetPath"] = path,
                ["meshName"] = mesh.Name,
                ["variantFolder"] = path[..path.IndexOf("/Model/", StringComparison.OrdinalIgnoreCase)],
            });
        }
        catch
        {
            // Some unrelated assets in the family may use unsupported cooked layouts.
        }
    }
    return results;
}

static Dictionary<string, object?> ExportSkeletonBoneUsage(
    DefaultFileProvider provider,
    string skeletonAssetPath,
    string searchPath)
{
    var users = FindSkeletonUsers(provider, skeletonAssetPath, searchPath);
    var meshes = new List<Dictionary<string, object?>>();
    var failures = new List<object?>();
    foreach (var user in users.OfType<Dictionary<string, object?>>())
    {
        var assetPath = user.TryGetValue("assetPath", out var assetPathValue)
            ? assetPathValue as string
            : null;
        if (string.IsNullOrWhiteSpace(assetPath))
            continue;
        try
        {
            var package = provider.LoadPackage(assetPath) as CUE4Parse.UE4.Assets.IoPackage
                ?? throw new InvalidOperationException("The mesh is not an IoStore package.");
            meshes.Add(ExportRebirthMeshBoneUsage(provider, assetPath, package));
        }
        catch (Exception error)
        {
            failures.Add(new Dictionary<string, object?>
            {
                ["assetPath"] = assetPath,
                ["error"] = error.Message,
            });
        }
    }

    var variants = new Dictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);
    foreach (var mesh in meshes)
    {
        var variant = mesh["variantId"] as string ?? "Unknown";
        if (!variants.TryGetValue(variant, out var bones))
        {
            bones = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            variants[variant] = bones;
        }
        if (mesh["weightedBoneNames"] is IEnumerable<string> weightedNames)
            bones.UnionWith(weightedNames);
    }

    var variantResults = new Dictionary<string, object?>();
    foreach (var variant in variants.OrderBy(entry => entry.Key, StringComparer.OrdinalIgnoreCase))
    {
        var sharedWithOtherVariants = variants
            .Where(other => !string.Equals(other.Key, variant.Key, StringComparison.OrdinalIgnoreCase))
            .SelectMany(other => other.Value)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        variantResults[variant.Key] = new Dictionary<string, object?>
        {
            ["weightedBoneNames"] = variant.Value.OrderBy(name => name, StringComparer.OrdinalIgnoreCase).ToList(),
            ["exclusiveWeightedBoneNames"] = variant.Value
                .Where(name => !sharedWithOtherVariants.Contains(name))
                .OrderBy(name => name, StringComparer.OrdinalIgnoreCase)
                .ToList(),
        };
    }

    return new Dictionary<string, object?>
    {
        ["sourceType"] = "Rebirth narrow skeletal-mesh bone usage",
        ["skeletonAssetPath"] = skeletonAssetPath,
        ["searchedMeshCount"] = users.Count,
        ["parsedMeshCount"] = meshes.Count,
        ["meshes"] = meshes,
        ["variants"] = variantResults,
        ["failures"] = failures,
    };
}

try
{
    var oodlePath = args.Length > 2 ? Path.GetFullPath(args[2]) : null;
    if (!string.IsNullOrWhiteSpace(oodlePath))
    {
        if (!File.Exists(oodlePath))
            throw new FileNotFoundException("Oodle DLL was not found.", oodlePath);
        OodleHelper.Initialize(oodlePath);
    }
    var pakDirectory = Path.Combine(gameDirectory, "End", "Content", "Paks");
    if (!Directory.Exists(pakDirectory))
        pakDirectory = gameDirectory;
    var provider = new DefaultFileProvider(
        pakDirectory,
        SearchOption.AllDirectories,
        new VersionContainer(EGame.GAME_FinalFantasy7Rebirth));
    var usmapPath = args.Length > 5 ? Path.GetFullPath(args[5]) : null;
    if (!string.IsNullOrWhiteSpace(usmapPath))
    {
        if (!File.Exists(usmapPath))
            throw new FileNotFoundException("usmap file was not found.", usmapPath);
        provider.MappingsContainer = new FileUsmapTypeMappingsProvider(usmapPath);
    }
    provider.Initialize();
    var mountedCount = provider.Mount();
    provider.PostMount();

    var operationMode = args.Length > 9 ? args[9] : null;
    if (string.Equals(operationMode, "asset-server", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(operationMode, "texture-server", StringComparison.OrdinalIgnoreCase))
    {
        string? requestLine;
        while ((requestLine = Console.ReadLine()) is not null)
        {
            if (string.IsNullOrWhiteSpace(requestLine))
                continue;
            try
            {
                using var request = JsonDocument.Parse(requestLine);
                var requestRoot = request.RootElement;
                var action = requestRoot.TryGetProperty("action", out var actionElement)
                    ? actionElement.GetString() ?? "texture"
                    : "texture";
                if (string.Equals(action, "release_batch_memory", StringComparison.OrdinalIgnoreCase))
                {
                    var restartThresholdBytes = requestRoot.TryGetProperty("restartThresholdBytes", out var thresholdElement)
                        ? thresholdElement.GetInt64()
                        : 0L;
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        memory = CollectBatchMemory(restartThresholdBytes),
                    }));
                    Console.Out.Flush();
                    continue;
                }
                if (string.Equals(action, "mesh_index", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPathFilter = requestRoot.TryGetProperty("pathFilter", out var pathFilterElement)
                        ? pathFilterElement.GetString()
                        : null;
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        meshIndex = FindMeshAssetsByExportType(provider, requestPathFilter),
                    }));
                    Console.Out.Flush();
                    continue;
                }
                var requestAsset = requestRoot.GetProperty("assetPath").GetString()
                    ?? throw new ArgumentException("Asset request has no assetPath.");
                if (string.Equals(action, "raw", StringComparison.OrdinalIgnoreCase))
                {
                    var requestOutput = requestRoot.GetProperty("output").GetString()
                        ?? throw new ArgumentException("Raw request has no output path.");
                    if (!provider.Files.TryGetValue(requestAsset, out var gameFile))
                        throw new FileNotFoundException("Virtual asset path was not found.", requestAsset);
                    var rawBytes = gameFile.Read(null);
                    File.WriteAllBytes(Path.GetFullPath(requestOutput), rawBytes);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        rawByteLength = rawBytes.Length
                    }));
                }
                else if (string.Equals(action, "metadata", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        importNames = ExportImportNames(requestPackage)
                    }));
                }
                else if (string.Equals(action, "kdi", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        kdi = ExportKdiAsset(requestPackage)
                    }));
                }
                else if (string.Equals(action, "umap_actors", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        umapActors = ExportUmapActors(requestPackage)
                    }));
                }
                else if (string.Equals(action, "umap_data", StringComparison.OrdinalIgnoreCase))
                {
                    // Metadata and actor properties share one package deserialize;
                    // package UMAP imports previously paid this cost twice.
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        importNames = ExportImportNames(requestPackage),
                        umapActors = ExportUmapActors(requestPackage)
                    }));
                }
                else if (string.Equals(action, "skeleton", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        skeleton = ExportSkeletonPackage(requestPackage)
                    }));
                }
                else if (string.Equals(action, "static_mesh", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset) as CUE4Parse.UE4.Assets.IoPackage
                        ?? throw new InvalidOperationException("Rebirth StaticMesh import requires an IoStore package.");
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        staticMesh = ExportRebirthStaticMesh(provider, requestAsset, requestPackage)
                    }));
                }
                else if (string.Equals(action, "skeletal_mesh", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        skeletalMesh = ExportRebirthSkeletalMesh(provider, requestAsset, requestPackage)
                    }));
                }
                else if (string.Equals(action, "mesh_usage", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        meshUsage = ExportMeshUsagePackage(requestPackage)
                    }));
                }
                else if (string.Equals(action, "rebirth_mesh_usage", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = provider.LoadPackage(requestAsset) as CUE4Parse.UE4.Assets.IoPackage
                        ?? throw new InvalidOperationException("Rebirth mesh usage requires an IoStore package.");
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        meshUsage = ExportRebirthMeshUsage(provider, requestAsset, requestPackage)
                    }));
                }
                else if (string.Equals(action, "mesh_usage_unfiltered", StringComparison.OrdinalIgnoreCase))
                {
                    var requestPackage = LoadPackageWithoutEditorFiltering(provider, requestAsset);
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        meshUsage = ExportMeshUsagePackage(requestPackage)
                    }));
                }
                else if (string.Equals(action, "package_parts", StringComparison.OrdinalIgnoreCase))
                {
                    var parts = provider.SavePackage(requestAsset)
                        .Select(entry => new { path = entry.Key, byteLength = entry.Value.Length });
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        assetPath = requestAsset,
                        parts,
                    }));
                }
                else if (string.Equals(action, "skeleton_users", StringComparison.OrdinalIgnoreCase))
                {
                    var searchPath = requestRoot.TryGetProperty("searchPath", out var searchPathElement)
                        ? searchPathElement.GetString() ?? string.Empty
                        : string.Empty;
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        skeletonAssetPath = requestAsset,
                        users = FindSkeletonUsers(provider, requestAsset, searchPath)
                    }));
                }
                else if (string.Equals(action, "skeleton_bone_usage", StringComparison.OrdinalIgnoreCase))
                {
                    var searchPath = requestRoot.TryGetProperty("searchPath", out var searchPathElement)
                        ? searchPathElement.GetString() ?? string.Empty
                        : string.Empty;
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        skeletonBoneUsage = ExportSkeletonBoneUsage(provider, requestAsset, searchPath)
                    }));
                }
                else if (string.Equals(action, "mapping_type", StringComparison.OrdinalIgnoreCase))
                {
                    if (!request.RootElement.TryGetProperty("typeName", out var typeNameElement))
                        throw new ArgumentException("mapping_type requires a typeName.");
                    var typeName = typeNameElement.GetString();
                    if (string.IsNullOrWhiteSpace(typeName) || !provider.MappingsForGame.Types.TryGetValue(typeName, out var mapping))
                    {
                        Console.WriteLine(JsonSerializer.Serialize(new
                        {
                            ok = false,
                            action,
                            error = $"No mapping was found for '{typeName}'.",
                            relatedTypes = provider.MappingsForGame.Types.Keys
                                .Where(key => key.Contains("Skeletal", StringComparison.OrdinalIgnoreCase))
                                .OrderBy(key => key, StringComparer.OrdinalIgnoreCase)
                                .ToArray()
                        }));
                        continue;
                    }
                    Console.WriteLine(JsonSerializer.Serialize(new
                    {
                        ok = true,
                        action,
                        typeName,
                        properties = DescribeMappingProperties(mapping),
                        relatedTypes = provider.MappingsForGame.Types.Keys
                            .Where(key => key.Contains("Skeletal", StringComparison.OrdinalIgnoreCase))
                            .OrderBy(key => key, StringComparer.OrdinalIgnoreCase)
                            .ToArray()
                    }));
                }
                else
                {
                    var requestOutput = requestRoot.GetProperty("ddsOutput").GetString()
                        ?? throw new ArgumentException("Texture request has no ddsOutput.");
                    var requestPackage = provider.LoadPackage(requestAsset);
                    var textureInfo = ExportTextureDds(requestPackage, requestOutput);
                    Console.WriteLine(JsonSerializer.Serialize(new { ok = true, action = "texture", assetPath = requestAsset, dds = textureInfo }));
                }
            }
            catch (Exception requestError)
            {
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    ok = false,
                    error = requestError.Message,
                    errorDetails = requestError.ToString()
                }));
            }
            Console.Out.Flush();
        }
        return 0;
    }

    var pathFilter = args.Length > 3 ? args[3] : null;
    var files = provider.Files.Keys
        .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
        .Where(path => string.IsNullOrWhiteSpace(pathFilter) || path.Contains(pathFilter, StringComparison.OrdinalIgnoreCase))
        .ToArray();
    var assetPath = args.Length > 4 ? args[4] : null;
    object? asset = null;
    if (!string.IsNullOrWhiteSpace(assetPath))
    {
        var rawOutputArg = args.Length > 7 ? args[7] : null;
        var rawOutput = string.IsNullOrWhiteSpace(rawOutputArg) ? null : Path.GetFullPath(rawOutputArg);
        if (!string.IsNullOrWhiteSpace(rawOutput))
        {
            if (!provider.Files.TryGetValue(assetPath, out var gameFile))
                throw new FileNotFoundException("Virtual asset path was not found.", assetPath);
            var rawBytes = gameFile.Read(null);
            File.WriteAllBytes(rawOutput, rawBytes);
            asset = new { path = assetPath, rawByteLength = rawBytes.Length, rawOutput };
        }
        else
        {
        var summaryOnly = args.Length > 8 && string.Equals(args[8], "summary", StringComparison.OrdinalIgnoreCase);
        var package = provider.LoadPackage(assetPath);
        var importNames = Enumerable.Range(0, package.ImportMapLength)
            .Select(index => package.ResolvePackageIndex(new FPackageIndex(package, -index - 1)))
            .Select(resolved => resolved?.GetPathName() ?? resolved?.Name.ToString())
            .ToArray();
        object? dds = null;
        if (package.GetExports().OfType<UTexture2D>().Any())
        {
            var ddsOutputArg = args.Length > 6 ? args[6] : null;
            var ddsOutput = string.IsNullOrWhiteSpace(ddsOutputArg) ? null : Path.GetFullPath(ddsOutputArg);
            dds = ExportTextureDds(package, ddsOutput);
        }
        asset = new
        {
            path = assetPath,
            packageName = package.Name,
            canDeserialize = package.CanDeserialize,
            exportCount = package.ExportMapLength,
            importNames,
            dds,
            exports = package.GetExports().Select(export => new
            {
                name = export.Name,
                type = export.ExportType,
                fullName = export.GetFullName(),
                properties = summaryOnly
                    ? export.Properties.Select(property => property.ToString()).ToArray()
                    : export.Properties.Select(property => ConvertValue(property)).ToArray()
            }).ToArray()
        };
        }
    }
    var result = new
    {
        gameDirectory,
        packageDirectory = pakDirectory,
        oodleDll = oodlePath,
        usmap = usmapPath,
        pathFilter,
        unrealVersion = "Final Fantasy VII Rebirth (CUE4Parse game profile)",
        mountResult = mountedCount,
        mountedVfsCount = provider.MountedVfs.Count,
        unloadedVfsCount = provider.UnloadedVfs.Count,
        mountedVfs = provider.MountedVfs.Select(v => new
        {
            type = v.GetType().FullName,
            encrypted = v.IsEncrypted,
            encryptedFileCount = v.EncryptedFileCount
        }).ToArray(),
        unloadedVfs = provider.UnloadedVfs.Select(v => new
        {
            type = v.GetType().FullName,
            encrypted = v.IsEncrypted,
            encryptedFileCount = v.EncryptedFileCount
        }).ToArray(),
        fileCount = files.Length,
        files,
        asset
    };
    var json = JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true });
    if (args.Length > 1 && args[1] != "-")
        File.WriteAllText(Path.GetFullPath(args[1]), json);
    else
        Console.WriteLine(json);
    return 0;
}
catch (Exception ex)
{
    Console.Error.WriteLine(ex.ToString());
    return 1;
}

/// <summary>
/// Rebirth's streamed skeletal render payload changes after the LOD bone tables.
/// This reader intentionally stops before that vertex payload: only the bind hierarchy,
/// RequiredBones, ActiveBoneIndices, and section BoneMaps are needed for rig analysis.
/// </summary>
sealed class RebirthSkeletalMeshUsageReader : UObject
{
    public FReferenceSkeleton ReferenceSkeleton { get; private set; } = null!;
    public List<FStaticLODModel> Lods { get; } = [];

    public override void Deserialize(FAssetArchive archive, long validPos)
    {
        base.Deserialize(archive, validPos);
        var hasVertexColors = GetOrDefault<bool>("bHasVertexColors");
        var vertexColorChannels = GetOrDefault<byte>("NumVertexColorChannels");
        var stripDataFlags = new FStripDataFlags(archive);
        _ = new CUE4Parse.UE4.Objects.Core.Math.FBoxSphereBounds(archive);
        _ = archive.ReadArray(() => new FSkeletalMaterial(archive));
        ReferenceSkeleton = new FReferenceSkeleton(archive);

        if (FSkeletalMeshCustomVersion.Get(archive) < FSkeletalMeshCustomVersion.Type.SplitModelAndRenderData)
        {
            var count = archive.Read<int>();
            for (var index = 0; index < count; index++)
                Lods.Add(new FStaticLODModel(archive, hasVertexColors));
            return;
        }

        if (!stripDataFlags.IsEditorDataStripped())
        {
            var editorLodCount = archive.Read<int>();
            for (var index = 0; index < editorLodCount; index++)
                _ = new FStaticLODModel(archive, hasVertexColors);
        }

        var isCooked = archive.ReadBoolean();
        if (!isCooked)
            return;

        // Rebirth retains UE4.26's desktop/mobile minimum-LOD setting.  It is
        // serialized between bCooked and the cooked LOD array.
        if (archive.Versions["SkeletalMesh.KeepMobileMinLODSettingOnDesktop"])
            _ = archive.Read<int>();

        var cookedLodCount = archive.Read<int>();
        if (cookedLodCount > 0)
            Lods.Add(ReadCookedLod(archive));

        // The remaining streamed LOD payload contains the changed vertex format,
        // including normal packing. LOD 0 provides the full mesh's used-bone set,
        // so deliberately stop before that payload.
        _ = vertexColorChannels;
    }

    private static FStaticLODModel ReadCookedLod(FAssetArchive archive)
    {
        var lod = new FStaticLODModel();
        var stripDataFlags = new FStripDataFlags(archive);
        var isCookedOut = archive.ReadBoolean();
        _ = archive.ReadBoolean(); // bInlined: the omitted payload begins after our data.
        lod.RequiredBones = archive.ReadArray<short>();
        if (isCookedOut)
            return lod;

        lod.Sections = archive.ReadArray(() =>
        {
            var section = new FSkelMeshSection();
            section.SerializeRenderItem(archive);
            return section;
        });
        lod.ActiveBoneIndices = archive.ReadArray<short>();
        _ = archive.Read<uint>(); // BuffersSize
        return lod;
    }
}
