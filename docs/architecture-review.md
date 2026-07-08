I reviewed the `project/v1-contract` branch through the GitHub source view and the existing proposed runtime architecture document. I did not run the code locally, so this is a source-level architectural review rather than a test-run report. The branch already has several of the right primitives: `ActionAvailabilityService` / `ActionAvailabilityCache`, `BindingPlanner`, `ControlAttachmentState`, `RenderDispatcher`, and `DeviceManager` page-frame state. The problem is that these primitives need to be made into one explicit state contract, with stronger separation between “this control is configured to do X” and “the remote action implementation for X is currently available.” The repository’s own architecture document is explicitly marked as proposed, not guaranteed to describe the current implementation, so I would turn the spec below into the normative document for this branch. ([GitHub][1])

# Control ↔ Action State Management Specification

## 1. Core principle

A **control binding** is controller-owned UI state. An **action** is remote executable behavior. These must never be collapsed into the same state.

A configured key remains a valid configured key even when its action is offline. In that case the control is still present in the page layout, but it has no live action attachment and renders `unavailable`. When the action returns, the same configured binding becomes live again without requiring a page rebuild, config reload, or device reconnect.

The runtime has four separate state domains:

| Domain                    |                   Owner | Meaning                                              | Must not imply           |
| ------------------------- | ----------------------: | ---------------------------------------------------- | ------------------------ |
| Device/page state         |              Controller | Which page/frame is visible and which controls exist | Action availability      |
| Config binding state      |       Controller/config | Which action a control wants                         | Remote provider liveness |
| Action availability state | Provider-observed cache | Whether a provider can currently serve an action     | Control/page ownership   |
| Action attachment state   |   Controller + provider | A live runtime context for one available binding     | Config validity          |

The existing architecture doc already states the important separation: device state and action availability are independent, and config joins them only at binding time. That should become a hard invariant, not just a design intention. ([GitHub][1])

## 2. Required runtime objects

### 2.1 `ControlSlotState`

Introduce an explicit per-visible-control slot state. This should exist even when no action is attached.

```python
@dataclass
class ControlSlotState:
    frame_id: str
    control_id: str
    binding_id: str | None
    configured_action: ActionIntentKey | None
    status: ControlBindingStatus
    selected_provider_key: ProviderActionKey | None
    attachment_id: str | None
    last_render_kind: Literal[
        "blank",
        "controller_pending",
        "controller_unavailable",
        "controller_invalid",
        "action_output",
    ]
```

This is the state of “what the controller believes belongs on this key.”

### 2.2 `ActionAttachment`

Rename or split the current `BindingLease` concept. A lease should mean “there is a live provider context,” not “this control is part of the page.” The current `BindingLease` contains both control identity and provider/action runtime identity, while `ControlAttachmentState` tracks active input, output, command authority, and held input. That is useful, but unavailable controls should not need a fake lease just to exist in the layout. ([GitHub][2])

```python
@dataclass
class ActionAttachment:
    attachment_id: str
    binding_id: str
    control_id: str
    context_id: str
    action_instance_id: str
    provider_action_key: ProviderActionKey
    provider_id: str
    provider_session_id: str | None
    input_caps: tuple[str, ...]
    output_caps: tuple[str, ...]
    command_generation: int
    output_generation: int
```

`ControlSlotState` is persistent for a visible page. `ActionAttachment` is optional and exists only in `BOUND`.

### 2.3 `ProviderActionKey`

Current availability keys are effectively `provider_instance_id + action_uuid`. I would expand the identity model to include `provider_id` as part of the stable provider identity, while keeping `provider_session_id` as an epoch/version used to reject stale messages rather than as the logical key. The current code already records provider ID and session ID in lifecycle-unavailable paths, but `ProviderActionKey` itself is narrower. ([GitHub][3])

```python
@dataclass(frozen=True)
class ProviderActionKey:
    provider_instance_id: str
    provider_id: str
    action_uuid: str
```

Session identity should live on the availability record:

```python
@dataclass
class ActionAvailabilityRecord:
    key: ProviderActionKey
    state: ActionAvailabilityState
    source: AvailabilitySource
    provider_session_id: str | None
    metadata: ActionMetadata | None
    observed_at: float
    fresh_until: float
    stale_until: float
    reason: str | None
```

This prevents accidental conflation if a provider instance ID is reused or if stale provider messages arrive after a provider restart.

## 3. Binding states

`BindingPlanner` already has the right rough statuses: `BOUND`, `PENDING`, `UNAVAILABLE`, `INVALID_CONFIG`, and `INVALID_DEVICE_CONTROL`. Those should be treated as the public state machine for a visible control. ([GitHub][4])

| State                    | Meaning                                                          | Render                                 | Input behavior         | Provider context |
| ------------------------ | ---------------------------------------------------------------- | -------------------------------------- | ---------------------- | ---------------- |
| `UNCONFIGURED`           | No binding for this visible control                              | blank / default                        | ignored                | none             |
| `INVALID_DEVICE_CONTROL` | Config refers to missing/unsupported control or capability       | invalid                                | ignored                | none             |
| `INVALID_CONFIG`         | Binding descriptor is malformed or semantically invalid          | invalid                                | ignored                | none             |
| `PENDING`                | Provider/action may exist but is not currently proven executable | pending                                | ignored or local no-op | none             |
| `UNAVAILABLE`            | No usable provider/action is currently available                 | unavailable                            | ignored or local no-op | none             |
| `BOUND`                  | A provider/action has been selected and attached                 | action output                          | routed to action       | live attachment  |
| `DETACHING`              | Commit-only transient while replacing/revoking attachment        | previous output or controller fallback | cancel held inputs     | being revoked    |

Important distinction: `PENDING` is for “we have a reason to probe or wait”; `UNAVAILABLE` is for “there is no currently usable action.” Neither state invalidates the page.

## 4. Availability semantics

Availability is local, cached, and observed. Page binding must never synchronously wait on Beacon, provider discovery, Concord session setup, provider attach, or action handshake. The proposed architecture document already calls this out: binding, page transitions, dynamic page open/close, timeout, and input routing must not wait on provider liveness or discovery. ([GitHub][1])

The current `ActionAvailabilityCache` distinguishes Beacon service candidates from service-view availability, and the proposed architecture describes Beacon as discovery-only rather than authoritative availability. That direction is correct. ([GitHub][3])

Availability states should be interpreted as follows:

| Availability state                | Binding result for new binding                         | Existing bound binding                                                           |
| --------------------------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `UNKNOWN`                         | `PENDING` if candidate exists, otherwise `UNAVAILABLE` | keep only if previous attachment still alive                                     |
| `PROBING`                         | `PENDING`                                              | keep if attachment still alive                                                   |
| `AVAILABLE` service view          | `BOUND`                                                | `BOUND` / refresh metadata                                                       |
| `STALE`                           | `PENDING` for new bindings                             | opt-in custom policy only; existing bindings may remain `BOUND` while the attachment is still alive |
| `UNAVAILABLE` service view        | `UNAVAILABLE`                                          | cancel inputs, detach, render unavailable                                        |
| `EXPIRED` / `RETIRED`             | `UNAVAILABLE`                                          | cancel inputs, detach, render unavailable                                        |

Beacon disappearance alone must not be treated as authoritative action unavailability. Service-view `UNAVAILABLE`, missing service view, provider session death, terminal lifecycle rejection, or an explicit provider/catalog retirement is authoritative for action projection. Missing service view does not close the service-use contract; the watch remains open for a later payload.

## 5. Action interest is not binding

Action interest is a demand signal to providers, not a control binding.

The existing branch has an `ActionInterestTracker`, and `DeviceManager` derives interests from connected config and the current visible/dynamic page. That is the right category of mechanism, but it must remain advisory. Interest may cause provider probing, warming, metadata refresh, or subscription setup; it must not create a binding and must not block page rendering. ([GitHub][5])

Recommended interest classes:

| Interest class | Source                                                                              | Expected provider behavior                         |
| -------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------- |
| `STRONG`       | Currently visible static page, currently visible dynamic page, active dynamic owner | keep availability fresh; notify changes promptly   |
| `WARM`         | Other pages in connected config, recently visible pages, settings UI                | keep low-cost metadata or availability if possible |
| `PROBE`        | Beacon/catalog candidate, unresolved configured action                              | answer availability once; do not imply binding     |

A configured action can have interest without being visible. A visible control can be unavailable even while it has strong interest. Those facts must not conflict.

## 6. Provider selection

Provider selection belongs to the binding plan, not to action availability itself.

The availability cache can return candidate records and metadata, but the selected provider should be stored on the `ControlSlotState` or page-frame plan. The current cache tracks selected records by intent, which risks coupling multiple controls that happen to refer to the same action intent but have different page, settings, or dynamic context. ([GitHub][3])

Selection order should be deterministic:

1. Explicit `provider_instance_id` in the binding.
2. Existing selected provider for the same control slot, if still usable.
3. Provider labels match, if labels are specified.
4. Current service-view availability beats missing or stale local knowledge.
5. Provider priority, if contract supports it.
6. Stable lexical tie-breaker: `(provider_instance_id, provider_id, action_uuid)`.

A provider switch is a detach/attach transition. It must cancel held inputs for the old attachment, revoke output/command authority, then attach the new provider.

## 7. Planning and commit protocol

The binding planner should be pure and local. The branch already has a `BindingPlanner` that produces `PagePlan` / `PlannedBinding` values and binding statuses, which is the right boundary. ([GitHub][4])

### 7.1 Build phase

The build phase reads:

```text
device descriptor
+ config snapshot
+ target page frame
+ availability planning snapshot
+ previous committed frame/slot state
= proposed page plan
```

The build phase must not:

```text
query Beacon
open provider session
wait for Concord
send provider lifecycle messages
create action contexts
mutate current frame stack
clear or render controls
```

The build result should include:

```python
@dataclass
class PagePlan:
    frame_id: str
    slots: dict[str, ControlSlotState]
    attachments_to_keep: set[str]
    attachments_to_create: list[AttachmentCreateSpec]
    attachments_to_revoke: list[AttachmentRevokeSpec]
    render_ops: list[RenderOp]
    validation_errors: list[BindingValidationError]
```

### 7.2 Commit phase

The commit phase is the only place that mutates runtime state.

Commit order must be:

1. Acquire the device navigation/state lock.
2. Validate that the base frame/config generation has not changed.
3. Cancel held inputs for attachments that will be revoked.
4. Revoke command/output authority for outgoing attachments.
5. Notify provider detach best-effort; never block page transition indefinitely.
6. Create/attach new action contexts for `BOUND` slots only.
7. Install new `ControlSlotState` map atomically.
8. Install new attachment map atomically.
9. Render controller-owned fallback states: invalid, pending, unavailable.
10. Allow action-owned renders only after output authority generation is installed.
11. Update frame stack and current frame pointer.
12. Publish action interest snapshot.

The existing `ControlAttachmentState` and `RenderDispatcher` already have the right idea of generation/authority checks to reject stale output, and `DeviceManager` already cancels held inputs in revocation paths. Those should become mandatory commit invariants. ([GitHub][2])

## 8. Dynamic page model

A dynamic page is not owned by the provider after it opens. It is a controller-owned frame created from a provider-supplied descriptor.

Each dynamic frame must store:

```python
@dataclass
class DynamicPageFrame:
    frame_id: str
    page_id: str
    page_session_id: str
    descriptor_snapshot: DynamicPageCommand
    owner_action_key: ProviderActionKey
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    invoking_frame_id: str
    opened_at: float
    last_activity_at: float
    timeout_ms: int
    close_reason: str | None
```

The current `DynamicPageSession` already records owner action/provider/session/context/binding/control fields, which is good, but I would add an explicit `invoking_frame_id` or return-frame pointer. `owner_profile` / `owner_page` is not enough once dynamic pages can nest or once a static page changes beneath an open dynamic frame. ([GitHub][4])

### 8.1 Opening a dynamic page

An action may request a dynamic page only from a live, command-authorized context. On open:

1. Validate the command sender matches an active attachment.
2. Validate the descriptor against the current device capabilities.
3. Capture the current top frame as `invoking_frame_id`.
4. Freeze the descriptor into a controller-owned dynamic frame.
5. Build the dynamic page plan locally.
6. Commit it transactionally.
7. Notify the owner action best-effort that the page session opened.

Child bindings inside the dynamic page resolve independently. If a child action is unavailable, that child control renders `unavailable`; the page remains open.

### 8.2 Closing a dynamic page

A dynamic page close must not depend on the owning provider still being alive. The proposed architecture document already correctly says close/timeout must be controller-owned and must not require Beacon, provider liveness, child actions, or a provider contract. ([GitHub][1])

Close algorithm:

1. Identify the target dynamic frame.
2. Pop that frame and all descendants.
3. Build the return plan for the nearest surviving invoking frame.
4. Cancel held inputs for all popped-frame attachments.
5. Revoke popped-frame attachments.
6. Commit the return frame.
7. Render the return frame’s current statuses.
8. Notify page-session-closed best-effort if owner context still exists.

Do not clear the active dynamic-session pointer before the return plan has committed. The architecture doc notes this exact class of bug: clearing `_dynamic_page_session` before navigating home makes close behavior depend on mutated state rather than a transactional page-stack update. ([GitHub][1])

## 9. Critical requirement: owner action unavailable closes dynamic page

This is the one place where I would make the spec stricter than the existing proposed architecture document. The proposed doc says a dynamic frame may remain controller-owned if the owning provider disappears and can later be closed by timeout. Your product requirement is different and should win: **if the action that opened a dynamic page becomes unavailable, the dynamic page must close immediately and return to the invoking page.** The existing doc’s behavior should be changed. ([GitHub][1])

Normative rule:

```text
If a dynamic frame’s owner action transitions to authoritative unavailable,
the controller MUST close that dynamic frame and all descendant dynamic frames,
then return to the frame that invoked it.
```

Authoritative unavailable means one of:

```text
service-view unavailable
provider/action retired
provider session rejected or terminal lifecycle rejection
custom stale grace expired
action instance destroyed without replacement
provider removed with no replacement after policy grace
```

Authoritative unavailable does **not** mean:

```text
Beacon candidate temporarily absent
availability probe in flight
metadata stale but existing owner attachment still alive
network discovery lag without service-view confirmation
```

Nested dynamic behavior:

```text
Static A opens Dynamic B by Action X.
Dynamic B opens Dynamic C by Action Y.

If Y becomes unavailable:
    close C, return to B.

If X becomes unavailable while C is visible:
    close C and B, return to A.

If a non-owner child action on B becomes unavailable:
    keep B open; render only that child control unavailable.
```

Implementation hook:

```python
async def on_action_availability_changed(changed_keys: set[ProviderActionKey]) -> None:
    async with self._nav_lock:
        lost_owner_frames = [
            frame for frame in self._page_frames
            if frame.is_dynamic
            and frame.owner_action_key in changed_keys
            and self._availability.is_authoritatively_unavailable(frame.owner_action_key)
        ]

        if lost_owner_frames:
            first_lost = earliest_frame_in_stack(lost_owner_frames)
            await self._close_dynamic_frame_and_descendants(
                first_lost.frame_id,
                reason="owner_action_unavailable",
            )

        await self._replan_visible_frame_for_availability_change(changed_keys)
```

The existing `ControllerService` already fans availability changes out to device managers, and `DeviceManager` already has logic to determine whether changed availability affects a plan. The missing piece is the owner-frame close decision before ordinary replan. ([GitHub][6])

## 10. Input invariants

Input routing must be attachment-based, not slot-based.

Rules:

1. `BOUND` controls route input to their current attachment.
2. `PENDING`, `UNAVAILABLE`, and invalid controls do not send input to any provider.
3. If a physical `down` was delivered to an attachment, that same attachment must receive exactly one terminal `up` or `cancel`.
4. If a binding is revoked while pressed, send `cancel` to the old attachment.
5. A physical `up` after rebind must not be delivered to the new attachment if the new attachment did not receive the corresponding `down`.

The existing attachment state and device manager revocation code already track held inputs and send cancel events; keep that behavior and make it universal for every detach/replan/page-close path. ([GitHub][2])

## 11. Rendering invariants

Rendering authority must follow binding authority.

| Source                                        | Allowed when                                        |
| --------------------------------------------- | --------------------------------------------------- |
| Controller unavailable/pending/invalid render | Slot is not `BOUND`                                 |
| Action output render                          | Attachment is current and output generation matches |
| Old action output                             | Must be dropped after detach/rebind                 |
| Page close clear/render                       | Must be generation-authorized                       |

The current `RenderDispatcher` checks generation/context/binding authorization before applying render results; this should remain the only path by which asynchronous action output reaches hardware. ([GitHub][7])

On transition to unavailable:

```text
BOUND -> UNAVAILABLE:
    cancel held input
    revoke command authority
    revoke output authority
    detach/destroy action context best-effort
    clear or supersede old action output
    render controller-owned unavailable state
```

On transition back to available:

```text
UNAVAILABLE -> BOUND:
    select provider deterministically
    create action attachment
    install command/output authority
    request initial render/refresh
```

## 12. Page-stack ownership

Unify navigation state into one service/object.

Right now the branch has both `NavigationService` and `DeviceManager`-owned `_page_frames` / `_dynamic_page_session` state. The navigation service’s source says it owns current page transitions without a stack, while `DeviceManager` maintains page frames and syncs top-frame dynamic session state. That split invites the exact bugs you described. ([GitHub][8])

Recommended rewrite:

```text
DeviceManager owns device I/O and commit orchestration.
FrameStack owns static/dynamic page stack and return-frame semantics.
BindingPlanner builds plans for a requested frame.
AttachmentManager owns live provider attachments.
AvailabilityService owns provider/action availability cache.
```

There should be no separate `_dynamic_page_session` mutable source of truth. It can be a derived property:

```python
@property
def active_dynamic_session(self) -> DynamicPageSession | None:
    top = self.frame_stack.top
    return top.page_session if top.is_dynamic else None
```

## 13. Required invariants

These are the invariants I would put directly into tests and comments.

1. A configured binding never disappears solely because its action is unavailable.
2. An unavailable configured binding renders `unavailable`, not blank.
3. Page navigation never waits on provider discovery or provider attach.
4. Beacon candidate state is never authoritative executable state.
5. Provider-direct availability is authoritative for binding.
6. Action interest never implies a control binding.
7. A dynamic page descriptor becomes controller-owned after open.
8. A dynamic page close never requires the owner provider to be alive.
9. Owner action authoritative unavailability closes its dynamic page and descendants.
10. Non-owner child action unavailability does not close the dynamic page.
11. Every delivered input `down` receives exactly one `up` or `cancel`.
12. Stale action output is dropped after detach/rebind/page close.
13. Static page state survives action provider churn.
14. Config removal/device detach revokes provider attachments but does not corrupt availability cache.
15. Provider selection is deterministic and sticky per control slot, not globally per action intent.

The existing proposed architecture doc already lists a similar invariant set; I would amend it with the owner-unavailable dynamic-page rule and the explicit `ControlSlotState` / `ActionAttachment` split. ([GitHub][1])

## 14. Concrete refactor recommendations

### 14.1 Split slot state from attachment state

Keep `ControlAttachmentState`, but narrow it to live attachments, held inputs, output generation, and command authority. Add `ControlSlotState` as the controller-owned page layout state.

This is the most important cleanup. It makes unavailable controls first-class instead of treating them as failed bindings.

### 14.2 Replace dynamic-session shadow state with a frame stack

Make `FrameStack` the only source of truth for:

```text
current static page
dynamic frames
invoking frame
active page session
dynamic timeout
close reason
```

`_dynamic_page_session` should be deleted or derived from the top frame.

### 14.3 Move sticky provider selection out of availability cache

The availability cache should answer “what providers are currently usable for this action intent?” The planner/frame state should answer “which provider did this control select last time?”

This prevents one control’s provider choice from unexpectedly affecting another control with the same action intent.

### 14.4 Add explicit owner-loss close path

Before normal availability replan, detect dynamic frames whose owner action is authoritatively unavailable. Close those frames first, then replan the revealed frame.

This directly implements the behavior you described.

### 14.5 Treat provider lifecycle rejection as availability input

The branch already records lifecycle-unavailable for bindings/action instances/page sessions. That is good. Make those records feed the same availability event path as service-view `UNAVAILABLE`, including dynamic owner close. ([GitHub][5])

### 14.6 Make page open/close fully transactional

Page open and close should build the target plan before mutating the active stack. If build fails, keep the current frame. If commit fails midway, fall back to a controlled invalid/unavailable render, not a half-popped stack.

## 15. Regression tests to add

I would add these tests before rewriting more code:

1. **Static unavailable render**: static page has three controls; one action unavailable; page renders two bound controls and one unavailable control.
2. **Unavailable recovers**: unavailable action becomes available; same control attaches without page navigation.
3. **Available drops**: bound action becomes unavailable; input is cancelled, attachment removed, control renders unavailable.
4. **Beacon loss is not unavailability**: Beacon candidate disappears; existing service-view bound action remains bound until service-view policy says otherwise.
5. **Dynamic owner unavailable**: action opens dynamic page; owner action becomes unavailable; page closes to invoking frame.
6. **Dynamic child unavailable**: dynamic page child action becomes unavailable; page remains open; only child control renders unavailable.
7. **Nested dynamic owner unavailable**: A opens B, B opens C; owner of B disappears; both B and C close, static A is visible.
8. **Held input on page close**: key down on dynamic page, owner unavailable closes page, old action receives `cancel`, static page does not receive stray `up`.
9. **Provider returns after dynamic close**: owner provider returns after forced close; static invoking page remains visible; dynamic page is not resurrected.
10. **Provider switch**: selected provider dies, another matching provider is available; old attachment cancels/detaches; new attachment binds deterministically.
11. **Config change while dynamic open**: config changes under dynamic page; close returns to nearest valid invoking frame or controlled fallback.
12. **Stale output rejection**: old action render completes after detach; render dispatcher drops it.

## Bottom line

The branch is close in terms of raw components, but the architecture should be made stricter:

```text
ControlSlotState is the persistent UI truth.
ActionAttachment is optional remote executable state.
Availability is provider-observed cache state.
ActionInterest is only a demand signal.
DynamicPageFrame is controller-owned, with an explicit invoking frame.
Owner action unavailability closes its dynamic frame immediately.
```

That model directly addresses the confusion between action availability and control bindings, and it gives dynamic pages a single state machine instead of many interacting partial states.

[1]: https://github.com/kws/deckr-controller/blob/project/v1-contract/docs/controller-runtime-architecture.md "deckr-controller/docs/controller-runtime-architecture.md at project/v1-contract · kws/deckr-controller · GitHub"
[2]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_control_attachment_state.py "raw.githubusercontent.com"
[3]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_action_availability.py "raw.githubusercontent.com"
[4]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_binding_planner.py "raw.githubusercontent.com"
[5]: https://github.com/kws/deckr-controller/blob/project/v1-contract/src/deckr/controller/_device_manager.py "deckr-controller/src/deckr/controller/_device_manager.py at project/v1-contract · kws/deckr-controller · GitHub"
[6]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_controller_service.py "raw.githubusercontent.com"
[7]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_render_dispatcher.py "raw.githubusercontent.com"
[8]: https://github.com/kws/deckr-controller/raw/refs/heads/project/v1-contract/src/deckr/controller/_navigation_service.py "raw.githubusercontent.com"
