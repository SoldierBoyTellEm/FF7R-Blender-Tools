using CUE4Parse.UE4.Assets.Exports.Animation;
using CUE4Parse.UE4.Assets.Objects;
using CUE4Parse.UE4.Assets.Readers;

namespace Ff7r.Rebirth;

/// <summary>
/// Rebirth's AnimSequence export, replacing CUE4Parse's <see cref="UAnimSequence"/>.
/// </summary>
/// <remarks>
/// Rebirth does not lay out the bytes after the tagged properties the way stock UE4.25+
/// does, so CUE4Parse reads a garbage FStripDataFlags, decides editor data is not stripped
/// and then throws on a bogus RawAnimationData count.  The loader swallows that, leaving
/// CompressedDataStructure null, which is what surfaces as
/// "Unsupported compressed data type " out of CUE4Parse-Conversion.
///
/// The properties themselves read correctly, and the animation is an ACL compressed clip
/// sitting in those trailing bytes, so this type keeps the property parsing and hands the
/// remainder to <see cref="Ff7r.Acl.AclClip"/> instead of guessing at the descriptor in
/// between.  See docs/REBIRTH_ANIMSEQUENCE_FORMAT.md.
///
/// Register it with <c>ObjectTypeRegistry.RegisterClass("AnimSequence", typeof(URebirthAnimSequence))</c>
/// before mounting.  It deliberately derives from <see cref="UAnimSequenceBase"/> rather
/// than <see cref="UAnimSequence"/> so none of CUE4Parse's compressed-data deserialization runs.
/// </remarks>
public class URebirthAnimSequence : UAnimSequenceBase
{
    public int NumFrames;

    /// <summary>Maps an animation track index to its index in the Skeleton's bone tree.</summary>
    /// <remarks>
    /// Read through FStructFallback: CUE4Parse's FTrackToSkeletonMap is a plain struct with
    /// no property-tag reader, so asking for FTrackToSkeletonMap[] yields default zeroes.
    /// </remarks>
    public int[] TrackToBoneIndex = [];

    /// <summary>Export bytes following the tagged properties: descriptor, ACL clip, curve block.</summary>
    public byte[] CompressedPayload = [];

    public override void Deserialize(FAssetArchive Ar, long validPos)
    {
        base.Deserialize(Ar, validPos);

        NumFrames = GetOrDefault<int>(nameof(NumFrames));
        var trackMap = GetOrDefault<FStructFallback[]>("TrackToSkeletonMapTable");
        TrackToBoneIndex = trackMap?
            .Select(entry => entry.GetOrDefault("BoneTreeIndex", 0))
            .ToArray() ?? [];

        var remaining = validPos - Ar.Position;
        CompressedPayload = remaining > 0 ? Ar.ReadBytes((int) remaining) : [];
        Ar.Position = validPos;
    }
}
