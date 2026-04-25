"""Tests for render pipeline: resolve, _title_options_to_params, title_options flow."""

from unittest.mock import patch

import pytest
from deckr.hardware.events import HardwareImageFormat
from deckr.pluginhost.messages import TitleOptions

from deckr.controller._render import (
    RenderModel,
    RenderService,
    _font_style_to_weight_and_style,
    _hex_to_rgba,
    _parse_font_size,
    _title_options_to_params,
    resolve,
)
from deckr.controller._state_store import (
    ControlStateStore,
    RenderContent,
    TransientOverlay,
)

# --- _hex_to_rgba ---


def test_hex_to_rgba_white():
    assert _hex_to_rgba("#FFFFFF") == (255, 255, 255, 255)


def test_hex_to_rgba_black():
    assert _hex_to_rgba("#000000") == (0, 0, 0, 255)


def test_hex_to_rgba_short_form():
    assert _hex_to_rgba("#FFF") == (255, 255, 255, 255)


def test_hex_to_rgba_green():
    assert _hex_to_rgba("#00FF00") == (0, 255, 0, 255)


def test_hex_to_rgba_invalid_returns_white():
    assert _hex_to_rgba("nothex") == (255, 255, 255, 255)
    assert _hex_to_rgba("#GGGGGG") == (255, 255, 255, 255)


# --- _font_style_to_weight_and_style ---


def test_font_style_regular():
    assert _font_style_to_weight_and_style("Regular") == (400, "normal")
    assert _font_style_to_weight_and_style("") == (400, "normal")
    assert _font_style_to_weight_and_style(None) == (400, "normal")


def test_font_style_bold():
    assert _font_style_to_weight_and_style("Bold") == (700, "normal")


def test_font_style_italic():
    assert _font_style_to_weight_and_style("Italic") == (None, "italic")


def test_font_style_bold_italic():
    assert _font_style_to_weight_and_style("Bold Italic") == (700, "italic")


# --- _title_options_to_params ---


def test_title_options_to_params_none_uses_defaults():
    fmt = HardwareImageFormat(width=72, height=72)
    params = _title_options_to_params(None, fmt)
    assert params["font"] == "Inter"
    assert params["font_size"] == '${decimal("17") * canvas.width / 72}'  # 1.25rem
    assert params["needs_canvas"] is True
    assert params["color"] == (255, 255, 255, 255)
    assert params["title_alignment"] is None
    assert params["weight"] is None
    assert params["style"] == "normal"


def test_title_options_to_params_applies_options():
    fmt = HardwareImageFormat(width=72, height=72)
    opts = TitleOptions(
        font_family="Roboto Mono",
        font_size=24,
        font_style="Bold",
        title_color="#00FF00",
        title_alignment="top",
    )
    params = _title_options_to_params(opts, fmt)
    assert params["font"] == "Roboto Mono"
    assert params["font_size"] == 24
    assert params["color"] == (0, 255, 0, 255)
    assert params["title_alignment"] == "top"
    assert params["weight"] == 700
    assert params["style"] == "normal"


def test_title_options_to_params_passes_font_size_through():
    """font_size is passed through as-is; no clamping."""
    fmt = HardwareImageFormat(width=72, height=72)
    opts = TitleOptions(font_size=100)
    params = _title_options_to_params(opts, fmt)
    assert params["font_size"] == 100

    opts_small = TitleOptions(font_size=5)
    params_small = _title_options_to_params(opts_small, fmt)
    assert params_small["font_size"] == 5


def test_font_size_px_string():
    """font_size='14px' parses to size 14 pixels."""
    fmt = HardwareImageFormat(width=72, height=72)
    opts = TitleOptions(font_size="14px")
    params = _title_options_to_params(opts, fmt)
    assert params["font_size"] == 14
    assert params["fit_width"] is None
    assert params["needs_canvas"] is False


def test_font_size_rem_string():
    """font_size='1rem' yields CEL size and needs_canvas."""
    fmt = HardwareImageFormat(width=72, height=72)
    opts = TitleOptions(font_size="1rem")
    params = _title_options_to_params(opts, fmt)
    assert params["font_size"] == '${decimal("14") * canvas.width / 72}'
    assert params["fit_width"] is None
    assert params["needs_canvas"] is True


def test_font_size_vw_string():
    """font_size='100vw' yields fit_width; '80vw' yields 0.8 * canvas.width."""
    fmt = HardwareImageFormat(width=72, height=72)
    opts_100 = TitleOptions(font_size="100vw")
    params_100 = _title_options_to_params(opts_100, fmt)
    assert params_100["font_size"] is None
    assert params_100["fit_width"] == "${canvas.width}"
    assert params_100["needs_canvas"] is True

    opts_80 = TitleOptions(font_size="80vw")
    params_80 = _title_options_to_params(opts_80, fmt)
    assert params_80["font_size"] is None
    assert params_80["fit_width"] == '${decimal("0.8") * canvas.width}'
    assert params_80["needs_canvas"] is True


def test_parse_font_size_invalid_raises():
    """Invalid font_size string raises ValueError."""
    with pytest.raises(ValueError, match="font_size must be int"):
        _parse_font_size("10pt")
    with pytest.raises(ValueError, match="font_size must be int"):
        _parse_font_size("abc")


# --- resolve: title_options flow ---


def test_resolve_title_options_from_override():
    """When current content has title_options, it flows to RenderModel."""
    store = ControlStateStore(context_id="dev.0,0")
    opts = TitleOptions(font_family="Roboto", font_size=20)
    store.content = RenderContent(title="Hello", title_options=opts)

    model = resolve(store)
    assert model.title == "Hello"
    assert model.title_options is opts


def test_resolve_title_options_fallback_to_store():
    """When content has a title but no title_options, use store defaults."""
    store = ControlStateStore(context_id="dev.0,0")
    store.default_title_options = TitleOptions(
        font_family="Inter",
        title_color="#FF0000",
    )
    store.content = RenderContent(title="Hello")

    model = resolve(store)
    assert model.title == "Hello"
    assert model.title_options is store.default_title_options
    assert model.title_options.font_family == "Inter"
    assert model.title_options.title_color == "#FF0000"


def test_resolve_title_options_override_takes_precedence():
    """Explicit content title_options take precedence over store defaults."""
    store = ControlStateStore(context_id="dev.0,0")
    store.default_title_options = TitleOptions(font_family="StoreFont")
    override_opts = TitleOptions(font_family="OverrideFont")
    store.content = RenderContent(title="Hi", title_options=override_opts)

    model = resolve(store)
    assert model.title_options.font_family == "OverrideFont"


def test_resolve_overlay_ignores_title_options():
    """Overlay (alert/ok) returns overlay_type, no title."""
    store = ControlStateStore(context_id="dev.0,0")
    store.overlay = TransientOverlay(type="alert", expires_at=999999.0)

    model = resolve(store, now=0.0)
    assert model.overlay_type == "alert"
    assert model.title is None
    assert model.title_options is None


# --- RenderService.encode: isolate failures ---


@pytest.mark.asyncio
async def test_render_service_encode_graph_failure_returns_none():
    """Encoding errors must not propagate; callers skip device write."""
    fmt = HardwareImageFormat(width=72, height=72)
    model = RenderModel(title="Hi", title_options=TitleOptions(font_size=12))
    with patch(
        "deckr.controller._render._graph_to_jpeg_bytes",
        side_effect=ValueError("size must be positive, got 0"),
    ):
        out = await RenderService().encode(model, fmt)
    assert out is None
