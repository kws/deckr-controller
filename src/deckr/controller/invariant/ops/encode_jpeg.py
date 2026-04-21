"""deckr:encode_jpeg op - encode ImageArtifact to JPEG bytes."""

import io

from invariant_gfx.artifacts import BlobArtifact, ImageArtifact


def encode_jpeg(image: ImageArtifact, quality: int = 95) -> BlobArtifact:
    """Encode an ImageArtifact to JPEG bytes.

    Args:
        image: ImageArtifact to encode.
        quality: JPEG quality 1-100 (default 95).

    Returns:
        BlobArtifact with content_type "image/jpeg" containing the encoded bytes.
    """
    pil_image = image.image
    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")
    buf = io.BytesIO()
    pil_image.save(buf, format="JPEG", quality=quality)
    return BlobArtifact(data=buf.getvalue(), content_type="image/jpeg")
