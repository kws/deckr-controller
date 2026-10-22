# AGENTS.md

This file gives repository rules for agents.
Use [README.md](README.md) for setup, checks, configuration, and packaging.
Use [Controller architecture](docs/architecture.md) for current behavior and source links.

## Code placement

This repository owns controller orchestration, configuration, device routes, pages, bindings, and rendering policy.
Shared specifications belong in the sibling `deckr` repository.
Reusable Python contracts and managed APIs belong in the sibling `deckr-core-python` repository.
Use public core APIs for hardware and services.

| Directory | Responsibility |
| --- | --- |
| `src/deckr/controller` | Runtime construction, device-session state boundaries, effects, and rendering dispatch. |
| `src/deckr/controller/_hardware` | Config selection, managed hardware uses, routes, and commands. |
| `src/deckr/controller/_actions` | Common action requests/adapters, managed Action Runtime services, and availability. |
| `src/deckr/controller/_pages` | Page frames, sessions, and transition decisions. |
| `src/deckr/controller/_bindings` | Binding reconciliation, input ownership, lifecycle, presentation, and render admission. |
| `src/deckr/controller/config` | Config models, YAML storage, and materialized projections. |
| `src/deckr/controller/settings` | Read-only settings snapshots and stable action identities. |
| `src/deckr/controller/action_provider` | Builtin action lookup and navigation actions. |
| `src/deckr/controller/invariant` | Local rendering operations and recipes. |
| `tests` | Controller behavior tests. |
| `test_support` | Shared controller test helpers. |

## Checks and documentation

Use `uv` for development commands:

```bash
uv sync --locked --dev
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync pytest
uv run --no-sync python scripts/check_spec_conformance.py
uv build
```

Update the architecture reference when component responsibilities or runtime behavior change.
Describe implemented behavior and link to source code and tests.
Keep setup and packaging procedures in README.
Keep personal preferences and context outside this repository.
