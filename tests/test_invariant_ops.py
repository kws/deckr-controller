from __future__ import annotations

import base64

import pytest
from invariant_gfx.artifacts import ImageArtifact
from PIL import Image

from deckr.controller.invariant.ops import fetch_url
from deckr.controller.invariant.ops.encode_jpeg import encode_jpeg


def test_jpeg_encoding_converts_non_rgb_image_and_returns_jpeg_blob() -> None:
    image = ImageArtifact(Image.new("RGBA", (2, 2), (255, 0, 0, 128)))

    blob = encode_jpeg(image)

    assert blob.content_type == "image/jpeg"
    assert blob.data.startswith(b"\xff\xd8")


def test_fetch_image_url_handles_base64_data_uri() -> None:
    payload = base64.b64encode(b"png-bytes").decode("ascii")

    blob = fetch_url.fetch_image_url(f"data:image/png;base64,{payload}")

    assert blob.content_type == "image/png"
    assert blob.data == b"png-bytes"


def test_fetch_image_url_handles_url_encoded_data_uri() -> None:
    blob = fetch_url.fetch_image_url("data:image/svg+xml;utf8,%3Csvg%3E%3C/svg%3E")

    assert blob.content_type == "image/svg+xml"
    assert blob.data == b"<svg></svg>"


def test_fetch_image_url_rejects_unsupported_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        fetch_url.fetch_image_url("file:///tmp/image.png")


def test_fetch_image_url_strips_http_content_type_charset(monkeypatch) -> None:
    class _Response:
        headers = {"content-type": "image/webp; charset=utf-8"}
        content = b"webp-bytes"

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str):
            assert url == "https://example.test/image.webp"
            return _Response()

    monkeypatch.setattr(fetch_url.httpx, "Client", _Client)

    blob = fetch_url.fetch_image_url("https://example.test/image.webp")

    assert blob.content_type == "image/webp"
    assert blob.data == b"webp-bytes"
