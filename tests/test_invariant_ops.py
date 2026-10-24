from __future__ import annotations

import base64
import io

import httpx
import pytest
from invariant_gfx.artifacts import ImageArtifact
from PIL import Image

from deckr.controller.invariant.ops import fetch_url
from deckr.controller.invariant.ops.encode_jpeg import encode_jpeg


@pytest.mark.parametrize("failure", ["bytes", "deadline"])
def test_http_artwork_stream_is_closed_before_exhausting_resources(
    monkeypatch, failure
):
    received = []
    closed = []
    clock = [0.0]

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                received.append(index)
                clock[0] += 0.6 if failure == "deadline" else 0.01
                yield b"x" * (128 * 1024)

        def close(self):
            closed.append(True)

    factory = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, stream=Stream(), headers={"content-type": "image/png"}
        )
    )
    monkeypatch.setattr(fetch_url.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        fetch_url.httpx,
        "Client",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    with pytest.raises((ValueError, httpx.ReadTimeout), match="budget|deadline"):
        fetch_url.fetch_image_url("https://artwork.test/unbounded")
    assert len(received) < 10
    assert closed == [True]


def test_inline_artwork_cannot_allocate_oversized_decoded_pixels(monkeypatch):
    png = io.BytesIO()
    Image.new("L", (2049, 2048)).save(png, format="PNG")
    payload = base64.b64encode(png.getvalue()).decode()
    assert len(png.getvalue()) < fetch_url.MAX_IMAGE_BYTES

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("Pixels must not be decoded before the dimension check")

    monkeypatch.setattr(Image.Image, "load", unexpected_decode)
    with pytest.raises(ValueError, match="pixel budget"):
        fetch_url.fetch_image_url(f"data:image/png;base64,{payload}")


def test_inline_artwork_has_a_decoded_byte_budget():
    with pytest.raises(ValueError, match="byte budget"):
        fetch_url.fetch_image_url(
            "data:image/svg+xml;utf8," + "x" * (fetch_url.MAX_IMAGE_BYTES + 1)
        )


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

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self):
            yield self.content

    class _Client:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def stream(self, method, url: str):
            assert method == "GET"
            assert url == "https://example.test/image.webp"
            return _Response()

    monkeypatch.setattr(fetch_url.httpx, "Client", _Client)

    blob = fetch_url.fetch_image_url("https://example.test/image.webp")

    assert blob.content_type == "image/webp"
    assert blob.data == b"webp-bytes"
