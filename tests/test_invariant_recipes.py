"""Tests for controller invariant recipe helpers."""

from invariant.expressions import resolve_params

from deckr.controller.invariant.recipes import (
    STATUS_OVERLAY_STYLES,
    alert_overlay,
    icon_button,
    ok_overlay,
    title_card,
    unavailable_overlay,
)


def test_icon_button_title_size_nonzero_when_canvas_wider_than_72():
    """Regression: int division 72/canvas.width was 0 for width>72, yielding size 0."""
    graph = icon_button(title="Garage", title_size=15)
    size_expr = graph.graph["label"].params["size"]
    resolved = resolve_params(
        {"size": size_expr},
        {"canvas": {"width": 96, "height": 96}},
    )
    assert resolved["size"] > 0


def test_legacy_status_overlay_helpers_delegate_to_status_styles():
    assert (
        alert_overlay().graph["icon_blob"].params["name"]
        == STATUS_OVERLAY_STYLES["unknown"].icon
    )
    assert (
        ok_overlay().graph["icon_blob"].params["name"]
        == STATUS_OVERLAY_STYLES["ok"].icon
    )
    assert (
        unavailable_overlay().graph["icon_blob"].params["name"]
        == STATUS_OVERLAY_STYLES["unavailable"].icon
    )


def test_title_card_top_and_bottom_alignment() -> None:
    top = title_card("Top", title_alignment="top")
    bottom = title_card("Bottom", title_alignment="bottom")

    assert top.graph["output"].params["layers"][1]["anchor"]["align"] == "cs@cs"
    assert bottom.graph["output"].params["layers"][1]["anchor"]["align"] == "ce@ce"


def test_title_card_fit_width_weight_and_style() -> None:
    graph = title_card(
        "Headline",
        fit_width="${canvas.width - 8}",
        weight=700,
        style="italic",
    )
    text = graph.graph["text"]

    assert text.params["fit_width"] == "${canvas.width - 8}"
    assert "size" not in text.params
    assert text.params["weight"] == 700
    assert text.params["style"] == "italic"
    assert text.deps == ["canvas"]
