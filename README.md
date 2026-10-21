# deckr-controller

`deckr-controller` is the Python controller runtime for Deckr.
It selects device configurations, connects actions to controls, and manages pages, input, and output.

Read [Controller architecture](docs/architecture.md) for the current components, state flows, authority checks, and test map.

Shared specifications belong in [deckr](https://github.com/kws/deckr).
The Python `deckr` package comes from [deckr-core-python](https://github.com/kws/deckr-core-python).

## Development

Use Python 3.11 or later and `uv`.
Check out `deckr-core-python` beside this repository.
The source override in `pyproject.toml` uses that checkout as an editable dependency.

Install dependencies and run the checks:

```bash
uv sync
uv run ruff check .
uv run pytest
uv run python scripts/check_spec_conformance.py
```

The reference check requires the sibling `deckr` checkout.
Set `DECKR_SPEC_ROOT` to use another location.

CI selects the core commit from `.deckr-core-revision`.
It selects the specification commit from the core checkout's `.deckr-spec-revision`.
Update `.deckr-core-revision` and `uv.lock` together when the controller adopts a new core commit.
Check the controller against that commit before the update.

Managed integration tests start isolated NATS brokers with temporary storage and authentication.
The development dependencies include the NATS binary.
A separate broker is not necessary.

The optional language workload uses Python, TypeScript, and Rust provider processes.
Run `scripts/controller_action_runtime.py` from the sibling
[deckr-integration-tests](https://github.com/kws/deckr-integration-tests) repository.
Use an environment with this controller and the sibling core package installed as editable dependencies.
The runner requires native toolchains and records evidence for each provider language.
Ordinary controller test runs skip this workload.

Build distributions:

```bash
uv build
```

## Component configuration

The controller component ID is `dev.deckr.controller`.
The component requires a `controller` endpoint slot.
This file-source example uses the component configuration model:

```toml
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.endpoints]
controller = "controller-main"

[deckr.components.instances.controller_main.config.device_config.file]
path = "settings"
```

To use materialized config, replace the file-source section with this section:

```toml
[deckr.components.instances.controller_main.config.device_config.materialized]
bucket = "dev_deckr_controller_config_v1"
```

A separate config watcher can send local YAML configurations to a remote NATS broker:

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

The watcher publishes a complete projection for the target controller.
See [configuration and settings](docs/architecture.md#3-configuration-and-settings) for storage and activation behavior.

## Releases

The root `pyproject.toml` owns the published `deckr-controller` version.
Stable releases use PEP 440 versions and tags of the form `deckr-controller-vX.Y.Z`.

1. Set `version` in `pyproject.toml` to the stable version.
2. Update the `deckr` dependency if the controller requires a new core package release.
3. Run the checks:

   ```bash
   uv run ruff check .
   uv run pytest
   uv run python scripts/check_spec_conformance.py
   ```

4. Refresh the lockfile:

   ```bash
   uv lock --refresh
   ```

5. Commit the release changes.
6. Tag the release commit as `deckr-controller-vX.Y.Z`.
7. Build from the tag:

   ```bash
   git checkout deckr-controller-vX.Y.Z
   uv build
   git checkout -
   ```

8. Publish the wheel and source distribution with the usual PyPI workflow.
9. Set the next development version to `X.(Y+1).0.dev0` in a separate commit.
   Refresh `uv.lock` after this version change.
