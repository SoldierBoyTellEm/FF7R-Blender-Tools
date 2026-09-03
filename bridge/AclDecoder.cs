using System.Buffers.Binary;
using System.Numerics;

namespace Ff7r.Acl;

/// <summary>
/// Reader for an ACL 1.x <c>CompressedClip</c> buffer as Rebirth stores it inside an
/// AnimSequence export.  Only the header is parsed here; <see cref="AclDecoder"/>
/// does the sample decompression.
/// </summary>
/// <remarks>
/// Ported from nfrechette/acl v1.3.5 (MIT).  Every offset inside the clip is relative
/// to the ClipHeader, which starts immediately after the 16 byte CompressedClip header.
/// </remarks>
public sealed class AclClip
{
    public const uint CompressedClipTag = 0xAC10AC10;

    /// <summary>Serialization version of <c>AlgorithmType8::UniformlySampled</c> in ACL 1.3.</summary>
    public const ushort UniformlySampledVersion = 5;

    public const byte AlgorithmUniformlySampled = 0;

    public const byte RotationQuat128 = 0;
    public const byte RotationQuatDropW96 = 1;
    public const byte RotationQuatDropW48 = 2;
    public const byte RotationQuatDropW32 = 3;
    public const byte RotationQuatDropWVariable = 4;

    public const byte Vector396 = 0;
    public const byte Vector348 = 1;
    public const byte Vector332 = 2;
    public const byte Vector3Variable = 3;

    public const byte RangeReduceRotations = 0x01;
    public const byte RangeReduceTranslations = 0x02;
    public const byte RangeReduceScales = 0x04;

    private const int ClipHeaderOffset = 16;
    private const ushort InvalidOffset16 = 0xFFFF;
    private const uint InvalidOffset32 = 0xFFFFFFFF;

    /// <summary>
    /// The clip bytes, copied out of the export and zero padded.  ACL's "unsafe" unpack
    /// helpers read up to 16 bytes past the value they decode, so the padding is required.
    /// </summary>
    private readonly byte[] _data;

    private AclClip(byte[] data)
    {
        _data = data;

        Size = BinaryPrimitives.ReadUInt32LittleEndian(data);
        Hash = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(4));
        Version = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(12));
        AlgorithmType = data[14];

        var h = data.AsSpan(ClipHeaderOffset);
        NumBones = BinaryPrimitives.ReadUInt16LittleEndian(h);
        NumSegments = BinaryPrimitives.ReadUInt16LittleEndian(h[2..]);
        RotationFormat = h[4];
        TranslationFormat = h[5];
        ScaleFormat = h[6];
        ClipRangeReduction = h[7];
        SegmentRangeReduction = h[8];
        HasScale = h[9] != 0;
        DefaultScaleIsOne = h[10] != 0;
        NumSamples = BinaryPrimitives.ReadUInt32LittleEndian(h[12..]);
        SampleRate = BinaryPrimitives.ReadSingleLittleEndian(h[16..]);
        _segmentStartIndicesOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[20..]);
        _segmentHeadersOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[22..]);
        _defaultTracksBitsetOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[24..]);
        _constantTracksBitsetOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[26..]);
        _constantTrackDataOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[28..]);
        _clipRangeDataOffset = BinaryPrimitives.ReadUInt16LittleEndian(h[30..]);
    }

    public uint Size { get; }
    public uint Hash { get; }
    public ushort Version { get; }
    public byte AlgorithmType { get; }
    public ushort NumBones { get; }
    public ushort NumSegments { get; }
    public byte RotationFormat { get; }
    public byte TranslationFormat { get; }
    public byte ScaleFormat { get; }
    public byte ClipRangeReduction { get; }
    public byte SegmentRangeReduction { get; }
    public bool HasScale { get; }
    public bool DefaultScaleIsOne { get; }
    public uint NumSamples { get; }
    public float SampleRate { get; }

    private readonly ushort _segmentStartIndicesOffset;
    private readonly ushort _segmentHeadersOffset;
    private readonly ushort _defaultTracksBitsetOffset;
    private readonly ushort _constantTracksBitsetOffset;
    private readonly ushort _constantTrackDataOffset;
    private readonly ushort _clipRangeDataOffset;

    internal ReadOnlySpan<byte> Data => _data;

    /// <summary>Absolute index into <see cref="Data"/> for a ClipHeader-relative offset.</summary>
    private static int Resolve(ushort offset) =>
        offset == InvalidOffset16 ? -1 : ClipHeaderOffset + offset;

    private static int Resolve(uint offset) =>
        offset == InvalidOffset32 ? -1 : ClipHeaderOffset + (int) offset;

    internal int DefaultTracksBitset => Resolve(_defaultTracksBitsetOffset);
    internal int ConstantTracksBitset => Resolve(_constantTracksBitsetOffset);
    internal int ConstantTrackData => Resolve(_constantTrackDataOffset);
    internal int ClipRangeData => Resolve(_clipRangeDataOffset);

    internal uint SegmentStartIndex(int segmentIndex)
    {
        var at = Resolve(_segmentStartIndicesOffset);
        if (at < 0) return 0;
        return BinaryPrimitives.ReadUInt32LittleEndian(_data.AsSpan(at + segmentIndex * 4));
    }

    /// <summary>A segment's animated pose bit size plus the three ClipHeader-relative data offsets.</summary>
    internal (uint PoseBitSize, int FormatPerTrackData, int RangeData, int TrackData) Segment(int segmentIndex)
    {
        var at = Resolve(_segmentHeadersOffset) + segmentIndex * 16;
        var s = _data.AsSpan(at);
        return (
            BinaryPrimitives.ReadUInt32LittleEndian(s),
            Resolve(BinaryPrimitives.ReadUInt32LittleEndian(s[4..])),
            Resolve(BinaryPrimitives.ReadUInt32LittleEndian(s[8..])),
            Resolve(BinaryPrimitives.ReadUInt32LittleEndian(s[12..])));
    }

    /// <summary>
    /// Finds the single ACL clip inside an AnimSequence export payload.  Rebirth writes it
    /// 16 byte aligned behind a descriptor whose trailing padding is uninitialised cooker
    /// memory, so the tag is the only dependable anchor.
    /// </summary>
    public static AclClip? Find(ReadOnlySpan<byte> payload, out string? error)
    {
        error = null;
        var reasons = new List<string>();
        for (var at = 0; at + 16 <= payload.Length; at++)
        {
            if (BinaryPrimitives.ReadUInt32LittleEndian(payload[(at + 8)..]) != CompressedClipTag)
                continue;

            var size = BinaryPrimitives.ReadUInt32LittleEndian(payload[at..]);
            if (size < 32 || at + size > payload.Length)
            {
                reasons.Add($"tag at {at} has out of range size {size}");
                continue;
            }

            var padded = new byte[size + 64];
            payload.Slice(at, (int) size).CopyTo(padded);

            var expected = BinaryPrimitives.ReadUInt32LittleEndian(padded.AsSpan(4));
            var actual = Fnv1a32(padded.AsSpan(8, (int) size - 8));
            if (expected != actual)
            {
                reasons.Add($"tag at {at} failed its hash check (0x{expected:X8} != 0x{actual:X8})");
                continue;
            }

            return new AclClip(padded);
        }

        error = reasons.Count > 0
            ? "No usable ACL clip: " + string.Join("; ", reasons)
            : "No ACL compressed_clip tag (0xAC10AC10) in the AnimSequence export.";
        return null;
    }

    private static uint Fnv1a32(ReadOnlySpan<byte> data)
    {
        var hash = 2166136261U;
        foreach (var b in data)
            hash = (hash ^ b) * 16777619U;
        return hash;
    }
}
