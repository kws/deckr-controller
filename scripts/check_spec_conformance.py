"""Run acceptance, baseline content, ordering and advertised extension vectors."""

import os
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
spec = Path(os.environ.get("DECKR_SPEC_ROOT", root.parent / "deckr")).resolve()
for name in (
    "action-runtime-rendering",
    "action-runtime-rendering-content",
    "action-runtime-rendering-ordering",
    "ksi-invariant-rendering",
):
    path = spec / f"contract/v1/vectors/{name}.v1.json"
    if not path.is_file():
        raise SystemExit(f"Missing reference artifact: {path}")
os.environ["DECKR_SPEC_ROOT"] = str(spec)
raise SystemExit(
    pytest.main(["-q", "--no-cov", str(root / "tests/test_rendering_vectors.py")])
)
