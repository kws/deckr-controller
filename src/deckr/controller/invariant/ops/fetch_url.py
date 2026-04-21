"""deckr:fetch_image_url op - download image from URL or parse data URI."""

import base64
import urllib.parse

import httpx
from invariant_gfx.artifacts import BlobArtifact


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
    return BlobArtifact(data=raw, content_type=content_type)


def _fetch_http(url: str) -> BlobArtifact:
    with httpx.Client() as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/octet-stream")
        # Strip charset if present, e.g. "image/png; charset=utf-8"
        if ";" in content_type:
            content_type = content_type.split(";", 1)[0].strip()
        return BlobArtifact(data=response.content, content_type=content_type)
