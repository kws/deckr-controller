"""Tests for controller invariant recipe helpers."""

from invariant.expressions import resolve_params

from deckr.controller.invariant.recipes import icon_button


def test_icon_button_title_size_nonzero_when_canvas_wider_than_72():
    """Regression: int division 72/canvas.width was 0 for width>72, yielding size 0."""
    graph = icon_button(title="Garage", title_size=15)
    size_expr = graph.graph["label"].params["size"]
    resolved = resolve_params(
        {"size": size_expr},
        {"canvas": {"width": 96, "height": 96}},
    )
    assert resolved["size"] > 0
