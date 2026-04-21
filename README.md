# deckr-controller

`deckr-controller` is the Python controller runtime for the Deckr ecosystem.

It owns the controller-specific parts of the system:

- configuration loading and runtime services
- device management and remote hardware integration
- command routing, navigation, and persistence
- rendering and plugin execution context

The shared contracts it depends on live in the sibling `deckr` repository:

- `https://github.com/kws/deckr`

## Repository Layout

```text
src/deckr/controller/
  config/      Configuration models and file-backed services
  invariant/   Rendering helpers and invariant integrations
  mqtt/        MQTT host integration
  plugin/      Controller-side plugin runtime and builtins
tests/
```

## Requirements

- Python 3.11+
- `uv`

## Quick Start

Install the project and development tooling:

```bash
uv sync
```

Run the default validation suite:

```bash
uv run ruff check .
uv run pytest
```

Build distributables:

```bash
uv build
```

## Relationship To `deckr`

`deckr-controller` depends on `deckr`.

Keep the boundary clean:

- shared contracts and reusable wire models belong in `deckr`
- controller orchestration and runtime policy belong here

If a controller change needs a new shared contract, release `deckr` first, then
update the `deckr` dependency range in this repo before cutting the controller
release.

## Releases

This repository releases a single distribution: `deckr-controller`.

- The source of truth for the published version is the root `pyproject.toml`.
- Use package tags in the form `deckr-controller-vX.Y.Z`.
- Stable releases use normal PEP 440 versions such as `0.3.0`.
- After each stable release, bump immediately to the next development line,
  e.g. `0.4.0.dev0`, in a separate follow-up commit.

### Release Flow

1. Update `version` in `pyproject.toml` to the stable release number.
2. If needed, update the `deckr` dependency constraint to the newly released
   core version.
3. Run the validation suite:

   ```bash
   uv run ruff check .
   uv run pytest
   ```

4. Refresh the lockfile:

   ```bash
   uv lock --refresh
   ```

5. Commit the release, for example:

   ```bash
   git commit -am "chore(deckr-controller): release v0.3.0"
   ```

6. Tag the release commit:

   ```bash
   git tag deckr-controller-v0.3.0
   ```

7. Build from the tag so the artifacts match the stable version exactly:

   ```bash
   git checkout deckr-controller-v0.3.0
   uv build
   git checkout -
   ```

8. Publish the wheel and sdist using your usual PyPI workflow.
9. Immediately bump `pyproject.toml` to the next development version, refresh
   the lockfile, and commit that separately:

   ```bash
   uv lock --refresh
   git commit -am "chore(deckr-controller): bump to development release 0.4.0.dev0"
   ```
