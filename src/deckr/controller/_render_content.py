"""Bounded baseline content decoding and logical-canvas rendering."""

from __future__ import annotations

import base64
import binascii
import io
import warnings

from deckr.actions.rendering import (
    RENDER_IMAGE_FORMAT,
    RENDER_TEXT_FORMAT,
    RenderContent,
    RenderImage,
    RenderLimits,
    RenderText,
)
from deckr.contracts.models import thaw_json
from PIL import Image, ImageDraw, ImageFont, ImageOps


class ContentFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def decode_image(data: RenderImage, limits: RenderLimits) -> Image.Image:
    if len(data.image) > ((limits.max_image_bytes + 2) // 3) * 4:
        raise ContentFailure("limit_exceeded")
    try:
        raw = base64.b64decode(data.image, validate=True)
        if base64.b64encode(raw).decode("ascii") != data.image:
            raise ContentFailure("invalid_content")
        if len(raw) > limits.max_image_bytes:
            raise ContentFailure("limit_exceeded")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != {"png": "PNG", "jpeg": "JPEG"}[data.encoding]:
                    raise ContentFailure("invalid_content")
                if source.width * source.height > limits.max_image_pixels:
                    raise ContentFailure("limit_exceeded")
                if getattr(source, "n_frames", 1) != 1 or getattr(
                    source, "is_animated", False
                ):
                    raise ContentFailure("invalid_content")
                # Verify structure before reopening and allocating the full pixel buffer.
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                return source.convert("RGBA")
    except ContentFailure:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ContentFailure("limit_exceeded") from exc
    except (ValueError, OSError, binascii.Error, SyntaxError) as exc:
        raise ContentFailure("invalid_content") from exc


def render_content(
    content: RenderContent, width: int, height: int, limits: RenderLimits
) -> bytes:
    """Return an opaque, unrotated PNG for the logical canvas."""
    if content.format == RENDER_IMAGE_FORMAT:
        data = RenderImage.from_wire(thaw_json(content.data))
        source = decode_image(data, limits)
        canvas = Image.new("RGBA", (width, height), data.background)
        if data.fit == "contain":
            fitted = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
        elif data.fit == "cover":
            fitted = ImageOps.fit(source, (width, height), Image.Resampling.LANCZOS)
        else:
            fitted = source.resize((width, height), Image.Resampling.LANCZOS)
        canvas.alpha_composite(
            fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2)
        )
    elif content.format == RENDER_TEXT_FORMAT:
        data = RenderText.from_wire(thaw_json(content.data))
        if len(data.text) > limits.max_text_code_points:
            raise ContentFailure("limit_exceeded")
        canvas = Image.new("RGBA", (width, height), data.background)
        if data.text:
            draw = ImageDraw.Draw(canvas)
            font = ImageFont.load_default(size=max(8, min(width, height) // 5))
            bounds = draw.multiline_textbbox(
                (0, 0), data.text, font=font, align="center"
            )
            position = (
                (width - (bounds[2] - bounds[0])) / 2 - bounds[0],
                (height - (bounds[3] - bounds[1])) / 2 - bounds[1],
            )
            draw.multiline_text(
                position, data.text, font=font, fill=data.foreground, align="center"
            )
    else:
        raise ContentFailure("unsupported_format")
    result = io.BytesIO()
    canvas.convert("RGB").save(result, format="PNG")
    return result.getvalue()
