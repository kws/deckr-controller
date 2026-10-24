# deckr-controller

`deckr-controller` is the Python controller runtime for Deckr.
It selects device configurations, connects actions to controls, and manages pages, input, and output.

Read [Controller architecture](docs/architecture.md) for the current components, state flows, authority checks, and test map.

Shared specifications belong in [deckr](https://github.com/kws/deckr).
The Python `deckr` package comes from [deckr-core-python](https://github.com/kws/deckr-core-python).

## Development

Use Python 3.11 or later and `uv`.
Check out `deckr-core-python`, `invariant-core` and `invariant-gfx` beside this
repository. The source overrides in `pyproject.toml` use those editable checkouts.
The Invariant changes require the exact commits recorded in `.invariant-core-revision`
and `.invariant-gfx-revision`, rather than the older published stable packages.

```bash
git clone https://github.com/kws/invariant-core.git ../invariant-core
git -C ../invariant-core checkout --detach "$(cat .invariant-core-revision)"
git clone https://github.com/kws/invariant-gfx.git ../invariant-gfx
git -C ../invariant-gfx checkout --detach "$(cat .invariant-gfx-revision)"
```

In the parent Deckr workspace, this controller is an editable standalone dependency
and is excluded from workspace membership. That lets both the parent and standalone
CI resolve its sibling source overrides consistently.

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
Update dependency revision files and `uv.lock` together when adopting new core or Invariant source commits.
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

When hosted by the configured Deckr launcher, supervised restart cleanup is
provided by the Python core. Set `DECKR_RESTART_HANDOFF` to a supervisor-controlled
file and `DECKR_INSTANCE_ID` to the configured host process identity. The
[handoff contract](https://github.com/kws/deckr/blob/main/docs/deckr-deployment.md#verified-restart-handoff)
requires confirmed exit of the exact predecessor and its directly reported
endpoint sessions. The host cancels that predecessor's participation before
starting replacement endpoints; other live instances retain their contracts.
Embedded hosts pass the verified evidence to `Deckr(restart=proof)` explicitly.

For Linux Docker deployments, use the workspace's
[trusted supervisor setup](https://github.com/kws/sidepanel/blob/main/docker/README.md#supervised-controller-restarts-on-linux).
It supplies `DECKR_SUPERVISOR_SOCKET` as well as the handoff settings, records
sessions before the controller can use them, and owns container restart ordering.
Docker's automatic restart must be disabled for this supervised container.
The launcher confirms predecessor cleanup before controller components start.

## Packaging

Deckr is under active alpha development. Ordinary changes use Git commits as
revisions and leave package versions unchanged. Version changes, release tags and
publication require an explicit release request.

The root `pyproject.toml` owns the distribution version. Build a source distribution
and wheel after running the development checks:

```bash
uv build
```
