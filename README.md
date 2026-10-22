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

The Invariant renderer also needs the Cairo system library (`libcairo2` on
Debian/Ubuntu, or `brew install cairo` on macOS). If a managed Python on macOS
cannot locate Homebrew's library, set `DYLD_FALLBACK_LIBRARY_PATH` to the Homebrew
library directory, for example `/opt/homebrew/lib` on Apple Silicon.
If a console-script launcher drops that variable, invoke its Python module instead,
for example `uv run python -m pytest`.

Install dependencies and run the checks:

```bash
uv sync --locked --dev
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync pytest
uv run --no-sync python scripts/check_spec_conformance.py
```

The reference check requires the sibling `deckr` checkout.
Set `DECKR_SPEC_ROOT` to use another location. It runs acceptance, content, ordering
and Invariant-extension cases; the selected reference must include all four files.

CI selects the core commit from `.deckr-core-revision`.
It selects the rendering reference from `.deckr-rendering-spec-revision`, including
the advertised Invariant extension vectors. The core implementation keeps its own
specification pin. CI validates this combination on Python 3.11, 3.12 and 3.13.
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
See [configuration](docs/architecture.md#configuration) and
[recovery policies](docs/architecture.md#recovery-policies) for storage and activation behavior.

## Packaging

Deckr is under active alpha development. Ordinary changes use Git commits as
revisions and leave package versions unchanged. Version changes, release tags and
publication require an explicit release request.

The root `pyproject.toml` owns the distribution version. Build a source distribution
and wheel after running the development checks:

```bash
uv build
```
