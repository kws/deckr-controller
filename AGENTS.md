# AGENTS.md

This file adds agent-specific guidance for the `deckr-controller` repository.

Use [README.md](./README.md) for developer-facing setup and the human release
flow. Keep README as the main source of truth and use this file for placement
rules and implementation hints.

## What Lives Here

`deckr-controller` is the Python controller runtime.

Use this repo for:

- controller services and orchestration
- config loading and persistence
- device-manager behavior and remote hardware wiring
- rendering policy and controller-side plugin runtime

Do not move shared contracts here if they are intended to be reused by other
Deckr components. Those belong in the sibling `deckr` repo.

## Directory Guide

- `src/deckr/controller`
  - Main controller runtime entry points and services.
- `src/deckr/controller/config`
  - Config models and file-backed config services.
- `src/deckr/controller/invariant`
  - Invariant-based rendering helpers and recipes.
- `src/deckr/controller/mqtt`
  - MQTT host integration.
- `src/deckr/controller/plugin`
  - Controller-side plugin runtime, providers, and builtins.
- `tests`
  - Tests for controller behavior only.

## Placement Rules

- If code is shared across multiple Deckr runtimes, it probably belongs in
  `deckr`, not here.
- If code depends on controller orchestration, state, rendering policy, or
  local config conventions, it belongs here.
- Prefer to depend on stable `deckr` contracts rather than reaching around them
  with ad hoc coupling.

## Development Commands

Use `uv` consistently:

```bash
uv sync
uv run ruff check .
uv run pytest
uv build
```

## Release Notes

Follow the release section in [README.md](./README.md#releases).

Short version:

- root `pyproject.toml` owns the published `deckr-controller` version
- tag stable releases as `deckr-controller-vX.Y.Z`
- if the controller needs a freshly released core package, update the `deckr`
  dependency before cutting the release
- after a stable release, bump immediately to the next `X.(Y+1).0.dev0`
- refresh `uv.lock` after every version change
