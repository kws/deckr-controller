# Controller Runtime Architecture: Device State, Configuration, and Action Availability

THIS IS A PROPOSED ARCHITECTURE, NOT CURRENT IMPLEMENTATION. The current code has the rough pieces but does not yet fully realize these boundaries and responsibilities.

## Status

Proposed intended architecture for the monolithic Deckr controller runtime.

This document defines the separation of responsibilities between:

1. **Device state** — the controller’s view of hardware, layout, navigation, rendering, and input ownership.
2. **Configuration state** — the dynamic glue that decides which devices should bind to which actions.
3. **Action availability** — the runtime availability of actions served by transient remote action providers.

The key rule is:

> **Device state and action availability are independent concerns. Configuration joins them, but action availability must never be allowed to make the device unresponsive or lose control of layout.**

The controller may run as a monolith, but devices and action providers are remote and transient. A live device can exist without a config. A config can exist without a live device. An action can be referenced by config while no provider currently serves it. All of these states are valid and must be represented explicitly.

The current code already has the rough pieces: device config is dynamic and subscribed by config id, with `None` used to represent config removal; config matching is based on device fingerprint and labels; device configs contain profiles, pages, controls, action ids, optional provider constraints, and widget timeouts.    The intended architecture below makes those responsibilities explicit and prevents action-provider volatility from destabilizing the device runtime.

---

# 1. Core Principles

## 1.1 Device layout is controller-owned

Once a device is claimed by the controller, the controller owns:

* the current profile/page
* dynamic page stack
* dynamic page timeout
* raster output state
* input routing
* held-input ownership
* fallback/unavailable rendering
* device clear/sleep/wake policy
* recovery from action-provider loss

Action providers may **request** dynamic pages, output updates, overlays, settings updates, or page closure. They do not own the device’s navigation state.

The controller may accept an action provider’s dynamic page descriptor, but once accepted, that descriptor becomes a controller-owned page frame. The device runtime must be able to close that frame by timeout or navigation even if the originating provider disappears.

## 1.2 Action availability is provider-owned, but controller-observed

An action is available only if at least one live action provider currently serves that action and confirms it is usable.

Beacon is not real-time action availability. Beacon can discover candidate providers and advertised capabilities, but it must not be treated as authoritative for whether an action can currently execute. The current branch’s registry is Beacon-backed and `get_action()` returns metadata from current Beacon-derived registry state.  That is useful for discovery, but not sufficient as the binding-time availability authority.

## 1.3 Configuration is the glue

A binding can exist only when:

```text
live device control
+ available configuration
+ resolved action reference
+ selected provider availability
= executable binding
```

Configuration is dynamic. It can appear, change, or disappear at any time. Available configuration is a precondition for device-to-action binding.

A device without matching config should remain connected and controllable by the controller, but it has no action bindings. A config without a live device should contribute to desired action interest only if the controller chooses to prewarm that config; it should not create bindings.

## 1.4 Binding must be local and non-blocking

Button binding, page transitions, dynamic page open/close, timeout handling, and input routing must never synchronously wait for:

* Beacon reconciliation
* provider discovery
* provider liveness confirmation
* Concord agreement creation
* action availability handshake
* remote provider startup

The controller may render a control as unavailable, pending, or checking. It must not leave the device half-bound or unbound because an action provider is slow or transient.

This is the main architectural correction. The branch currently makes binding activation depend on provider-session readiness: `_try_resolve_binding()` creates a pending `BindingLease`, and `_activate_binding()` only attaches it when the provider session is ready.   That puts distributed liveness directly in the hot path. The intended design moves liveness into an availability cache and treats unavailable actions as a renderable state, not as failure to own the device layout.

## 1.5 Interest in an action is not the same as a button binding

The controller may decide that an action is “needed” because it is referenced by:

* the current visible page
* a nearby page in the same profile
* an open dynamic page
* a connected device’s active config
* a recently active config
* a settings panel
* a warm-cache policy

That interest can be maintained for hours after last use. It should not be tied to a specific button binding. A button binding is a UI/control concern; action interest is a provider-readiness concern.

---

# 2. Responsibility Boundaries

## 2.1 Device State Service / Device Runtime

The device runtime owns all controller-local state for a claimed hardware device.

Responsibilities:

* Track hardware route, descriptor, manager session, and connection state.
* Match the live device to configuration.
* Maintain the active static page frame.
* Maintain a stack or overlay frame for dynamic pages.
* Own dynamic page timeout.
* Own all raster output for controls.
* Own input routing and held-input capture.
* Compute and commit page plans.
* Render unavailable/pending states when actions are absent.
* Cancel or synthesize terminal input when bindings change during a press.
* Clear outputs and release bindings when config disappears or device disconnects.

Non-responsibilities:

* Discovering action providers.
* Deciding global action availability.
* Maintaining provider-level action-interest leases.
* Treating Beacon advertisements as executable action truth.

Existing `DeviceManager` already owns dynamic page session state and timeout checks. It stores `_dynamic_page_session`, runs `_page_timeout_loop()`, and calls `close_page()` when elapsed time exceeds the configured timeout.   This ownership should remain in the device layer.

## 2.2 Configuration Service

The configuration service owns dynamic config state.

Responsibilities:

* Load, validate, and publish `DeviceConfig`.
* Match devices by fingerprint and labels.
* Notify device runtimes when config appears, changes, or disappears.
* Preserve config identity and versioning.
* Expose config references to the action-interest engine.
* Act as the source of truth for static profiles/pages/control-to-action references.

The current config service already exposes `match_device()`, `get_config()`, `write_config()`, and `subscribe()`, where subscribers receive the current config and then full config updates or `None` on removal.  Its file-backed implementation watches YAML files, updates caches, and notifies subscribers when config ids are affected. 

Non-responsibilities:

* Reporting whether an action is currently executable.
* Claiming a provider.
* Blocking device layout on provider availability.

## 2.3 Provider Discovery

Provider discovery identifies candidate action providers. Beacon is appropriate here.

Responsibilities:

* Discover provider instances.
* Record provider instance id, provider id, endpoint, labels, advertised action descriptors, and discovery revision/session metadata.
* Feed candidates into the action availability service.
* Remove candidate providers after discovery TTL or explicit disappearance.

Non-responsibilities:

* Authoritatively declaring an action available.
* Driving device page transitions.
* Invalidating existing device layouts directly.
* Acting as a binding precondition.

Beacon should answer:

```text
“Which providers might serve which actions?”
```

It should not answer:

```text
“Can this action execute right now?”
```

## 2.4 Action Availability Service

The action availability service owns real-time provider/action state.

Responsibilities:

* Maintain a local availability cache.
* Query providers directly for actions of interest.
* Subscribe to provider availability changes.
* Track multiple providers for the same action.
* Select the best provider for a configured action reference.
* Preserve sticky provider selection for existing bindings where possible.
* Expire stale availability using policy.
* Compute changed provider/action keys for provider and catalog updates.
* Manage optional action-interest leases/contracts.

Non-responsibilities:

* Owning device layout.
* Owning dynamic page timeout.
* Blocking binding or page transitions.
* Revoking device pages directly.

The availability service should answer:

```text
“For this configured action reference, which provider, if any, is currently usable?”
```

It should expose this answer through a local cache lookup. Device binding code can then be deterministic and fast.

## 2.5 Binding Planner

The binding planner joins device, config, and action availability.

Inputs:

* Device descriptor.
* Current config snapshot.
* Current page frame.
* Dynamic page frame, if any.
* Local action availability cache.
* Existing binding state, for sticky provider selection and input safety.

Outputs:

* A `PagePlan`.
* Per-control binding decisions:

  * `bound`
  * `unavailable`
  * `pending`
  * `invalid_config`
  * `invalid_device_control`

The planner does not perform network I/O.

A page can be valid even if some actions are unavailable. In that case, the controller still owns the layout and renders unavailable/pending placeholders. The current validator already distinguishes blocking selector/capability errors from non-blocking `action_not_found`.   The intended architecture should preserve that concept but avoid treating Beacon absence as final action absence.

---

# 3. The Three Independent State Domains

## 3.1 Device State

Device state is keyed by the hardware device reference.

Suggested model:

```python
@dataclass(frozen=True)
class DeviceIdentity:
    manager_id: str
    device_id: str

@dataclass
class DeviceRuntimeState:
    identity: DeviceIdentity
    descriptor: DeviceDescriptor
    route_state: Literal["claiming", "live", "stale", "disconnected"]
    manager_session_id: str | None
    config_id: str | None
    config_version: str | None
    current_static_frame: StaticPageFrame | None
    dynamic_frame: DynamicPageFrame | None
    control_bindings: dict[str, ControlBindingState]
    held_inputs: dict[InputKey, HeldInput]
```

Device state continues to exist while:

* action providers appear or disappear
* actions become available or unavailable
* Beacon is delayed or stale
* a dynamic page owner disappears
* config is being refreshed

Device state ends only when the device is disconnected/released or the controller stops.

## 3.2 Configuration State

Configuration state is keyed by config id.

Suggested model:

```python
@dataclass(frozen=True)
class ConfigSnapshot:
    config_id: str
    version: str
    enabled: bool
    match: DeviceConfigMatch
    profiles: tuple[Profile, ...]
    provider_settings: Mapping[str, Mapping[str, Any]]
```

Important rules:

* A config snapshot can appear at any time.
* A config snapshot can change at any time.
* A config snapshot can disappear at any time.
* A live device can be unmatched.
* A matched config is required before action bindings can be created.
* Removing config must remove bindings but not imply that the hardware device disappeared.

On config change, the device runtime should compute a new page plan from the new config and commit it transactionally. If the config disappears, the device enters an “unconfigured” layout state and releases action bindings.

## 3.3 Action Availability State

Action availability is keyed by provider/action, not by button binding.

Suggested model:

```python
@dataclass(frozen=True)
class ProviderActionKey:
    provider_instance_id: str
    action_uuid: str

@dataclass
class ActionAvailabilityRecord:
    key: ProviderActionKey
    state: Literal[
        "unknown",
        "probing",
        "available",
        "unavailable",
        "stale",
        "expired",
    ]
    source: Literal["beacon_candidate", "service_view"]
    updated_at: float
    metadata: ActionMetadata | None
    reason: str | None
    requires_provider_lifecycle_recovery: bool
```

By default, service-view records do not age out on a controller timer. They
remain current until the provider writes a new view, the view disappears, the
service-use contract becomes unavailable, or provider-session lifecycle
authority becomes invalid. Fresh/stale expiry is an optional local policy only;
it must not reintroduce provider-direct availability queries or a periodic
revalidation loop.

Beacon-derived data can create `unknown` or `probing` candidates. It must not create authoritative `available` records by itself.

---

# 4. Action Identity and Multiple Providers

The same action id can be served by multiple providers. Therefore action references must distinguish between:

```text
logical action id
provider instance
provider id
provider labels
provider-specific availability
```

Configuration currently has `action`, optional `provider_instance_id`, and optional `provider_labels` on a control.  The resolver should interpret those as constraints, not as availability proof.

## 4.1 Configured Action Reference

```python
@dataclass(frozen=True)
class ConfiguredActionRef:
    action_id: str
    provider_instance_id: str | None = None
    provider_labels: Mapping[str, str] = field(default_factory=dict)
```

## 4.2 Provider Selection Rules

When a control references an action:

1. If `provider_instance_id` is set, only that provider instance is eligible.
2. If provider labels are set, only providers matching those labels are eligible.
3. If an existing binding already selected a provider and that provider remains available, keep it.
4. Otherwise choose deterministically from available providers.
5. If no provider is available, render unavailable/pending but keep the control in the page layout.
6. If multiple providers are available, selection policy should be stable and explicit.

Suggested default provider ranking:

```text
1. Existing selected provider for this binding/control.
2. Explicit provider_instance_id match.
3. Provider with strongest label match.
4. Provider with freshest availability.
5. Provider with lowest configured priority.
6. Lexicographic provider_instance_id as final deterministic tie-breaker.
```

This avoids flapping when multiple providers serve the same action.

---

# 5. Binding Semantics

A binding is a controller-owned relationship between:

```text
device control
+ config control
+ selected action provider
+ selected action descriptor
+ action instance/runtime context
```

Bindings are created by the controller, not by providers.

## 5.1 Binding Outcomes

Every configured control on a valid page should produce one of these outcomes:

### `bound`

The control has a selected provider/action and can deliver input/output.

### `unavailable`

The control is valid and configured, but no provider currently serves the action.

The control remains part of the layout. The controller renders an unavailable state.

### `pending`

The control is valid and configured, and there are candidate providers, but real-time availability is unknown or probing.

The control remains part of the layout. The controller renders a pending/checking state.

### `invalid_config`

The config references something structurally invalid.

Depending on severity, this can either block the page or render an error placeholder.

### `invalid_device_control`

The config selector cannot resolve against the live device descriptor.

This can block the page if the page cannot be safely rendered.

## 5.2 Binding Must Not Block on Provider I/O

Binding commit must be a local operation:

```text
read current config snapshot
read current device descriptor
read local availability cache
compute page plan
commit page plan
render controls
```

It must not do:

```text
query provider
wait for Beacon
create Concord agreement
wait for provider attach
wait for action lifecycle acknowledgement
```

Remote availability checks happen asynchronously and cause later replans.

## 5.3 Action Instance Creation

Action instance creation should happen only after the planner selects an available provider.

If availability later becomes unavailable:

* the binding should transition to unavailable
* the action instance should be detached/destroyed
* the control should remain in the layout
* input capture must be terminated safely
* raster output should render unavailable or preserve last known output according to policy

If the action becomes available again:

* the planner may rebind the control
* the control id and config stable id should remain stable
* provider selection should be sticky where possible

---

# 6. Dynamic Pages

Dynamic pages are controller-owned page frames whose descriptor may originate from an action provider.

## 6.1 Opening a Dynamic Page

An action provider can request `open_page` with a dynamic page descriptor. The controller validates the descriptor against:

* current device descriptor
* current config/device state
* security/ownership rules
* local action availability cache

The controller then creates a dynamic page frame.

The provider does not own the frame after creation. It owns only the action context that requested it.

## 6.2 Dynamic Page Timeout

Dynamic page timeout belongs to the device runtime.

Timeout policy is taken from config. The current config model supports page-level and profile-level `widget_timeout_ms`.  The current device manager resolves timeout from page, profile, or default and then closes the dynamic page from its timeout loop.  That is the right ownership boundary.

Provider disappearance must not disable timeout. If the provider that opened the page disappears, the page should still close by timeout and return to the configured static page.

## 6.3 Closing a Dynamic Page

Closing a dynamic page should pop the dynamic frame and reveal or rebind the underlying static frame from the controller’s cached config/page state.

It must not require:

* the owner provider to still be available
* Beacon to currently advertise the owner action
* child actions to be available
* a provider-session contract to be valid

The close path must be transactional:

```text
old_state = current device frame stack
new_plan = build static page plan from cached config + device descriptor + availability cache

if new_plan is structurally valid:
    commit new static frame
    close dynamic frame
else:
    keep old state or enter controlled fallback page
```

The current branch’s close path clears `_dynamic_page_session` before navigating home, and does not gate the final close event on successful static rebind.  The intended architecture should reverse that order: build and commit the return plan first, then finalize the dynamic close.

## 6.4 Dynamic Page Child Bindings

Dynamic page children can target:

* `self`: the owner action
* another action id
* another provider instance
* a provider selected by labels

Each child binding is resolved independently against the availability cache.

If a child action is unavailable, the child control renders unavailable. The dynamic page frame remains active. Timeout still runs.

## 6.5 Provider Loss While Dynamic Page Is Open

If the provider that owns a dynamic page disappears:

* existing child bindings owned by that provider become unavailable or detached
* the dynamic page frame remains controller-owned
* timeout still runs
* close still returns to static page
* replacement commands from the missing provider are no longer accepted
* the controller may optionally shorten the dynamic page timeout

This prevents a dynamic page from becoming a dead-end.

---

# 7. Input Ownership and Stuck-Key Prevention

Input ownership must be independent of action availability and page transitions.

## 7.1 Mandatory Invariant

> Every input `down` delivered to a binding must eventually be followed by exactly one terminal event for that same binding: either `up` or `cancel`.

A transition must never silently drop the release.

The current branch records which binding received `"down"` and consumes `"up"` if the control has been rebound to a different binding.  That avoids sending `up` to the wrong new binding, but it can leave the old action logically pressed. The intended architecture must replace silent consumption with explicit terminal delivery.

## 7.2 Held Input Model

```python
@dataclass(frozen=True)
class InputKey:
    control_id: str
    capability_id: str
    producer: str | None

@dataclass
class HeldInput:
    binding_id: str
    context_id: str
    action_instance_id: str
    provider_action_key: ProviderActionKey | None
    down_event: CapabilityInputEvent
    started_at: float
```

## 7.3 Rebinding While Pressed

If a binding is revoked while it owns a held input:

1. If the provider/action context still exists, send `inputCancel`.
2. Otherwise synthesize local cancellation and clear the held input.
3. Do not deliver the eventual physical `up` to the new binding unless that new binding received a new `down`.

The controller may still ignore the physical `up` for the new binding, but the old binding must already have received a cancel.

## 7.4 Provider Contract

The action-provider API should define one of these explicitly:

Preferred:

```text
capabilityInputCancel(binding, originalInput, reason)
```

Acceptable alternative:

```text
bindingDetached(reason) implies all held inputs for that binding are cancelled
```

The preferred option is better because it is auditable, testable, and unambiguous.

---

# 8. Action Availability: Recommended Approach

There are three possible approaches.

## 8.1 Beacon-Only Availability

Beacon advertises provider actions. The controller treats current Beacon state as action availability.

### Pros

* Simple.
* Passive.
* No provider request/response protocol.
* Easy to bootstrap.

### Cons

* Not real-time.
* Advertisement disappearance may be transient.
* Cannot distinguish “provider exists” from “action is executable now.”
* Cannot model expensive provider startup.
* Cannot model temporary action unavailability.
* Tightly couples discovery to binding.
* Causes layout loss if used during page transitions.

### Assessment

Not sufficient.

Beacon should remain a discovery mechanism and candidate source only.

## 8.2 Contract Per Button Binding

The controller creates a contract/session for each active binding.

### Pros

* Strong explicit agreement.
* Clear lifecycle per binding.
* Provider can reject or accept binding.

### Cons

* Too much churn.
* Makes button binding depend on distributed agreement state.
* Makes page transitions slow and failure-prone.
* Makes dynamic pages expensive.
* Makes the device unresponsive when providers are delayed.
* Couples physical UI ownership to provider liveness.
* Creates stuck-key and lost-layout failure modes.

### Assessment

Do not use this model.

This is the failure mode seen in the current branch: provider-session readiness is introduced directly into `_try_resolve_binding()` / `_activate_binding()` and the binding can remain pending instead of immediately giving the device a stable layout state. 

## 8.3 Action Availability Service View With Long-Lived Interest

The controller uses Beacon to find internal action-availability services exposed
by provider runtimes. It opens a termless service-use Concord contract with each
matching service and watches that contract-fenced `actions/current` service
view for current action descriptors and status.

Action interest is maintained independently of button binding and may be kept
warm for hours. It is a local controller planning signal; it is not sent over
the action lane.

### Pros

* Current availability comes from the provider-owned service view.
* Binding remains local and non-blocking.
* Providers can prepare expensive resources.
* Providers can report temporary unavailability.
* The same action can be served by multiple providers.
* Existing UI state can remain stable while service views and lifecycle authority remain valid.
* Dynamic page timeout and navigation stay device-owned.
* The action lane remains reserved for lifecycle and execution messages.

### Cons

* Requires a controller service client and view watcher.
* Requires local cache and missing-view semantics.
* Requires provider selection policy.
* Requires runtime-managed service-view publication in each provider runtime.

### Assessment

Current architecture.

---

# 9. Action Interest

Action interest is the controller’s expression that a provider/action may be needed.

## 9.1 Interest Sources

The controller computes action interest from:

* active visible page bindings
* open dynamic page bindings
* all controls in the active config for connected devices
* recently visible pages
* provider settings panels
* recently active dynamic page actions
* optional prewarm policy

## 9.2 Interest Strength

Suggested model:

```python
@dataclass
class ActionInterest:
    action_ref: ConfiguredActionRef
    source: Literal[
        "visible_binding",
        "dynamic_page",
        "connected_config",
        "recent_use",
        "settings",
        "prewarm",
    ]
    strength: Literal["strong", "warm"]
    first_needed_at: float
    last_needed_at: float
    retain_until: float
```

## 9.3 Retention Policy

Strong interest exists while the action is actively needed.

Warm interest remains after strong interest disappears.

Suggested defaults:

```text
strong interest: while visible/config-active
warm retention: 4 hours
missing service view: provider actions unavailable
```

The “hours” timeout is important. It prevents a provider from repeatedly tearing down and rebuilding actions while the user navigates around. But that timeout belongs to action interest, not button binding.

## 9.4 Optional Action-Level Contract

If a provider needs stronger semantics, introduce an action-level interest contract:

```text
controller declares: I may need action X from provider P until time T
provider declares: I can keep action X warm/available under terms Y
```

This contract is per provider/action interest, not per control binding.

It should not block page layout. If the contract is pending, the control renders pending/unavailable while the controller continues to own the layout.

Concord may be useful here only when there is a real resource commitment or
exclusivity requirement. Ordinary action availability uses the provider-scoped
service-use contract and service view described below.

---

# 10. Action Availability Service View

This is intentionally protocol-level, not tied to an implementation.

## 10.1 Discovery

Beacon advertises the provider runtime's internal availability service:

```json
{
  "serviceId": "action-availability.python",
  "namespace": "dev.deckr.action_availability.service",
  "endpoint": "service:action-availability.python",
  "sessionId": "service-session",
  "backendStatus": "available",
  "views": {
    "actions": {
      "storeName": "deckr_action_availability_service_view_v1",
      "keyPrefix": "service.action-availability.python.actions."
    }
  }
}
```

This creates service candidates only. Beacon remains discovery only and is not
availability authority.

## 10.2 Service Use

The controller opens a termless service-use Concord contract with the service
endpoint. The service view is fenced by that contract, so each controller gets
its own authorized view entry.

```text
participants: controller:<controller-id>, service:action-availability.python
profile: dev.deckr.action_availability.service.use.v1
terms: null
```

## 10.3 Current View

The provider runtime writes one provider-scoped `actions/current` view for every
valid controller service-use contract:

```json
{
  "providerInstanceId": "python",
  "providerEndpoint": "action_provider:python",
  "providerId": "dev.deckr.python",
  "providerSessionId": "provider-session",
  "entries": [
    {
      "actionId": "clock",
      "status": "available",
      "descriptor": { "actionId": "clock", "name": "Clock" },
      "reason": null
    },
    {
      "actionId": "weather",
      "status": "unavailable",
      "descriptor": null,
      "reason": "missing_api_key"
    }
  ]
}
```

The controller treats this payload as authoritative current availability for
planning. It uses `providerSessionId` only when preparing Concord
action-provider lifecycle sessions; execution stays on the action lane.

---

# 11. Runtime Flows

## 11.1 Device Appears

1. Hardware discovery/claim identifies a live device.
2. Device runtime is created with descriptor and route state.
3. Config service attempts to match by fingerprint and labels.
4. If no config matches, device enters unconfigured layout.
5. If config matches, device runtime subscribes to that config.
6. Device runtime computes initial page plan.
7. Binding planner uses local action availability cache.
8. Controls are rendered as bound, unavailable, pending, or invalid.
9. Action interest service marks referenced actions as needed.
10. Availability service queries candidate providers asynchronously.
11. Availability changes trigger replans of affected controls only.

The current config service already supports matching by fingerprint and labels, including ambiguity detection. 

## 11.2 Config Appears

If a live unmatched device now matches the config:

1. Device runtime attaches config.
2. Static page plan is built.
3. Controls render immediately using local availability cache.
4. Action interest is updated.
5. Provider availability refresh happens asynchronously.

## 11.3 Config Changes

1. Config service emits full replacement config.
2. Device runtime builds a new static base plan.
3. If a dynamic page is open:

   * preserve it only if still valid under policy
   * otherwise close it with reason `config_changed`
4. Commit the new plan transactionally.
5. Recompute action interest.
6. Cancel held inputs for removed/rebound bindings.

The current `DeviceManager` already listens to config stream changes and clears/finalizes on config removal/change; the intended architecture keeps that dynamic behavior but makes the transition transactional. 

## 11.4 Config Disappears

1. Device runtime releases all action bindings.
2. Held inputs are cancelled.
3. Dynamic pages are closed.
4. Raster output is cleared or replaced with unconfigured placeholder.
5. Device remains connected.
6. Action interest from that config is removed or downgraded to warm retention.

## 11.5 Provider Candidate Appears

1. Beacon discovers a candidate provider.
2. Availability service records the candidate.
3. If the provider may serve needed actions, the controller watches that provider's action availability service view.
4. Availability cache updates when the service view changes or disappears.
5. Affected device runtimes replan relevant controls.

Device layout does not change merely because Beacon changed.

## 11.6 Provider Candidate Disappears

1. Beacon removal does not invalidate current service-view records.
2. The service view remains authoritative until it changes, disappears, or its service-use contract becomes unavailable.
3. Existing bindings may continue only while provider-session readiness and lifecycle authority remain valid.
4. If the service view or provider session becomes invalid, affected controls become unavailable or pending according to the current record.
5. Device pages remain intact.

## 11.7 Action Becomes Available

1. Availability cache updates.
2. Affected controls are replanned.
3. Existing unavailable/pending controls can become bound.
4. The controller creates action instances as needed.
5. The rendered control updates in place.

## 11.8 Action Becomes Unavailable

1. Availability cache updates.
2. Affected bindings detach.
3. Held inputs cancel.
4. Controls remain in layout and render unavailable.
5. Dynamic pages remain device-owned.
6. Timeout and close continue to work.

## 11.9 Dynamic Page Opens

1. Action sends dynamic page descriptor.
2. Controller validates descriptor structurally.
3. Controller builds dynamic page plan using local availability cache.
4. Dynamic frame is pushed/installed.
5. Timeout starts.
6. Child controls render as bound/unavailable/pending.
7. Additional child action interest is recorded.

## 11.10 Dynamic Page Times Out

1. Device runtime detects timeout.
2. Runtime builds static return plan from cached config snapshot.
3. Runtime commits static frame.
4. Runtime closes dynamic frame.
5. Runtime sends page closed notification if owner still exists.
6. If owner does not exist, close is still completed locally.

---

# 12. Transactional Page Planning

Page transitions should use a two-phase algorithm.

## 12.1 Build Phase

No mutation.

```text
resolve controls
resolve capabilities
resolve configured action refs
consult local availability cache
choose provider
compute binding outcomes
compute output effects
compute input cancellation effects
```

## 12.2 Commit Phase

Mutation happens in a defined order.

```text
cancel held inputs that will lose ownership
detach bindings that are leaving
create/update action instances for available bindings
install new binding map
render all changed controls
update frame stack
publish lifecycle messages
```

## 12.3 Failure Handling

If build phase fails structurally:

* keep current page, or
* enter a controller-owned error/fallback page

Do not leave the device with no bindings unless the config is absent or invalid in a way that makes layout impossible.

---

# 13. Availability and Rendering Policy

## 13.1 Control Rendering States

Every configured control should render one of:

```text
normal action output
unavailable
pending/checking
config error
device capability error
```

Action unavailability is not a layout failure.

## 13.2 Output Preservation

When rebinding a control:

* if the same logical binding remains selected, preserve output when possible
* if the provider changes, clear or render pending according to policy
* if action becomes unavailable, render unavailable
* if dynamic page closes, return to static page output without requiring provider calls

## 13.3 Current And Stale Availability

Default policy:

```text
service-view available + provider lifecycle ready: bind normally
service-view available + provider lifecycle not ready: render pending
service-view unavailable or missing: render unavailable
beacon candidate only: render pending/probing, not available
no candidate: render unavailable
```

An optional freshness policy may mark service-view records `stale` or `expired`,
but it is a UI planning policy, not a transport retry mechanism. Stale retention
for an existing binding can preserve output only while the service-use contract
and provider lifecycle authority remain valid.

---

# 14. Invariants

These should become tests.

1. **Device ownership invariant**
   Action availability changes must not clear the current device page or dynamic page stack.

2. **No remote wait invariant**
   Page transition and input routing must not perform provider network I/O.

3. **Config prerequisite invariant**
   No action binding exists without an active config snapshot.

4. **Unavailable action invariant**
   A configured control whose action is unavailable remains visible as unavailable/pending.

5. **Dynamic timeout invariant**
   Dynamic page timeout fires even if the owning provider disappears.

6. **Return home invariant**
   Closing a dynamic page returns to the static page from config without requiring Beacon or provider availability.

7. **Input terminal invariant**
   Every delivered input down receives up or cancel for the same binding.

8. **Beacon candidate invariant**
   Beacon can create provider candidates but not authoritative action availability.

9. **Multiple provider invariant**
   The same action id may be available through multiple providers, and provider selection must be deterministic and sticky.

10. **Config disappearance invariant**
    Config disappearance releases bindings but does not imply device disappearance.

11. **Provider disappearance invariant**
    Provider disappearance changes action availability, not device state.

12. **Action interest invariant**
    Action-interest leases/contracts are keyed by provider/action need, not by button binding.

---

# 15. Implementation Direction

## 15.1 Remove Provider-Session Gating From Binding

The binding path should not depend on provider-session contracts. Keep endpoint identity and recipient session routing, but do not make `_try_resolve_binding()` return pending solely because a provider session agreement is not ready.

In the intended design, a page can commit with unavailable/pending controls.

## 15.2 Introduce `ActionAvailabilityService`

This service should own:

* action-availability service candidates from Beacon
* service-use Concord leases for availability services
* `actions/current` service-view watches
* availability cache
* provider selection
* action interest TTLs
* changed provider/action keys for scoped ControllerService/DeviceManager fanout

## 15.3 Introduce `BindingPlanner`

Move page planning into a pure/plannable component:

```python
class BindingPlanner:
    def build_page_plan(
        self,
        *,
        device: DeviceDescriptor,
        config: DeviceConfig,
        frame: PageFrame,
        availability: ActionAvailabilityPlanningSnapshot,
        previous: DeviceRuntimeState,
    ) -> PagePlan:
        ...
```

No network I/O. No mutation.

## 15.4 Make Dynamic Pages Explicit Frames

Represent static and dynamic pages as frames:

```python
@dataclass
class StaticPageFrame:
    profile_name: str
    page_index: int
    config_version: str

@dataclass
class DynamicPageFrame:
    page_id: str
    page_session_id: str
    owner: DynamicPageOwner
    descriptor: DynamicPageCommand
    opened_at: float
    last_activity_at: float
    timeout_ms: int
```

Closing a dynamic page pops the dynamic frame and replans the static frame.

## 15.5 Add Explicit Input Cancel

Add an action input cancellation contract.

```json
{
  "messageType": "capabilityInputCancel",
  "body": {
    "binding": {},
    "input": {},
    "reason": "binding_replaced"
  }
}
```

Use this whenever a held binding is revoked before physical release.

## 15.6 Add Regression Tests

Minimum tests:

1. **Close dynamic page after Beacon withdrawal**

   * static home action bound
   * open dynamic page
   * simulate Beacon/provider discovery withdrawal
   * close dynamic page
   * assert home page controls still have layout
   * unavailable is acceptable; lost layout is not

2. **Dynamic timeout after provider disappearance**

   * open dynamic page
   * simulate owner provider disappearance
   * advance clock past timeout
   * assert static page restored

3. **Action unavailable does not clear layout**

   * action availability changes to unavailable
   * assert control renders unavailable
   * assert page/frame still active

4. **Config disappears while provider remains**

   * remove config
   * assert bindings removed
   * assert device remains connected/unconfigured

5. **Provider returns**

   * unavailable control becomes bound after provider availability update
   * no full device reset

6. **Held input gets cancel**

   * down on binding A
   * transition rebinds same physical control to binding B
   * assert A receives cancel
   * physical up is not delivered to B unless B received down

---

# 16. Final Architectural Decision

Use this model:

```text
Beacon = candidate provider discovery
Service view protocol = real-time action availability
Action interest = long-lived provider/action need, retained for hours
Device runtime = authoritative layout/input/rendering owner
Config = dynamic glue and binding prerequisite
Binding = local transaction over device + config + availability cache
```

Do **not** use this model:

```text
Beacon/Concord provider session readiness
→ button binding readiness
→ page transition success
→ device layout ownership
```

The latter makes the device’s physical UI depend on transient distributed state and is the root of the unresponsive/stuck/lost-binding behavior.

The controller should always be able to answer locally:

```text
What page is this device showing?
Which controls exist on that page?
What should each control render right now?
Who owns each held input?
How do I get home from this dynamic page?
```

Action availability can change the answer to:

```text
Can this control execute its configured action?
```

It must not change the controller’s ability to own and operate the device.
