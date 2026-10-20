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
    return encode_raster(image, quality=quality, format="JPEG")


def encode_raster(
    image: ImageArtifact, quality: int = 95, format: str = "JPEG"
) -> BlobArtifact:
    if format not in {"JPEG", "PNG"}:
        raise ValueError(f"Unsupported raster encoding: {format}")
    pil_image = image.image
    if format == "JPEG" and pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")
    buf = io.BytesIO()
    pil_image.save(buf, format=format, quality=quality)
    return BlobArtifact(data=buf.getvalue(), content_type=f"image/{format.lower()}")
