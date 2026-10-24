"""Small cached loading frames with execution capacity reserved for feedback."""

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from deckr.controller._render_workers import run_thread

LOADING_DELAY = 0.12
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deckr-feedback")
_SLOTS = threading.BoundedSemaphore(2)


@lru_cache(maxsize=64)
def _loading_frame(fmt):
    width, height = (
        (fmt.height, fmt.width) if fmt.rotation % 180 else (fmt.width, fmt.height)
    )
    canvas = Image.new("RGB", (width, height), (28, 31, 37))
    draw = ImageDraw.Draw(canvas)
    side = min(width, height)
    radius = max(2, int(side * 0.2))
    x, y = width // 2, int(height * 0.4)
    draw.arc(
        (x - radius, y - radius, x + radius, y + radius),
        25,
        305,
        fill=(96, 165, 250),
        width=max(1, side // 24),
    )
    font = ImageFont.load_default(size=max(8, side // 7))
    draw.text(
        (width / 2, height * 0.76), "Loading", anchor="mm", fill="white", font=font
    )
    if fmt.rotation:
        canvas = canvas.rotate(fmt.rotation, expand=True)
    out = io.BytesIO()
    canvas.save(out, format=fmt.format, quality=50)
    return out.getvalue()


async def loading_frame(fmt):
    return await run_thread(_loading_frame, fmt, slots=_SLOTS, pool=_POOL)
