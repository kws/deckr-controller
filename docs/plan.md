# Controller Runtime Architecture Implementation Plan

This document tracks implementation progress toward
[`docs/controller-runtime-architecture.md`](./controller-runtime-architecture.md).
Use the architecture document for design rationale and this file for sequencing,
ownership, and progress.

Last updated: 2026-06-16.

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
| Provider-session gating removal | Done | Binding/page transitions no longer wait on Concord provider-session readiness; endpoint sessions remain routing and authorization metadata. |
| Page frame model | In progress | Device runtime needs explicit static/dynamic frames with cached committed plans. |
| Held input cancellation | In progress | Rebinding must cancel old held inputs before releases are ignored. |
| Action availability service | Done | Local cache owns Beacon candidates, provider-direct records, freshness/stale policy, interest aggregation, provider requests, and availability-change notifications. |
| Binding planner extraction | Done | `_binding_planner.py` owns local planning decisions and outcomes, including pending and invalid-config states. |
| Action interest service | Done | Local tracker and controller service send provider action-interest updates for connected configs and visible page-frame interests. |
| Provider availability protocol | Done | Shared contracts and Python provider runtime support request/snapshot/change plus interest updates. |
| Multiple-provider selection | Partial | Deterministic fallback ranking exists; explicit priority metadata/config remains deferred. |

## Remaining Work

### 7. Provider Selection And Resource Contracts

Recommended commit title:

```text
Polish provider selection metadata and resource contracts
```

The provider-direct availability protocol is now implemented. Beacon remains
candidate discovery only; providers answer availability through
`actionAvailabilityRequest`, `actionAvailabilitySnapshot`, and
`actionAvailabilityChanged`, while controllers publish `actionInterestUpdate`.

Resolved decisions:

- Ordinary action availability uses provider-direct messages, not Concord.
- Concord is reserved for future explicit resource commitments or exclusivity.
- Warm interest retention defaults to 4 hours.
- Provider revalidation defaults to 60 seconds.
- Stale grace defaults to 5 minutes.
- Stale availability preserves existing bindings only; new bindings render
  pending until fresh availability arrives.
- Provider priority config remains out of scope; ranking uses deterministic
  fallback without a new config schema.

Remaining scope:

- Optional provider/action priority metadata and config syntax.
- Deeper provider resource contracts for actions that need explicit commitments.
- Final cleanup of standalone test helpers and any remaining implementation
  polish around provider ranking.

Suggested tests:

```bash
uv run ruff check .
uv run pytest tests/test_action_availability.py tests/test_action_interest.py tests/test_binding_planner.py tests/test_action_registry.py tests/test_device_manager_integration.py
git diff --check
```

## Milestones

### 1. Stabilize Device Runtime Boundaries

Goal: make the current `DeviceManager` behavior match the device-state
invariants before extracting services.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Remove provider-session readiness as a binding precondition | Done | Existing action provider routing metadata | Static and dynamic pages can commit without waiting for Concord/session readiness. |
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
| Model binding outcomes | Done | Availability state model | Planner models `bound`, `pending`, `unavailable`, `invalid_config`, and `invalid_device_control`. |
| Move structural validation into build phase | Done | Validator separation | Planner build phase performs no network I/O and no runtime mutation. |
| Move binding install/detach/render into commit phase | Not started | Planner output | Commit phase cancels inputs, detaches old bindings, installs new bindings, renders controls, and updates frames in order. |
| Add rollback/fallback handling for structural failures | Not started | Commit phase | Failed builds preserve the old page or enter a controlled fallback, never a half-cleared page. |
| Add unit tests for planner decisions | Done | Planner extraction | Planner tests cover bound/unavailable/pending controls, structural failures, dynamic children, retained metadata restore, and pure metadata snapshots. |

### 3. Introduce Action Availability Service

Goal: replace Beacon-backed binding-time action lookup with a local availability
cache fed by provider-direct state.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Define `ProviderActionKey` and availability records | Done | Provider identity contracts | Cache records include provider instance, action id, state, metadata, reason, timestamps, TTLs, and source. |
| Feed Beacon advertisements as candidates only | Done | Existing action registry events | Beacon records `unknown` or `probing` candidates, never authoritative `available` records; tests cover candidate-only snapshots and withdrawal. |
| Add direct availability request/snapshot messages | Done | Provider protocol update | Controller can ask providers for availability for actions of interest. |
| Add provider availability change messages | Done | Provider protocol update | Providers can publish action availability updates without Beacon churn. |
| Implement freshness and stale-grace expiry | Done | Clock/test helpers | Fresh, stale, and expired states transition deterministically in the local cache. |
| Publish availability-change events to device runtimes | Done | Runtime subscription path | Affected current controls replan in place when availability changes. |
| Keep stale existing bindings stable during grace | Done | Planner sticky selection | Existing bindings may remain bound during stale grace; new stale bindings render pending. |

### 4. Implement Action Interest

Goal: track action need independently from button bindings and keep provider
resources warm without blocking layout.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Define configured action references | Done | Config binding model | Action references include action id, optional provider instance, and provider label constraints. |
| Compute strong interest from visible/static/dynamic pages | Done | Planner/frame state | Connected config, current static page, and dynamic page child actions are marked strongly needed. |
| Compute warm interest from active configs and recent use | In progress | Config snapshots and retention policy | Removed strong interests are retained warm; recent-use/settings/prewarm sources remain future work. |
| Add interest retention and expiry policy | Done | Clock/test helpers | Warm interest is retained for hours by policy and expires predictably. |
| Send interest updates to providers | Done | Provider protocol update | Providers receive strong/warm interest updates without blocking page transitions. |
| Optional action-level Concord contract | Deferred | Need/resource decision | Contracts, if added, are per provider/action interest and never a layout precondition. |

### 5. Provider Selection and Multiple Providers

Goal: support more than one provider serving the same logical action without
flapping or ad hoc selection.

| Task | Status | Dependencies | Acceptance Criteria |
| --- | --- | --- | --- |
| Implement provider eligibility filtering | Done | Availability cache | `provider_instance_id` and provider-label constraints filter candidates before ranking. |
| Add deterministic ranking policy | Partial | Availability records | Current fallback ranking is deterministic; priority metadata/config remains future work. |
| Preserve sticky provider selections | Partial | Previous binding state | Existing bindings keep their provider while it remains available or stale-usable. |
| Rebind safely when selected provider changes | Done | Input cancellation | Held inputs are cancelled, old action instances detach, and controls remain in layout. |
| Test multi-provider tie-breaks | Partial | Planner tests | Cache tests cover deterministic fallback selection; priority/sticky-provider expansions remain future work. |

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
| Device ownership invariant | Partial | Availability/provider changes do not clear current static or dynamic page frames. |
| No remote wait invariant | Done | Page transition tests with hanging provider calls/settings calls complete within bounded time. |
| Config prerequisite invariant | Not started | No action binding exists without an active config snapshot. |
| Unavailable action invariant | Done | Missing/unavailable actions render unavailable while the page remains active. |
| Dynamic timeout invariant | Partial | Dynamic timeout fires after owner provider disappearance. |
| Return home invariant | Done | Closing dynamic page restores cached static plan after Beacon withdrawal. |
| Input terminal invariant | In progress | Rebind/revoke while pressed sends `cancel`; later physical `up` is not delivered to the new binding. |
| Beacon candidate invariant | Done | Beacon advertisements create candidates only, not authoritative availability. |
| Multiple provider invariant | Not started | Provider selection is deterministic and sticky. |
| Config disappearance invariant | Partial | Config removal releases bindings but keeps hardware connected. |
| Provider disappearance invariant | Partial | Provider disappearance changes availability, not device state. |
| Action interest invariant | Done | Interest updates are provider/action scoped, not button scoped. |

## Suggested PR Sequence

1. Land the device-runtime stabilization slice.
   Landed in the first slice. Keep follow-up cleanup scoped to the same
   invariants until the planner is extracted.
2. Extract planner models and tests.
   Landed as `f698bdb`. Planner decisions now live in `_binding_planner.py`;
   `DeviceManager` still owns metadata refresh and commit-time mutation.
3. Add availability data models and a service skeleton.
   Landed as `1f350dc`. `_action_availability.py` now provides provider/action
   records, freshness policy, and planner snapshot conversion.
4. Add action interest tracking.
   Landed in the local controller slice. `_action_interest.py` tracks
   strong/warm interest from connected configs and page frames before
   provider-direct availability work.
5. Connect Beacon as candidate input.
   Landed in this slice. Beacon metadata now records candidate state only;
   provider-direct records now supply authoritative availability.
6. Add provider-direct availability protocol.
   Landed in this slice. Shared action-lane contracts, Python provider runtime
   support, controller availability ingestion, interest updates, revalidation,
   and in-place replanning are wired.
7. Implement deterministic provider selection.
   Deterministic fallback and stale-existing behavior are in place. Optional
   priority metadata/config remains future work.
8. Replace transitional `manager.get_action()` planning calls.
   Production `DeviceManager` instances with the controller availability service
   plan from availability snapshots/local cache. Standalone test-helper lookup
   compatibility remains for direct `DeviceManager` construction.
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

- Whether dynamic page owner disappearance should shorten the page timeout.
- How provider priority should be represented in config or provider metadata.
- Which provider resource commitments deserve explicit action-level Concord
  contracts.

## Maintenance Rules

- Update this plan in the same PR that changes architecture-level behavior.
- Prefer moving items from `Not started` to `In progress` only when code or tests
  exist.
- Mark an item `Done` only after tests protect the intended behavior.
- Add new invariants here before implementing large behavior changes.
- Keep implementation details in code comments or PR notes; keep this file at
  the project-planning level.
