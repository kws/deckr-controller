# Controller Runtime Architecture Implementation Plan

This document tracks implementation progress toward
[`docs/controller-runtime-architecture.md`](./controller-runtime-architecture.md).
Use the architecture document for design rationale and this file for sequencing,
ownership, and progress.

Last updated: 2026-06-04.

## Status Legend

- `Done`: implemented, tested, and ready to rely on.
- `In progress`: active implementation exists but still needs cleanup, tests, or
  follow-up work.
- `Partial`: some behavior exists, but the architecture boundary is not complete.
- `Not started`: no meaningful implementation yet.
- `Blocked`: cannot proceed until an upstream contract, protocol, or dependency
  is decided.

## Target Outcome

The controller runtime should keep device state, configuration state, and action
availability as independent state domains:

- Device layout and navigation stay controller-owned.
- Configuration determines desired bindings but does not report live action
  availability.
- Provider discovery creates candidates only.
- Action availability is served from a local cache populated by provider-direct
  availability messages.
- Page planning and input routing are local, deterministic, and non-blocking.

## Current Snapshot

| Area | Status | Notes |
| --- | --- | --- |
| Config matching and config subscriptions | Partial | File-backed config service already supports matching, updates, and `None` removal events. |
| Validator separation from action lookup | Done | Binding validation resolves selectors/capabilities only; planner handles missing metadata as unavailable. |
| Provider-session gating removal | In progress | Binding/page transitions should not require Concord provider-session readiness. |
| Page frame model | In progress | Device runtime needs explicit static/dynamic frames with cached committed plans. |
| Held input cancellation | In progress | Rebinding must cancel old held inputs before releases are ignored. |
| Action availability service | Not started | Needs provider candidates, direct probes, cache, selection, expiry, and events. |
| Binding planner extraction | Done | `_binding_planner.py` owns local planning decisions and outcomes; `DeviceManager` still owns metadata refresh and commit-time mutation. |
| Action interest service | Not started | Needed actions must be tracked separately from button bindings. |
| Provider availability protocol | Not started | Requires controller/provider messages and provider-side support. |
| Multiple-provider selection | Not started | Needs deterministic, sticky provider ranking. |

## Recommended Next Slice

### 3. Introduce Action Availability Cache Skeleton

Recommended commit title:

```text
Introduce controller action availability cache
```

Focus this slice on creating the local availability data model and cache that
will eventually replace Beacon-backed binding-time `manager.get_action()` calls.
Keep runtime behavior compatible by bridging the current action registry lookup
into the new cache as a transitional source.

Why this first:

- The planner now consumes local metadata snapshots, so an availability service
  can be introduced behind that snapshot boundary without touching commit-time
  mutation.
- A small cache/data-model slice gives later provider-direct protocol work a
  stable internal contract before adding new wire messages.
- Tests can lock down the important distinction between discovery candidates,
  authoritative availability, stale records, and unavailable records before
  provider selection and action interest add more states.
- This is lower risk than replacing Beacon lookup in one step because the
  transitional bridge can preserve current `DeviceManager` behavior.

Scope for this slice:

- Add an internal action-availability module under `src/deckr/controller`, for
  example `_action_availability.py`.
- Define `ProviderActionKey`, `ActionAvailabilityState`, availability records,
  cache snapshots, freshness timestamps, stale-grace policy inputs, and source
  labels such as `beacon_candidate` and `provider_direct`.
- Add a small service/cache API that can answer planner `ActionIntentKey`
  requests from a local snapshot and update records from transitional
  `ActionMetadata` values.
- Keep `DeviceManager` responsible for the temporary `manager.get_action()`
  orchestration, but move `_action_metadata_cache` semantics behind the new
  availability cache where possible.
- Preserve the current metadata snapshot behavior for static pages, dynamic
  pages, catalog changes, retained-plan restore, and unavailable rendering.
- Add focused unit tests for record freshness, explicit provider filtering,
  provider-label matching, stale-grace behavior, and snapshot conversion for the
  planner.

Acceptance criteria:

- Availability cache APIs perform no bus, hardware, settings, or provider I/O.
- Beacon/action-registry metadata can populate cache records without making
  those records authoritative provider-direct availability.
- Planner metadata snapshots can be built from the cache for all currently
  visible page intents.
- Missing or expired records produce unavailable planner metadata without
  rejecting the page.
- Retained-plan restoration still works when the current cache snapshot is
  empty.
- Existing integration behavior remains unchanged while the transitional
  `manager.get_action()` bridge is still present.

Explicit non-goals:

- Do not add provider-direct availability request/snapshot/change messages yet.
- Do not implement provider-side availability support.
- Do not implement action interest tracking or warm interest retention.
- Do not implement deterministic multi-provider ranking beyond explicit
  provider/label filtering needed by the cache.
- Do not remove the transitional `manager.get_action()` bridge from
  `DeviceManager` yet.
- Do not change provider wire protocol or public package API.

Suggested tests:

```bash
uv run ruff check .
uv run pytest tests/test_action_availability.py tests/test_binding_planner.py tests/test_device_manager_integration.py
git diff --check
```

Add `tests/test_action_availability.py` with coverage for:

- cache record keys for explicit provider/action pairs
- provider-label constrained intent matching
- Beacon-backed candidate metadata converted into planner snapshots
- unavailable outcome for missing or expired records
- stale-grace retention of previously available metadata
- no awaitable provider lookup or bus mock used by the cache

## Milestones

### 1. Stabilize Device Runtime Boundaries

Goal: make the current `DeviceManager` behavior match the device-state
invariants before extracting services.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Remove provider-session readiness as a binding precondition | In progress | Existing action provider routing metadata | Static and dynamic pages can commit without waiting for Concord/session readiness. |
| Keep action lookup out of pure binding validation | In progress | Binding validator API compatibility | Validator only reports selector/capability errors; missing actions render unavailable later. |
| Add explicit page frame state | In progress | Navigation service page refs | Runtime stores committed static and dynamic frames and can restore prior plans. |
| Make dynamic close transactional | In progress | Page frame state | Closing a dynamic page builds/restores the return plan before finalizing the dynamic close. |
| Keep dynamic timeout device-owned | Partial | Existing timeout loop | Timeout fires even if the owner provider has disappeared. |
| Render unavailable controls without clearing layout | Partial | Unavailable render overlay | Unavailable action controls stay visible and do not invalidate the page. |
| Cancel held inputs during rebinding/revocation | In progress | Input tracking records | Every delivered `down` receives either `up` or `cancel` for the same binding. |
| Make config disappearance preserve device ownership | Partial | Config stream removal events | Bindings and dynamic pages are released, but the device remains connected and controllable. |

### 2. Extract a Local Binding Planner

Goal: move page planning out of mutation-heavy runtime code.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Define planner input/output models | Done | Current page plan/frame shape | Planner has explicit inputs for device, static/dynamic entries, metadata snapshots, retained plans, and dynamic sessions. |
| Model binding outcomes | In progress | Availability state model | Current planner models `bound`, `unavailable`, and `invalid_device_control`; pending, invalid-config, stale, and sticky-provider states remain future work. |
| Move structural validation into build phase | Done | Validator separation | Planner build phase performs no network I/O and no runtime mutation. |
| Move binding install/detach/render into commit phase | Not started | Planner output | Commit phase cancels inputs, detaches old bindings, installs new bindings, renders controls, and updates frames in order. |
| Add rollback/fallback handling for structural failures | Not started | Commit phase | Failed builds preserve the old page or enter a controlled fallback, never a half-cleared page. |
| Add unit tests for planner decisions | In progress | Planner extraction | Planner tests cover bound/unavailable controls, structural failures, dynamic children, retained metadata restore, and pure metadata snapshots. Pending, invalid-config, and sticky-provider cases remain future work. |

### 3. Introduce Action Availability Service

Goal: replace Beacon-backed binding-time action lookup with a local availability
cache fed by provider-direct state.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Define `ProviderActionKey` and availability records | Not started | Provider identity contracts | Cache records include provider instance, provider id, action id, state, descriptor, reason, timestamps, TTLs, and source. |
| Feed Beacon advertisements as candidates only | Not started | Existing action registry events | Beacon creates `unknown` or `probing` candidates, never authoritative `available` records. |
| Add direct availability request/snapshot messages | Not started | Provider protocol update | Controller can ask providers for availability for actions of interest. |
| Add provider availability change messages | Not started | Provider protocol update | Providers can publish action availability updates without Beacon churn. |
| Implement freshness and stale-grace expiry | Not started | Clock/test helpers | Fresh, stale, retired, and unavailable states transition deterministically. |
| Publish availability-change events to device runtimes | Not started | Runtime subscription path | Only affected devices/pages replan when availability changes. |
| Keep stale existing bindings stable during grace | Not started | Planner sticky selection | Existing bindings may remain bound while revalidation is pending according to policy. |

### 4. Implement Action Interest

Goal: track action need independently from button bindings and keep provider
resources warm without blocking layout.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Define configured action references | Not started | Config binding model | Action references include action id, optional provider instance, and provider label constraints. |
| Compute strong interest from visible/static/dynamic pages | Not started | Planner/frame state | Current page actions and dynamic page child actions are marked strongly needed. |
| Compute warm interest from active configs and recent use | Not started | Config snapshots and retention policy | Interest can outlive button bindings for the configured retention window. |
| Add interest retention and expiry policy | Not started | Clock/test helpers | Warm interest is retained for hours by policy and expires predictably. |
| Send interest updates to providers | Not started | Provider protocol update | Providers receive strong/warm interest updates without blocking page transitions. |
| Optional action-level Concord contract | Not started | Need/resource decision | Contracts, if added, are per provider/action interest and never a layout precondition. |

### 5. Provider Selection and Multiple Providers

Goal: support more than one provider serving the same logical action without
flapping or ad hoc selection.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Implement provider eligibility filtering | Not started | Availability cache | `provider_instance_id` and provider-label constraints filter candidates before ranking. |
| Add deterministic ranking policy | Not started | Availability records | Ranking uses existing selection, explicit instance, label strength, freshness, priority, then lexicographic tie-break. |
| Preserve sticky provider selections | Not started | Previous binding state | Existing bindings keep their provider while it remains available or stale-usable. |
| Rebind safely when selected provider changes | Not started | Input cancellation | Held inputs are cancelled, old action instances detach, and controls remain in layout. |
| Test multi-provider tie-breaks | Not started | Planner tests | Multiple providers for one action produce stable, repeatable selections. |

### 6. Dynamic Page Hardening

Goal: dynamic pages remain controller-owned even when provider availability
changes.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Represent dynamic pages as explicit frames | In progress | Page frame model | Dynamic descriptor, owner metadata, timeout, and committed plan are stored by the device runtime. |
| Allow unavailable child bindings | Partial | Planner outcomes | Dynamic page remains active even if one or more child actions are unavailable. |
| Close dynamic page after provider disappearance | In progress | Retained static frame | Closing restores the static page without requiring Beacon or provider availability. |
| Fire timeout after provider disappearance | Partial | Device-owned timeout loop | Timeout returns to the static page even if the owning provider is gone. |
| Reject replacement commands from missing/stale owners | Partial | Command authorization | Missing or stale provider sessions cannot mutate a dynamic page. |
| Preserve page close notification best-effort | Partial | Provider command routing | Runtime completes close locally even if notifying the owner is impossible. |

### 7. Command Authorization and Lifecycle Semantics

Goal: action commands remain tied to the selected provider/action context without
letting provider liveness own device state.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Keep sender/session authorization for active contexts | Partial | Existing message subject parsing | Stale sessions cannot issue commands for active bindings or page sessions. |
| Remove provider-session validity as layout authority | In progress | Provider-session gating removal | Session invalidity alone does not clear pages or revoke device state. |
| Define lifecycle rejection handling policy | Partial | Availability states | Retryable/unavailable rejections render unavailable without destroying layout. |
| Define explicit input cancel message or event | In progress | Provider API agreement | Providers can distinguish `cancel` from physical `up`. |
| Audit provider settings authorization | Partial | Settings target model | Settings commands require correct provider identity but do not depend on binding sessions. |

### 8. Regression Tests and Invariants

Goal: make the architecture difficult to regress.

| Invariant | Status | Required Tests |
| --- | --- | --- |
| Device ownership invariant | Not started | Availability/provider changes do not clear current static or dynamic page frames. |
| No remote wait invariant | Partial | Page transition tests with hanging provider calls/settings calls complete within bounded time. |
| Config prerequisite invariant | Not started | No action binding exists without an active config snapshot. |
| Unavailable action invariant | Partial | Missing/unavailable actions render unavailable while the page remains active. |
| Dynamic timeout invariant | Partial | Dynamic timeout fires after owner provider disappearance. |
| Return home invariant | In progress | Closing dynamic page restores cached static plan after Beacon withdrawal. |
| Input terminal invariant | In progress | Rebind/revoke while pressed sends `cancel`; later physical `up` is not delivered to the new binding. |
| Beacon candidate invariant | Not started | Beacon advertisements create candidates only, not authoritative availability. |
| Multiple provider invariant | Not started | Provider selection is deterministic and sticky. |
| Config disappearance invariant | Partial | Config removal releases bindings but keeps hardware connected. |
| Provider disappearance invariant | Partial | Provider disappearance changes availability, not device state. |
| Action interest invariant | Not started | Interest leases/contracts are provider/action scoped, not button scoped. |

## Suggested PR Sequence

1. Land the device-runtime stabilization slice.
   Landed in the first slice. Keep follow-up cleanup scoped to the same
   invariants until the planner is extracted.
2. Extract planner models and tests.
   Landed as `f698bdb`. Planner decisions now live in `_binding_planner.py`;
   `DeviceManager` still owns metadata refresh and commit-time mutation.
3. Add availability data models and a service skeleton.
   Recommended next. Introduce the local cache and snapshot API before
   provider-direct protocol work.
4. Connect Beacon as candidate input.
   Preserve existing discovery behavior but downgrade it from availability truth
   to candidate hints.
5. Add provider-direct availability protocol.
   Implement request/snapshot/change messages and wire provider-side support.
6. Add action interest tracking.
   Compute strong/warm interest from frames, configs, settings, and recent use.
7. Implement deterministic provider selection.
   Add sticky multi-provider selection and stale-grace behavior.
8. Replace transitional `manager.get_action()` planning calls.
   Planner should consult only availability snapshots/local cache.
9. Broaden regression tests around all invariants.
   Add failure-mode tests before deleting compatibility paths.

## Done Definition

The architecture is complete when all of these are true:

- Binding, page transitions, dynamic page close, timeout handling, and input
  routing perform no provider network I/O in their build path.
- Beacon is used only for discovery/candidate state.
- Action availability is answered from a local cache with freshness and stale
  grace policy.
- Every configured control on a valid page has an explicit planner outcome.
- Provider disappearance cannot clear device layout or strand a dynamic page.
- Dynamic pages close and time out without owner-provider availability.
- Every delivered input `down` receives exactly one terminal `up` or `cancel`.
- Multiple providers for the same action are selected deterministically and
  stickily.
- Config disappearance releases bindings but does not imply hardware
  disappearance.
- Regression tests cover every invariant listed above.

## Open Decisions

- Whether provider-direct availability should be request/response only,
  subscription/event based, or both.
- Whether ordinary action availability needs Concord at all, or whether Concord
  should be reserved for explicit resource commitments.
- Exact defaults for fresh TTL, stale grace TTL, warm interest retention, and
  provider revalidation interval.
- Whether stale available records can bind new controls or should render
  pending until refreshed.
- Whether dynamic page owner disappearance should shorten the page timeout.
- How provider priority should be represented in config or provider metadata.

## Maintenance Rules

- Update this plan in the same PR that changes architecture-level behavior.
- Prefer moving items from `Not started` to `In progress` only when code or tests
  exist.
- Mark an item `Done` only after tests protect the intended behavior.
- Add new invariants here before implementing large behavior changes.
- Keep implementation details in code comments or PR notes; keep this file at
  the project-planning level.
