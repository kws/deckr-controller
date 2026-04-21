"""Executor factory for invariant-gfx with deckr ops registered."""

from __future__ import annotations

from pathlib import Path

import invariant_gfx
from invariant import Executor, OpRegistry
from invariant.ops import stdlib
from invariant.store.chain import ChainStore
from invariant.store.disk import DiskStore
from invariant.store.memory import MemoryStore

from deckr.controller.invariant.ops.encode_jpeg import encode_jpeg
from deckr.controller.invariant.ops.fetch_url import fetch_image_url

L1_MAX_ARTIFACTS = 2_000
L2_SIZE_LIMIT_BYTES = 512 * 1024 * 1024
L2_EVICTION_POLICY = "least-frequently-used"


class ProcessSafeDiskStore(DiskStore):
    """Compatibility alias for tests and older imports.

    The underlying DiskStore is now backed by diskcache, which already handles
    concurrent access across threads and processes.
    """


def build_executor(*, cache_dir: Path | str | None = None) -> Executor:
    """Build an Executor with gfx core ops and a bounded LFU cache chain."""

    registry = OpRegistry()
    invariant_gfx.register_core_ops(registry)
    registry.register_package("stdlib", stdlib)
    registry.register("deckr:fetch_image_url", fetch_image_url)
    registry.register("deckr:encode_jpeg", encode_jpeg)

    store = ChainStore(
        l1=MemoryStore(cache="lfu", max_size=L1_MAX_ARTIFACTS),
        l2=ProcessSafeDiskStore(
            cache_dir=cache_dir,
            size_limit_bytes=L2_SIZE_LIMIT_BYTES,
            eviction_policy=L2_EVICTION_POLICY,
        ),
    )
    return Executor(registry=registry, store=store)


_EXECUTOR: Executor | None = None


def get_executor() -> Executor:
    """Return the process-local Executor singleton."""

    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = build_executor()
    return _EXECUTOR


# Backwards-compatible module attribute for existing imports.
executor = get_executor()
