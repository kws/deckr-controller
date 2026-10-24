"""Reproducible local renderer CPU and async-resource probes; no external network.

Run: uv run python scripts/benchmark_rendering.py
Numbers describe generated inputs on this host, not physical display latency.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import statistics
import time

import anyio
import httpx
from invariant_gfx.artifacts import BlobArtifact, ImageArtifact
from invariant_gfx.ops.composite import composite
from PIL import Image

from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
from deckr.controller._render_graph import render_graph
from deckr.controller._render_resources import ImageResources


def median_ms(function, repeats=15):
    function()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def cpu_probes():
    pixels = Image.frombytes(
        "RGBA", (640, 640), random.Random(7).randbytes(640 * 640 * 4)
    )

    def png_identity():
        buffer = io.BytesIO()
        pixels.save(buffer, format="PNG", compress_level=1, optimize=False)
        return hashlib.sha256(buffer.getvalue()).hexdigest()

    buffer = io.BytesIO()
    pixels.convert("RGB").save(buffer, format="JPEG", quality=90)
    url = "https://artwork.test/cover"
    resources = {url: BlobArtifact(buffer.getvalue(), "image/jpeg")}
    graph = {
        "format": "invariant-graph",
        "version": 1,
        "output": "out",
        "graph": {
            "fetch": {
                "kind": "node",
                "op_name": "deckr:fetch_image_url",
                "params": {"url": url},
                "deps": [],
            },
            "image": {
                "kind": "node",
                "op_name": "gfx:blob_to_image",
                "params": {"blob": {"$ref": "fetch"}},
                "deps": ["fetch"],
            },
            "out": {
                "kind": "node",
                "op_name": "gfx:resize",
                "params": {"image": {"$ref": "image"}, "width": 96, "height": 96},
                "deps": ["image"],
            },
        },
    }
    base = ImageArtifact(Image.new("RGBA", (512, 512), (200, 100, 50, 255)))
    blend = ImageArtifact(Image.new("RGBA", (512, 512), (70, 170, 220, 192)))

    def layers(mode):
        return [
            {"image": base},
            {
                "image": blend,
                "anchor": {"type": "absolute", "x": 0, "y": 0},
                "mode": mode,
            },
        ]

    return {
        "png_identity_640_median_ms": median_ms(png_identity),
        "rgba_identity_640_median_ms": median_ms(
            lambda: ImageArtifact(pixels).get_stable_hash()
        ),
        "provider_640_to_96_median_ms": median_ms(
            lambda: render_graph(graph, 96, 96, DEFAULT_LIMITS, resources)
        ),
        "normal_blend_512_median_ms": median_ms(lambda: composite(layers("normal"))),
        "multiply_blend_512_median_ms": median_ms(
            lambda: composite(layers("multiply"))
        ),
    }


async def resource_probes():
    async def response(request):
        await anyio.sleep(0.15)
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"sample"
        )

    resources = ImageResources(transport=httpx.MockTransport(response))
    urls = ["https://artwork.test/one", "https://artwork.test/two"]
    try:
        started = time.perf_counter()
        for url in urls:
            await resources.get(url, max_bytes=1024, deadline=time.monotonic() + 2)
        serial = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        await resources.acquire(urls, max_bytes=1024, deadline=time.monotonic() + 2)
        parallel = (time.perf_counter() - started) * 1000
        return {
            "two_150ms_resources_serial_ms": round(serial, 3),
            "two_150ms_resources_parallel_ms": round(parallel, 3),
        }
    finally:
        await resources.aclose()


if __name__ == "__main__":
    print(
        json.dumps(
            {"cpu": cpu_probes(), "resources": anyio.run(resource_probes)}, indent=2
        )
    )
