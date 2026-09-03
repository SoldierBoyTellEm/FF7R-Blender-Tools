using System.Buffers.Binary;
using System.Numerics;

namespace Ff7r.Acl;

/// <summary>
/// Decompresses whole poses out of an ACL 1.x uniformly-sampled clip.
/// </summary>
/// <remarks>
/// A scalar port of the parts of nfrechette/acl v1.3.5 (MIT) that Rebirth actually uses:
/// QuatDropW_Variable rotations and Vector3_Variable translations/scales, with clip and
/// segment range reduction.  Anything else is rejected by the constructor rather than
/// silently mis-decoded.
///
/// The reference decoder interpolates between two key frames.  Rebirth clips are uniformly
/// sampled and the bridge exports every frame, so this only ever samples one key frame
/// exactly: the interpolation collapses to the identity and the per-key-frame state in
/// acl's SamplingContext collapses to a single set of offsets.
/// </remarks>
public sealed class AclDecompressor
{
    /// <summary>Number of bits each variable bit rate index encodes, per component.</summary>
    private static readonly byte[] BitRateNumBits =
        [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 32];

    private const byte HighestBitRate = 18;     // index of the 32 bit raw entry above
    private const int RotationComponents = 3;   // W is always dropped in the formats we accept
    private const int SegmentRangeBytesPerComponent = 1;

    private readonly AclClip _clip;

    public AclDecompressor(AclClip clip)
    {
        _clip = clip;

        if (clip.Version != AclClip.UniformlySampledVersion || clip.AlgorithmType != AclClip.AlgorithmUniformlySampled)
            throw new NotSupportedException(
                $"ACL clip uses algorithm {clip.AlgorithmType} version {clip.Version}; " +
                $"only uniformly sampled version {AclClip.UniformlySampledVersion} is supported.");
        if (clip.RotationFormat != AclClip.RotationQuatDropWVariable)
            throw new NotSupportedException($"ACL rotation format {clip.RotationFormat} is not supported (expected QuatDropW_Variable).");
        if (clip.TranslationFormat != AclClip.Vector3Variable)
            throw new NotSupportedException($"ACL translation format {clip.TranslationFormat} is not supported (expected Vector3_Variable).");
        if (clip.HasScale && clip.ScaleFormat != AclClip.Vector3Variable)
            throw new NotSupportedException($"ACL scale format {clip.ScaleFormat} is not supported (expected Vector3_Variable).");
        if (clip.NumSamples == 0 || clip.NumBones == 0)
            throw new NotSupportedException("ACL clip has no samples or no bones.");
    }

    public int NumBones => _clip.NumBones;
    public int NumSamples => (int) _clip.NumSamples;
    public float SampleRate => _clip.SampleRate;

    /// <summary>Decompresses one whole pose. Output arrays must hold <see cref="NumBones"/> entries.</summary>
    public void DecompressPose(int sampleIndex, Quaternion[] rotations, Vector3[] translations, Vector3[] scales)
    {
        ArgumentOutOfRangeException.ThrowIfNegative(sampleIndex);
        ArgumentOutOfRangeException.ThrowIfGreaterThanOrEqual(sampleIndex, NumSamples);

        var data = _clip.Data;
        var segmentIndex = FindSegment(sampleIndex);
        var (poseBitSize, formatPerTrackData, segmentRangeData, trackData) = _clip.Segment(segmentIndex);
        var segmentSampleIndex = sampleIndex - (int) _clip.SegmentStartIndex(segmentIndex);

        var state = new PoseState
        {
            KeyFrameBitOffset = (uint) segmentSampleIndex * poseBitSize,
        };

        var defaultScale = _clip.DefaultScaleIsOne ? Vector3.One : Vector3.Zero;

        for (var bone = 0; bone < _clip.NumBones; bone++)
        {
            rotations[bone] = DecompressRotation(
                data, ref state, formatPerTrackData, segmentRangeData, trackData,
                (_clip.ClipRangeReduction & AclClip.RangeReduceRotations) != 0,
                (_clip.SegmentRangeReduction & AclClip.RangeReduceRotations) != 0);

            translations[bone] = DecompressVector(
                data, ref state, formatPerTrackData, segmentRangeData, trackData,
                Vector3.Zero,
                (_clip.ClipRangeReduction & AclClip.RangeReduceTranslations) != 0,
                (_clip.SegmentRangeReduction & AclClip.RangeReduceTranslations) != 0);

            scales[bone] = _clip.HasScale
                ? DecompressVector(
                    data, ref state, formatPerTrackData, segmentRangeData, trackData,
                    defaultScale,
                    (_clip.ClipRangeReduction & AclClip.RangeReduceScales) != 0,
                    (_clip.SegmentRangeReduction & AclClip.RangeReduceScales) != 0)
                : defaultScale;
        }
    }

    private int FindSegment(int sampleIndex)
    {
        if (_clip.NumSegments <= 1)
            return 0;

        // Segment start indices ascend; take the last segment starting at or before our
        // sample.  acl's decoder guesses an index first, which is only a speed trick.
        var found = 0;
        for (var segment = 1; segment < _clip.NumSegments; segment++)
        {
            if (_clip.SegmentStartIndex(segment) > (uint) sampleIndex)
                break;
            found = segment;
        }
        return found;
    }

    private struct PoseState
    {
        public int TrackIndex;
        public int ConstantTrackDataOffset;
        public int ClipRangeDataOffset;
        public int FormatPerTrackDataOffset;
        public int SegmentRangeDataOffset;
        public uint KeyFrameBitOffset;
    }

    private Quaternion DecompressRotation(
        ReadOnlySpan<byte> data, ref PoseState state,
        int formatPerTrackData, int segmentRangeData, int trackData,
        bool clipNormalized, bool segmentNormalized)
    {
        var result = Quaternion.Identity;

        if (!BitsetTest(data, _clip.DefaultTracksBitset, state.TrackIndex))
        {
            if (BitsetTest(data, _clip.ConstantTracksBitset, state.TrackIndex))
            {
                // Constant tracks keep the highest precision of their variant, QuatDropW_96.
                result = QuatFromPositiveW(UnpackVector3_96(data, _clip.ConstantTrackData + state.ConstantTrackDataOffset));
                state.ConstantTrackDataOffset += 3 * sizeof(float);
            }
            else
            {
                var bitRate = data[formatPerTrackData + state.FormatPerTrackDataOffset];
                var numBits = NumBitsAtBitRate(bitRate);

                Vector3 value;
                var skipSegmentRange = false;
                var skipClipRange = false;
                if (bitRate == 0)
                {
                    // Constant within this segment: the sample lives in the segment range slot.
                    value = UnpackVector3_u48(data, segmentRangeData + state.SegmentRangeDataOffset);
                    skipSegmentRange = true;
                }
                else if (bitRate == HighestBitRate)
                {
                    value = UnpackVector3_96(data, trackData, state.KeyFrameBitOffset);
                    skipSegmentRange = true;
                    skipClipRange = true;
                }
                else
                {
                    value = clipNormalized
                        ? UnpackVector3_uXX(numBits, data, trackData, state.KeyFrameBitOffset)
                        : UnpackVector3_sXX(numBits, data, trackData, state.KeyFrameBitOffset);
                }

                state.KeyFrameBitOffset += (uint) numBits * 3;
                state.FormatPerTrackDataOffset++;

                if (segmentNormalized)
                {
                    if (!skipSegmentRange)
                    {
                        var min = UnpackVector3_u24(data, segmentRangeData + state.SegmentRangeDataOffset);
                        var extent = UnpackVector3_u24(data, segmentRangeData + state.SegmentRangeDataOffset + RotationComponents);
                        value = value * extent + min;
                    }
                    state.SegmentRangeDataOffset += RotationComponents * SegmentRangeBytesPerComponent * 2;
                }

                if (clipNormalized)
                {
                    if (!skipClipRange)
                    {
                        var min = UnpackVector3_96(data, _clip.ClipRangeData + state.ClipRangeDataOffset);
                        var extent = UnpackVector3_96(data, _clip.ClipRangeData + state.ClipRangeDataOffset + RotationComponents * sizeof(float));
                        value = value * extent + min;
                    }
                    state.ClipRangeDataOffset += RotationComponents * sizeof(float) * 2;
                }

                result = QuatFromPositiveW(value);
            }
        }

        state.TrackIndex++;
        return result;
    }

    private Vector3 DecompressVector(
        ReadOnlySpan<byte> data, ref PoseState state,
        int formatPerTrackData, int segmentRangeData, int trackData,
        Vector3 defaultValue, bool clipNormalized, bool segmentNormalized)
    {
        var result = defaultValue;

        if (!BitsetTest(data, _clip.DefaultTracksBitset, state.TrackIndex))
        {
            if (BitsetTest(data, _clip.ConstantTracksBitset, state.TrackIndex))
            {
                // Constant Vector3 tracks keep full precision.
                result = UnpackVector3_96(data, _clip.ConstantTrackData + state.ConstantTrackDataOffset);
                state.ConstantTrackDataOffset += 3 * sizeof(float);
            }
            else
            {
                var bitRate = data[formatPerTrackData + state.FormatPerTrackDataOffset];
                var numBits = NumBitsAtBitRate(bitRate);

                Vector3 value;
                var skipSegmentRange = false;
                var skipClipRange = false;
                if (bitRate == 0)
                {
                    value = UnpackVector3_u48(data, segmentRangeData + state.SegmentRangeDataOffset);
                    skipSegmentRange = true;
                }
                else if (bitRate == HighestBitRate)
                {
                    value = UnpackVector3_96(data, trackData, state.KeyFrameBitOffset);
                    skipSegmentRange = true;
                    skipClipRange = true;
                }
                else
                {
                    value = UnpackVector3_uXX(numBits, data, trackData, state.KeyFrameBitOffset);
                }

                state.KeyFrameBitOffset += (uint) numBits * 3;
                state.FormatPerTrackDataOffset++;

                if (segmentNormalized)
                {
                    if (!skipSegmentRange)
                    {
                        var min = UnpackVector3_u24(data, segmentRangeData + state.SegmentRangeDataOffset);
                        var extent = UnpackVector3_u24(data, segmentRangeData + state.SegmentRangeDataOffset + 3);
                        value = value * extent + min;
                    }
                    state.SegmentRangeDataOffset += 3 * SegmentRangeBytesPerComponent * 2;
                }

                if (clipNormalized)
                {
                    if (!skipClipRange)
                    {
                        var min = UnpackVector3_96(data, _clip.ClipRangeData + state.ClipRangeDataOffset);
                        var extent = UnpackVector3_96(data, _clip.ClipRangeData + state.ClipRangeDataOffset + 3 * sizeof(float));
                        value = value * extent + min;
                    }
                    state.ClipRangeDataOffset += 3 * sizeof(float) * 2;
                }

                result = value;
            }
        }

        state.TrackIndex++;
        return result;
    }

    private static int NumBitsAtBitRate(byte bitRate)
    {
        if (bitRate >= BitRateNumBits.Length)
            throw new InvalidDataException($"ACL clip has invalid bit rate {bitRate}.");
        return BitRateNumBits[bitRate];
    }

    private static bool BitsetTest(ReadOnlySpan<byte> data, int bitsetOffset, int bitIndex)
    {
        if (bitsetOffset < 0) return false;
        var word = BinaryPrimitives.ReadUInt32LittleEndian(data[(bitsetOffset + bitIndex / 32 * 4)..]);
        return (word & (1U << (31 - bitIndex % 32))) != 0;
    }

    private static Quaternion QuatFromPositiveW(Vector3 xyz)
    {
        // Kept in acl's operation order: it is more accurate than 1 - dot(xyz, xyz).
        var wSquared = ((1.0f - xyz.X * xyz.X) - xyz.Y * xyz.Y) - xyz.Z * xyz.Z;
        return new Quaternion(xyz, MathF.Sqrt(MathF.Abs(wSquared)));
    }

    private static Vector3 UnpackVector3_96(ReadOnlySpan<byte> data, int at) => new(
        BinaryPrimitives.ReadSingleLittleEndian(data[at..]),
        BinaryPrimitives.ReadSingleLittleEndian(data[(at + 4)..]),
        BinaryPrimitives.ReadSingleLittleEndian(data[(at + 8)..]));

    private static Vector3 UnpackVector3_u48(ReadOnlySpan<byte> data, int at) => new(
        BinaryPrimitives.ReadUInt16LittleEndian(data[at..]) / 65535.0f,
        BinaryPrimitives.ReadUInt16LittleEndian(data[(at + 2)..]) / 65535.0f,
        BinaryPrimitives.ReadUInt16LittleEndian(data[(at + 4)..]) / 65535.0f);

    private static Vector3 UnpackVector3_u24(ReadOnlySpan<byte> data, int at) =>
        new(data[at] / 255.0f, data[at + 1] / 255.0f, data[at + 2] / 255.0f);

    /// <summary>Three raw float32 components packed at an arbitrary bit offset, big-endian.</summary>
    private static Vector3 UnpackVector3_96(ReadOnlySpan<byte> data, int baseOffset, uint bitOffset)
    {
        var byteOffset = baseOffset + (int) (bitOffset / 8);
        var shift = (int) (bitOffset % 8);
        return new Vector3(
            BitConverter.UInt32BitsToSingle(ReadShiftedU32(data, byteOffset + 0, shift)),
            BitConverter.UInt32BitsToSingle(ReadShiftedU32(data, byteOffset + 4, shift)),
            BitConverter.UInt32BitsToSingle(ReadShiftedU32(data, byteOffset + 8, shift)));
    }

    private static uint ReadShiftedU32(ReadOnlySpan<byte> data, int at, int shift) =>
        (uint) ((BinaryPrimitives.ReadUInt64BigEndian(data[at..]) << shift) >> 32);

    /// <summary>Three unsigned components of <paramref name="numBits"/> bits each, big-endian.</summary>
    private static Vector3 UnpackVector3_uXX(int numBits, ReadOnlySpan<byte> data, int baseOffset, uint bitOffset)
    {
        var mask = (1U << numBits) - 1;
        var inverseMax = 1.0f / mask;
        return new Vector3(
            (ReadBitsBigEndian(data, baseOffset, bitOffset, numBits) & mask) * inverseMax,
            (ReadBitsBigEndian(data, baseOffset, bitOffset + (uint) numBits, numBits) & mask) * inverseMax,
            (ReadBitsBigEndian(data, baseOffset, bitOffset + (uint) numBits * 2, numBits) & mask) * inverseMax);
    }

    private static Vector3 UnpackVector3_sXX(int numBits, ReadOnlySpan<byte> data, int baseOffset, uint bitOffset)
    {
        var unsigned = UnpackVector3_uXX(numBits, data, baseOffset, bitOffset);
        return unsigned * 2.0f - Vector3.One;
    }

    private static uint ReadBitsBigEndian(ReadOnlySpan<byte> data, int baseOffset, uint bitOffset, int numBits)
    {
        var at = baseOffset + (int) (bitOffset / 8);
        var value = BinaryPrimitives.ReadUInt32BigEndian(data[at..]);
        return value >> (32 - numBits - (int) (bitOffset % 8));
    }
}
