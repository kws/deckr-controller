"""Tests for controller invariant recipe helpers."""

from invariant.expressions import resolve_params

from deckr.controller.invariant.recipes import (
    STATUS_OVERLAY_STYLES,
    alert_overlay,
    feedback_overlay,
    icon_button,
    ok_overlay,
    status_overlay_style,
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


def test_status_overlays_use_one_circle_icon_style_table():
    assert {
        key: style.icon for key, style in STATUS_OVERLAY_STYLES.items()
    } == {
        "ok": "lucide:circle-check",
        "error": "lucide:circle-x",
        "unavailable": "lucide:circle-alert",
        "pending": "lucide:circle-ellipsis",
        "loading": "lucide:circle-dashed",
        "unknown": "lucide:circle-question-mark",
    }


def test_feedback_overlay_uses_status_style_definition():
    graph = feedback_overlay("pending")
    style = STATUS_OVERLAY_STYLES["pending"]

    assert graph.graph["icon_blob"].params["name"] == style.icon
    assert graph.graph["icon"].params["color"] == style.color
    assert graph.graph["label"].params["text"] == style.title


def test_unknown_feedback_overlay_falls_back_to_unknown_status_style():
    graph = feedback_overlay("surprise")
    style = status_overlay_style("unknown")

    assert graph.graph["icon_blob"].params["name"] == style.icon
    assert graph.graph["icon"].params["color"] == style.color
    assert graph.graph["label"].params["text"] == style.title


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
