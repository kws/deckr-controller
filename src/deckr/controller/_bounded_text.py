"""Controller-owned bounded text operations using public Pillow/font APIs.

Only metrics are computed while fitting. Pixel buffers are allocated after the
budget check; packed text draws directly onto its bounded canvas. The adapter
implements the extension's text policy without importing renderer internals.
"""

from decimal import Decimal

from invariant_gfx.artifacts import ImageArtifact
from justmytype import get_default_registry
from PIL import Image, ImageDraw, ImageFont

from deckr.controller._render_content import ContentFailure


def _font(family, size, weight, style):
    info = get_default_registry().find_font(family, weight=weight, style=style)
    if info is None:
        raise ContentFailure("invalid_content")
    return ImageFont.truetype(str(info.path), size=size)


def _bounds(text, font):
    # Public multiline metrics need a drawing context, but only a one-pixel image.
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return draw.multiline_textbbox((0, 0), text, font=font)


def _dimensions(size, limits, max_dimension):
    width, height = map(int, size)
    if (
        min(width, height) < 1
        or max(width, height) > max_dimension
        or width * height > limits.max_image_pixels
    ):
        raise ContentFailure("limit_exceeded")
    return width, height


def _validate(text, font, color, limits):
    if not isinstance(text, str) or font != "Inter":
        raise ContentFailure("invalid_content")
    if len(text) > limits.max_text_code_points:
        raise ContentFailure("limit_exceeded")
    if len(color) != 4 or any(
        not isinstance(c, int) or not 0 <= c <= 255 for c in color
    ):
        raise ContentFailure("invalid_content")


def render_text(
    *,
    text,
    font,
    color,
    limits,
    max_dimension,
    size=None,
    fit_width=None,
    weight=None,
    style="normal",
    exact=False,
):
    _validate(text, font, color, limits)
    if (size is None) == (fit_width is None):
        raise ContentFailure("invalid_content")
    if size is None:
        # Fitting policy is bounded to 500 points; no renderer-private search API.
        low, high, selected = 1, 500, 1
        while low <= high:
            mid = (low + high) // 2
            bounds = _bounds(text, _font(font, mid, weight, style))
            if bounds[2] - bounds[0] <= Decimal(str(fit_width)):
                selected, low = mid, mid + 1
            else:
                high = mid - 1
        size = selected
    size = int(size)
    if not 1 <= size <= 500:
        raise ContentFailure("limit_exceeded")
    face = _font(font, size, weight, style)
    left, top, right, bottom = _bounds(text, face)
    dimensions = _dimensions(
        (right - left + 4, bottom - top + 4), limits, max_dimension
    )
    canvas = Image.new("RGBA", dimensions)
    ImageDraw.Draw(canvas).multiline_text(
        (2 - left, 2 - top), text, font=face, fill=tuple(color)
    )
    return ImageArtifact(canvas)


def _lines(text, face, width):
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for character in paragraph:
            candidate = line + character
            bounds = face.getbbox(candidate)
            if line and bounds[2] - bounds[0] + 4 > width:
                lines.append(line.rstrip())
                line = character.lstrip()
            else:
                line = candidate
        lines.append(line)
    return lines


def packed_text(
    *,
    text,
    size,
    font,
    limits,
    max_dimension,
    color=(255, 255, 255, 255),
    min_font_size=10,
    max_font_size=None,
    line_gap=0,
    align_horizontal="center",
    align_vertical="center",
    weight=None,
    style="normal",
):
    _validate(text, font, color, limits)
    width, height = _dimensions(size, limits, max_dimension)
    minimum, maximum = (
        int(min_font_size),
        int(max_font_size or max(min(width, height), min_font_size)),
    )
    gap = int(line_gap)
    if not 1 <= minimum <= maximum <= max_dimension or not 0 <= gap <= max_dimension:
        raise ContentFailure("limit_exceeded")
    horizontal = {
        "start": 0,
        "left": 0,
        "s": 0,
        "center": 0.5,
        "c": 0.5,
        "end": 1,
        "right": 1,
        "e": 1,
    }
    vertical = {
        "top": 0,
        "start": 0,
        "s": 0,
        "center": 0.5,
        "c": 0.5,
        "bottom": 1,
        "end": 1,
        "e": 1,
    }
    if align_horizontal not in horizontal or align_vertical not in vertical:
        raise ContentFailure("invalid_content")
    low, high, selected = minimum, maximum, minimum
    while low <= high:
        mid = (low + high) // 2
        face = _font(font, mid, weight, style)
        lines = _lines(text, face, width)
        line_height = sum(face.getmetrics()) + 4
        fits = len(lines) * (line_height + gap) - gap <= height
        if fits:
            selected, low = mid, mid + 1
        else:
            high = mid - 1
    face = _font(font, selected, weight, style)
    line_height = sum(face.getmetrics()) + 4
    lines = _lines(text, face, width)
    count = max(1, (height + gap) // (line_height + gap))
    if len(lines) > count:
        lines = lines[:count]
        last = lines[-1]
        while last and face.getlength(last + "…") + 4 > width:
            last = last[:-1]
        lines[-1] = last + "…"
    canvas = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(canvas)
    block_height = len(lines) * (line_height + gap) - gap
    y = (height - block_height) * vertical[align_vertical]
    for line in lines:
        left, top, right, bottom = face.getbbox(line)
        x = (width - (right - left)) * horizontal[align_horizontal] - left
        draw.text((x, y + 2 - top), line, font=face, fill=tuple(color))
        y += line_height + gap
    return ImageArtifact(canvas)
