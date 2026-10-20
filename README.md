# deckr-controller

`deckr-controller` is the Python controller runtime for the Deckr ecosystem.

It owns the controller-specific parts of the system:

- configuration loading and runtime services
- device management and remote hardware integration
- command routing, navigation, and persistence
- rendering and plugin execution context

The specification lives in [`deckr`](https://github.com/kws/deckr).
The Python implementation is the `deckr` distribution, developed in
[`deckr-core-python`](https://github.com/kws/deckr-core-python).

## Repository Layout

```text
src/deckr/controller/
  config/      Configuration models and file-backed services
  invariant/   Rendering helpers and invariant integrations
  _hardware/   Hardware discovery, claims, and controller routing
  _actions/    Action Runtime service integration
  action_provider/  Controller-side action lookup and builtins
tests/
```

## Requirements

- Python 3.11+
- `uv`

## Quick Start

Check out `deckr-core-python` beside this repository, then install the project
and development tooling. The uv source override uses that sibling checkout
as an editable dependency while the core API is in alpha:

```bash
uv sync
```

CI checks out the core revision recorded in `.deckr-core-revision` and installs
the locked dependencies. When adopting a new core revision, update that file
and `uv.lock` together after running the controller checks against that checkout.
The local source override does not change the published dependency requirement.

Run the default validation suite:

```bash
uv run ruff check .
uv run pytest
```

## Materialized Config

Local monolith runs can use the direct file-backed config service:

```toml
[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"
```

For a controller that consumes config from NATS KV, configure the materialized
source instead:

```toml
[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev_deckr_controller_config_v1"
```

To push local files to a remote broker, configure the same file-backed watcher
as a standalone Deckr component instance in a normal `deckr.toml`:

```toml
[deckr.runtime.substrate]
kind = "nats"
url = "nats://remote.example:4222"

[deckr.components.instances.local_config_watcher]
component = "dev.deckr.controller.config_watcher"
instance_id = "local"

[deckr.components.instances.local_config_watcher.config]
path = "settings"
target_controller_id = "controller-main"
```

The watcher has no endpoint slots and does not register a Deckr endpoint. It
writes the whole projection to
`dev_deckr_controller_config_v1/config.controllers.<controller-id>.materialized`.

Build distributables:

```bash
uv build
```

## Relationship To `deckr`

`deckr-controller` depends on `deckr`.

Keep the boundary clean:

- shared contracts belong in the `deckr` specification
- reusable Python APIs and wire models belong in `deckr-core-python`
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
