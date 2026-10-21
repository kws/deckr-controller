"""Run controller content and ordering cases from the external Deckr reference."""

import os
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
spec = Path(os.environ.get("DECKR_SPEC_ROOT", root.parent / "deckr")).resolve()
for name in ("content", "ordering"):
    path = spec / f"contract/v1/vectors/action-runtime-rendering-{name}.v1.json"
    if not path.is_file():
        raise SystemExit(f"Missing reference artifact: {path}")
os.environ["DECKR_SPEC_ROOT"] = str(spec)
raise SystemExit(
    pytest.main(["-q", "--no-cov", str(root / "tests/test_rendering_vectors.py")])
)
