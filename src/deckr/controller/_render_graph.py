"""Owner-defined, bounded Invariant rendering extension.

This executor has an explicit operation registry and no persistent graph cache.
It runs in a cancellable worker process; the caller enforces the render deadline.
"""

from __future__ import annotations

import ast
import base64
import io
import math
import re
import time
from functools import wraps
from urllib.parse import urlsplit

import httpx
from deckr.actions.rendering import RenderImage
from deckr.contracts.models import thaw_json

from deckr.controller._render_content import ContentFailure, decode_image

INVARIANT_FORMAT = "com.k-si.deckr.render.invariant.v1"
MAX_NODES = 64
MAX_DEPTH = 16
MAX_DIMENSION = 1024
MAX_TOTAL_PIXELS = 16 * 1024 * 1024
OPS = frozenset(
    {
        "gfx:create_solid",
        "gfx:resolve_resource",
        "gfx:render_svg",
        "gfx:colorize",
        "gfx:render_text",
        "gfx:composite",
        "gfx:resize",
        "gfx:blob_to_image",
        "gfx:packed_text",
        "stdlib:make_dict",
        "deckr:fetch_image_url",
    }
)


def _expression(value):
    if not value.startswith("${") or not value.endswith("}"):
        raise ContentFailure("invalid_content")
    tree = ast.parse(value[2:-1], mode="eval")
    references = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                not isinstance(node.func, ast.Name)
                or node.func.id != "decimal"
                or len(node.args) != 1
                or node.keywords
            ):
                raise ContentFailure("invalid_content")
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.attr not in {
                "width",
                "height",
                "size",
            }:
                raise ContentFailure("invalid_content")
        elif isinstance(node, ast.Name) and node.id != "decimal":
            references.add(node.id)
        elif isinstance(node, ast.Constant):
            try:
                number = float(node.value)
            except (ValueError, TypeError):
                raise ContentFailure("invalid_content") from None
            if not math.isfinite(number) or abs(number) > 4096:
                raise ContentFailure("limit_exceeded")
        elif not isinstance(
            node,
            (
                ast.Expression,
                ast.BinOp,
                ast.UnaryOp,
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.Div,
                ast.UAdd,
                ast.USub,
                ast.Name,
                ast.Load,
            ),
        ):
            raise ContentFailure("invalid_content")

    return references


def _value(value, limits, depth=0, dependencies=None):
    if depth > MAX_DEPTH:
        raise ContentFailure("limit_exceeded")
    if isinstance(value, str):
        if len(value) > max(limits.max_text_code_points, 2048):
            raise ContentFailure("limit_exceeded")
        if "${" in value:
            if not _expression(value) <= dependencies:
                raise ContentFailure("invalid_content")
    elif isinstance(value, (int, float)) and (
        not math.isfinite(value) or abs(value) > 4096
    ):
        raise ContentFailure("limit_exceeded")
    elif isinstance(value, dict):
        if len(value) > MAX_NODES:
            raise ContentFailure("limit_exceeded")
        if "$ref" in value:
            if (
                set(value) != {"$ref"}
                or not isinstance(value["$ref"], str)
                or value["$ref"] not in dependencies
            ):
                raise ContentFailure("invalid_content")
        if "$tuple" in value and (
            set(value) != {"$tuple"} or not isinstance(value["$tuple"], list)
        ):
            raise ContentFailure("invalid_content")
        for key, item in value.items():
            if key.startswith("$") and key not in {"$ref", "$tuple"}:
                raise ContentFailure("invalid_content")
            _value(item, limits, depth + 1, dependencies)
    elif isinstance(value, list):
        if len(value) > MAX_NODES:
            raise ContentFailure("limit_exceeded")
        for item in value:
            _value(item, limits, depth + 1, dependencies)


def validate_graph(document, limits):
    if (
        set(document) != {"format", "version", "output", "graph"}
        or document["format"] != "invariant-graph"
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ContentFailure("invalid_content")
    graph = document["graph"]
    if not isinstance(graph, dict) or not 1 <= len(graph) <= MAX_NODES:
        raise ContentFailure("limit_exceeded")
    if not isinstance(document["output"], str) or document["output"] not in graph:
        raise ContentFailure("invalid_content")
    for name, node in graph.items():
        if (
            not name
            or name == "canvas"
            or not isinstance(node, dict)
            or set(node) != {"kind", "op_name", "params", "deps"}
            or node["kind"] != "node"
            or not isinstance(node["op_name"], str)
            or node["op_name"] not in OPS
        ):
            raise ContentFailure("invalid_content")
        if (
            not isinstance(node["params"], dict)
            or not isinstance(node["deps"], list)
            or len(node["deps"]) > MAX_NODES
            or any(
                not isinstance(dep, str) or dep not in graph and dep != "canvas"
                for dep in node["deps"]
            )
            or len(set(node["deps"])) != len(node["deps"])
        ):
            raise ContentFailure("invalid_content")
        _value(node["params"], limits, dependencies=set(node["deps"]))
        if node["op_name"] == "gfx:render_svg":
            ref = node["params"].get("svg_content")
            source = graph.get(ref.get("$ref")) if isinstance(ref, dict) else None
            if source is None or source["op_name"] != "gfx:resolve_resource":
                raise ContentFailure("invalid_content")
    depths = {"canvas": 0}

    def visit(name, path):
        if name in path:
            raise ContentFailure("invalid_content")
        if name in depths:
            return depths[name]
        depth = 1 + max(
            (visit(dep, (*path, name)) for dep in graph[name]["deps"]), default=0
        )
        if depth > MAX_DEPTH:
            raise ContentFailure("limit_exceeded")
        depths[name] = depth
        return depth

    for name in graph:
        visit(name, ())


class _Registry:
    def __init__(self, limits, deadline):
        from invariant.ops.stdlib import make_dict
        from invariant_gfx.ops import OPS as gfx_ops

        self.limits, self.deadline = limits, deadline
        self.pixels = 0
        self.operations = {
            f"gfx:{name}": op for name, op in gfx_ops.items() if f"gfx:{name}" in OPS
        }
        self.operations.update(
            {
                "stdlib:make_dict": make_dict,
                "deckr:fetch_image_url": self.fetch,
                "gfx:blob_to_image": self.decode,
            }
        )

    def check_size(self, width, height):
        width, height = int(width), int(height)
        if (
            min(width, height) < 1
            or max(width, height) > MAX_DIMENSION
            or width * height > self.limits.max_image_pixels
        ):
            raise ContentFailure("limit_exceeded")

    def check_text_allocation(self, name, params):
        # Invariant's text operations allocate their tight image after measuring.
        # Preflight the same layout so limits apply before that allocation.
        from decimal import Decimal

        from invariant_gfx.ops.render_text import (
            _fit_width_font_size,
            _measure_text_width,
        )

        text, font = params["text"], params["font"]
        weight, style = params.get("weight"), params.get("style", "normal")
        if name == "gfx:render_text":
            size = (
                _fit_width_font_size(
                    text,
                    font,
                    Decimal(str(params["fit_width"])),
                    weight,
                    style,
                    params.get("exact", False),
                )
                if params.get("fit_width") is not None
                else int(params["size"])
            )
            width, height = _measure_text_width(text, font, size, weight, style)
            self.check_size(width + 4, height + 4)
            return
        from invariant_gfx.ops.packed_text import _fit_layout, _TextMeasurer

        width, height = map(int, params["size"])
        minimum = int(params.get("min_font_size", 10))
        maximum = int(params.get("max_font_size") or max(min(width, height), minimum))
        gap = int(params.get("line_gap", 0))
        if (
            not 1 <= minimum <= maximum <= MAX_DIMENSION
            or not 0 <= gap <= MAX_DIMENSION
        ):
            raise ContentFailure("limit_exceeded")
        layout = _fit_layout(
            text=text,
            font=font,
            width=width,
            height=height,
            min_font_size=minimum,
            max_font_size=maximum,
            line_gap=gap,
            weight=weight,
            style=style,
            measurer=_TextMeasurer(font, weight, style),
        )
        sizes = [
            _measure_text_width(line, font, layout.font_size, weight, style)
            for line in layout.lines
        ]
        for line_width, line_height in sizes:
            self.check_size(line_width + 4, line_height + 4)
        self.check_size(
            max(w + 4 for w, _ in sizes),
            sum(h + 4 for _, h in sizes) + gap * (len(sizes) - 1),
        )

    def decode(self, blob):
        from invariant_gfx.artifacts import ImageArtifact

        encoding = {"image/png": "png", "image/jpeg": "jpeg"}.get(blob.content_type)
        if encoding is None:
            raise ContentFailure("invalid_content")
        return ImageArtifact(
            decode_image(
                RenderImage(
                    image=base64.b64encode(blob.data).decode("ascii"), encoding=encoding
                ),
                self.limits,
            )
        )

    def fetch(self, url):
        from invariant_gfx.artifacts import BlobArtifact

        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ContentFailure("invalid_content")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ContentFailure("deadline_exceeded")
        # No redirects, cookies, environment proxies, credentials, or persistent cache.
        with (
            httpx.Client(
                timeout=min(1.0, remaining), follow_redirects=False, trust_env=False
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip()
            )
            if content_type not in {"image/png", "image/jpeg"}:
                raise ContentFailure("invalid_content")
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=16384):
                if time.monotonic() >= self.deadline:
                    raise ContentFailure("deadline_exceeded")
                if len(data) + len(chunk) > self.limits.max_image_bytes:
                    raise ContentFailure("limit_exceeded")
                data.extend(chunk)
        return BlobArtifact(data=bytes(data), content_type=content_type)

    def has(self, name):
        return name in self.operations

    def get(self, name):
        op = self.operations[name]

        @wraps(op)
        def run(**params):
            from invariant_gfx.artifacts import ImageArtifact

            if time.monotonic() >= self.deadline:
                raise ContentFailure("deadline_exceeded")
            if name == "gfx:resolve_resource" and not re.fullmatch(
                r"lucide:[a-z0-9-]+", params.get("name", "")
            ):
                raise ContentFailure("invalid_content")
            if "font" in params and params["font"] != "Inter":
                raise ContentFailure("invalid_content")
            if (
                "text" in params
                and len(params["text"]) > self.limits.max_text_code_points
            ):
                raise ContentFailure("limit_exceeded")
            size = params.get("size")
            if isinstance(size, (list, tuple)):
                self.check_size(*size)
            elif size is not None and not 1 <= float(size) <= 128:
                raise ContentFailure("limit_exceeded")
            for key in ("width", "height", "fit_width", "max_font_size"):
                if (
                    key in params
                    and params[key] is not None
                    and not 1 <= float(params[key]) <= MAX_DIMENSION
                ):
                    raise ContentFailure("limit_exceeded")
            if name in {"gfx:resize", "gfx:render_svg"}:
                # Both dimensions are explicit, so aspect/scale cannot allocate beyond limits.
                if "scale" in params or "width" not in params or "height" not in params:
                    raise ContentFailure("invalid_content")
                self.check_size(params["width"], params["height"])
            if name == "gfx:composite":
                for layer in params["layers"]:
                    anchor = layer.get("anchor", {})
                    if any(
                        abs(float(anchor.get(key, 0))) > MAX_DIMENSION
                        for key in ("x", "y")
                    ):
                        raise ContentFailure("limit_exceeded")
            if name in {"gfx:render_text", "gfx:packed_text"}:
                self.check_text_allocation(name, params)
            result = op(**params)
            if isinstance(result, ImageArtifact):
                self.check_size(result.width, result.height)
                self.pixels += result.width * result.height
                if self.pixels > MAX_TOTAL_PIXELS:
                    raise ContentFailure("limit_exceeded")
            return result

        return run


def render_graph(document, width, height, limits):
    from invariant import Executor, load_graph_document_from_dict
    from invariant.store.memory import MemoryStore
    from invariant_gfx.artifacts import ImageArtifact
    from PIL import Image, ImageOps

    document = thaw_json(document)
    validate_graph(document, limits)
    registry = _Registry(limits, time.monotonic() + limits.max_render_time_ms / 1000)
    registry.check_size(width, height)
    graph, output = load_graph_document_from_dict(document)
    result = Executor(registry=registry, store=MemoryStore(max_size=MAX_NODES)).execute(
        graph, [output], context={"canvas": {"width": width, "height": height}}
    )[output]
    if not isinstance(result, ImageArtifact):
        raise ContentFailure("invalid_content")
    canvas = Image.new("RGBA", (width, height), "black")
    source = ImageOps.contain(result.image, (width, height))
    canvas.alpha_composite(
        source, ((width - source.width) // 2, (height - source.height) // 2)
    )
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
