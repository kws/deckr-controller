# Device session refactor

The supplied architectural review is the acceptance scope for this change.
The controller uses a device state machine behind ports and adapters.
See [the architecture reference](architecture.md) for implemented responsibilities,
policies, source links and the acceptance-test map.

## Invariants and policies

- A physical device reference, a runtime session ID, and a configuration ID are different identities. One live device per configuration remains the assignment policy.
- A device session owns navigation, desired and installed bindings, held interactions, presentation authority, and pending operations. Local decisions are serialized; provider, settings, action, and render work runs outside that boundary.
- Operations are recorded before dispatch. Completion may install resources only while its operation, desired binding, provider incarnation, and hardware session remain current.
- A preparing or unavailable control accepts no input. Other controls and navigation remain responsive. Input is ordered and is never replayed after an uncertain send.
- Queue saturation defers preparation until capacity returns; cancellation and teardown have a separate bounded reserve. Refused attachment delivery cannot activate a binding.
- Revocation is local and immediate. Remote detach, cancellation, destruction, and page notifications are bounded best-effort effects.
- A binding authorizes opening a temporary page. The resulting page occurrence independently authorizes updates and closure, and retains its owning action instance.
- Navigation has a configured route and at most one temporary page. Closing a temporary page replans the configured route against current facts.
- An invalid underlying layout never traps an expired or orphaned temporary page: close retires it, retains an empty configured route, and permits later descriptor/config recovery.
- Config replacement resets to the first profile/page. Explicit removal clears the UI. Source failure retains the last validated snapshot. A hardware reconnection creates a new session and resets navigation.
- Provider replacement revokes the previous incarnation even if logical names are reused. Ordinary child provider loss leaves the temporary page open; owner loss closes it.
- Press ownership survives until release or local cancellation. A cancelled interaction's release never reaches a replacement binding.
- Queued nonterminal input is discarded after binding or hardware revocation. Lifecycle recovery rechecks authority after each external preparation step.
- Materialized snapshot publication is ordered, publishes validated empty snapshots, and retries failures without requiring a source edit.
- Only hardware input extends a page deadline. Expiry checks page identity and deadline revision; content changes do not extend it.
- Local and remote actions use scoped requests and equivalent authority checks. Local callbacks execute outside transitions and cannot directly mutate navigation or presentation.
- Render tickets identify the binding and presentation revision. Completion and output handoff revalidate authority; queued output is invalidated by revocation. Already dispatched bytes cannot be recalled. Hardware rejections remain correlated to the exact command.
- Interest snapshots are diagnostics, not an implicit prewarming or retention policy.

## Implementation checklist

- [x] Common action gateway, scoped typed requests, local and remote adapters.
- [x] DeviceSession state ownership, distinct identities, serialized events and operation ledger.
- [x] Nonblocking reconciliation and lifecycle effects with stale-completion rejection.
- [x] Pure navigation rules, independent page grants and revisioned expiry events.
- [x] Explicit rendering port and versioned output handoff, without private binding-host access.
- [x] Bounded queues, ordered input, explicit lifetime retention and disconnect policies.
- [x] Boundary acceptance tests and architecture documentation.
- [x] Ruff, pytest, specification conformance, and build validation.
