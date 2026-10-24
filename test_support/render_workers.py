"""Importable probes executed in managed render subprocesses."""

import signal

from deckr.controller._render_workers import _init_render_worker


def initialized_sigint_handler():
    _init_render_worker()
    return int(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)


def block_in_process(path):
    import os
    import time
    from pathlib import Path

    Path(path).write_text(str(os.getpid()))
    time.sleep(60)


def fail_http_in_process():
    import httpx

    request = httpx.Request("GET", "http://artwork.test/cover.jpg")
    response = httpx.Response(503, request=request)
    response.raise_for_status()


def unpicklable_result_in_process():
    return lambda: None
