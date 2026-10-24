"""deckr:fetch_image_url op - consume acquired image bytes or parse a data URI."""

import base64
import io
import urllib.parse
from contextvars import ContextVar

from invariant_gfx.artifacts import BlobArtifact
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 4 * 1024 * 1024


IMAGE_RESOURCES = ContextVar("image_resources", default=None)


class ImageResourceRequired(Exception):
    """An active graph requires bytes from the controller's async resource stage."""


def fetch_image_url(url: str) -> BlobArtifact:
    """Fetch image bytes from a URL or data URI.

    Args:
        url: HTTP/HTTPS URL or data: URI (e.g. data:image/png;base64,...).

    Returns:
        BlobArtifact with the raw bytes and content_type.

    Raises:
        ValueError: If URL scheme is unsupported or data URI is malformed.
    """
    if url.startswith("data:"):
        return _parse_data_uri(url)
    if url.startswith(("http://", "https://")):
        resources = IMAGE_RESOURCES.get() or {}
        if url not in resources:
            raise ImageResourceRequired(url)
        blob = resources[url]
        return _bounded_blob(blob.data, blob.content_type)
    raise ValueError(f"Unsupported URL scheme: {url[:20]}...")


def _parse_data_uri(url: str) -> BlobArtifact:
    if len(url) > MAX_IMAGE_BYTES * 3 + 1024:
        raise ValueError("Image exceeds byte budget")
    header, _, data = url.partition(",")
    if not data:
        raise ValueError("Invalid data URI: no comma")
    # header is e.g. "data:image/png;base64"
    parts = header.split(";")
    if len(parts) < 2:
        raise ValueError("Invalid data URI: missing media type or encoding")
    content_type = parts[0].removeprefix("data:").strip() or "application/octet-stream"
    if "base64" in parts[1].lower():
        raw = base64.b64decode(data)
    else:
        raw = urllib.parse.unquote_to_bytes(data)
    return _bounded_blob(raw, content_type)


def _bounded_blob(data, content_type):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds byte budget")
    try:
        with Image.open(io.BytesIO(data)) as source:
            # Inspect headers before gfx's downstream decoder allocates pixels.
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image exceeds pixel budget")
    except UnidentifiedImageError:
        # This operation also transports SVG; its renderer validates that format.
        pass
    return BlobArtifact(data=data, content_type=content_type)
