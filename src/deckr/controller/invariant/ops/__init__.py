"""deckr-specific invariant ops."""

from deckr.controller.invariant.ops.encode_jpeg import encode_jpeg
from deckr.controller.invariant.ops.fetch_url import fetch_image_url

__all__ = ["fetch_image_url", "encode_jpeg"]
