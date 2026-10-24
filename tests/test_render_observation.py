from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from deckr.controller._raster import RasterFormat
from deckr.controller._render_observation import (
    ObservingRenderBackend,
    RenderObservationOptions,
)
from test_support.render_dispatch import (
    ImmediateBackend,
    _read_observations,
    _request_with_graph,
    _solid_request,
)


@pytest.mark.asyncio
async def test_observing_render_backend_records_error(tmp_path: Path):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(frame=None, error="boom"),
        controller_id="controller-main",
        options=RenderObservationOptions(path=path),
    )

    request = _request_with_graph({"output": "value"})
    request = replace(
        request, source=replace(request.source, content={"text": "private content"})
    )
    result = await backend.render(request)

    assert result.frame is None
    assert result.error == "boom"
    record = _read_observations(path)[0]
    assert record["frameSha256"] is None
    assert record["error"] == "boom"
    assert "graph" not in record
    assert "content" not in record


@pytest.mark.asyncio
async def test_observing_render_backend_can_include_graph_and_context(
    tmp_path: Path,
):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(),
        controller_id="controller-main",
        options=RenderObservationOptions(
            path=path,
            include_graph=True,
            include_context=True,
        ),
    )
    request = _request_with_graph({"output": {"params": {"title": "Clock"}}})
    request = replace(
        request, source=replace(request.source, content={"text": "Clock"})
    )

    await backend.render(request)

    record = _read_observations(path)[0]
    assert record["graph"] == request.graph
    assert record["content"] == {"text": "Clock"}
    assert record["context"] == request.context
    assert record["providerSessionId"] == "provider-session"


@pytest.mark.asyncio
async def test_observing_render_backend_records_availability_source_metadata(
    tmp_path: Path,
):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(),
        controller_id="controller-main",
        options=RenderObservationOptions(path=path),
    )
    request = _request_with_graph({"output": "value"})
    assert request.source is not None
    request = replace(
        request,
        binding_id=None,
        source=replace(
            request.source,
            command_type="controller_fallback",
            content_kind="overlay:unavailable_service",
            availability_cause="service",
            availability_state="unavailable",
            availability_source="service_view",
            availability_reason="sonos_service_unavailable",
        ),
    )

    await backend.render(request)

    record = _read_observations(path)[0]
    assert record["bindingId"] is None
    assert record["commandType"] == "controller_fallback"
    assert record["contentKind"] == "overlay:unavailable_service"
    assert record["availabilityCause"] == "service"
    assert record["availabilityState"] == "unavailable"
    assert record["availabilitySource"] == "service_view"
    assert record["availabilityReason"] == "sonos_service_unavailable"


@pytest.mark.asyncio
async def test_observing_render_backend_delegates_close(tmp_path: Path):
    delegate = ImmediateBackend()
    backend = ObservingRenderBackend(
        delegate,
        controller_id="controller-main",
        options=RenderObservationOptions(path=tmp_path / "render.jsonl"),
    )

    await backend.aclose()

    assert delegate.closed is True


@pytest.mark.asyncio
async def test_observation_failure_preserves_render_success_and_actual_encoding(
    tmp_path,
):
    path = tmp_path / "occupied"
    path.mkdir()
    backend = ObservingRenderBackend(
        ImmediateBackend(frame=b"png-frame"),
        controller_id="controller-main",
        options=RenderObservationOptions(path=path),
    )
    request = replace(
        _solid_request(),
        image_format=RasterFormat(width=72, height=72, format="PNG"),
    )
    result = await backend.render(request)
    assert result.frame == b"png-frame" and result.error is None
    assert backend._record(request, result, duration_ms=0)["encoding"] == "png"


def test_observation_files_have_a_byte_budget_and_keep_recent_complete_records(
    tmp_path,
):
    import json

    path = tmp_path / "renders.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(),
        controller_id="bounded",
        options=RenderObservationOptions(path=path, max_bytes=100, backup_count=2),
    )
    for index in range(100):
        backend._append_line(json.dumps({"index": index, "text": "é" * 10}))
    files = list(tmp_path.iterdir())
    assert len(files) == 3
    assert all(file.stat().st_size <= 100 for file in files)
    records = [
        json.loads(line) for file in files for line in file.read_text().splitlines()
    ]
    assert max(record["index"] for record in records) == 99
    assert min(record["index"] for record in records) > 0
    with pytest.raises(ValueError, match="exceeds the file budget"):
        backend._append_line("x" * 101)
    assert all(file.stat().st_size <= 100 for file in files)
