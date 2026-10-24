"""deckr:fetch_image_url op - download image from URL or parse data URI."""

import base64
import io
import time
import urllib.parse

import httpx
from invariant_gfx.artifacts import BlobArtifact
from PIL import Image, UnidentifiedImageError

HTTP_IMAGE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 4 * 1024 * 1024
HTTP_IMAGE_SECONDS = 2.0


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
        return _fetch_http(url)
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


def _fetch_http(url: str) -> BlobArtifact:
    deadline = time.monotonic() + HTTP_IMAGE_SECONDS
    with (
        httpx.Client(timeout=HTTP_IMAGE_TIMEOUT, follow_redirects=True) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream")
        # Strip charset if present, e.g. "image/png; charset=utf-8"
        if ";" in content_type:
            content_type = content_type.split(";", 1)[0].strip()
        data = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout("Image download exceeded its total deadline")
            if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds byte budget")
            data.extend(chunk)
        return _bounded_blob(bytes(data), content_type)


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
