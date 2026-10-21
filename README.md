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
  _hardware/   Controller policy and routes over managed core hardware
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

## Managed runtime integration

The controller targets the alpha Action Runtime contract at the core source revision in
`.deckr-core-revision`. Hardware discovery, agreements, recovery, event authorization,
and command validation use the public `DeckrHardware` managed API. Controller
code selects configurations and owns device routes. Material descriptor changes
require a new hardware agreement; discovery refreshes do not replace a live claim.

Action Runtime commands use canonical `services/serviceMessage` envelopes and
public managed service authorization. `open_page` accepts a binding or page-session
target; `replace_page` and `close_page` require a page-session target. Targets carry
an expected generation. The controller serializes transitions and checks the
retained owner, generation, hardware authority, and service agreement again after
planning, immediately before committing. A successor agreement does not inherit
a previous agreement's dynamic page. Capability selection uses the core matcher
within the configured control scope. Omitted, null, and empty requirements grant
no capabilities; actions must explicitly declare the capabilities they use.

The default test suite includes managed hardware and Action Runtime integration
on both the in-process transport and a real NATS JetStream broker. The development
dependencies include the pinned NATS binary. Tests start isolated brokers on
localhost with temporary storage and authentication, then shut them down; no
running broker is required. The focused integration command is:

```bash
uv run pytest tests/test_hardware_service.py tests/test_hardware_endpoint_routing.py tests/test_action_runtime_integration.py
```

Coverage includes input and command validation, label-based configuration,
agreement loss and recovery, descriptor replacement, distinct manager identities,
page target and generation checks, control scope, lifecycle delivery, authority
loss during planning, and shutdown.

Rendering follows the [Action Runtime rendering specification](https://github.com/kws/deckr/blob/main/docs/deckr-action-runtime-rendering.md).
Binding attachment advertises named raster targets, logical canvas dimensions,
and fixed limits. The controller accepts plain text and inline PNG/JPEG content
through `binding_render`; URL and Invariant formats are not advertised. Internal
controller recipes can still use those facilities. `binding_output.set_frame`
accepts encoded image bytes, with descriptor dimensions and encoding checks.

Base renders, direct frames, clears, overlays, and fallback output share output
ordering. New admitted base content supersedes unfinished older work even if the
new content fails. Base changes preserve overlays, and failures retain the last
successfully dispatched base. Final dispatch rechecks the binding, service
agreement, hardware claim, and output revisions. Results distinguish local
dispatch from a correlated hardware rejection; neither is a display acknowledgement.

Limits per advertised target are 262,144 envelope bytes, 131,072 image-file bytes,
1,048,576 source pixels, 1,024 text code points, and 2,000 ms from admission to
local dispatch. Admission allows at most two active jobs per control and eight
across the process. Obsolete workers keep their slot until they finish. No pending
base queue is allocated. Retained request records are bounded at 512 per device;
hardware rejection mappings are bounded at 1,024 and expire after 60 seconds.

Run `uv run pytest tests/test_rendering_contract.py` for rendering integration
checks. Run `uv run python scripts/check_spec_conformance.py` for the reference
content and ordering vectors over memory and NATS. The latter reads `../deckr`,
or the checkout selected by `DECKR_SPEC_ROOT`, and fails on missing artifacts.
It verifies content geometry and limits, reverse completion, clear supersession,
failure supersession, rejection ordering, and exact hardware-command correlation.
The full suite additionally covers overlays, duplicate delivery, deadlines,
capacity, authority loss, page hiding, direct frames, and late hardware rejection.

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
