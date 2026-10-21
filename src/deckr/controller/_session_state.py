"""Device identity, local state and asynchronous operation authority.

These models perform no external work. They are mutated only by the session's
serialized event handlers; adapters return operation results to those handlers.
"""

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

from deckr.controller._bindings._attachments import ControlAttachmentState
from deckr.controller._pages import PageSessionService
from deckr.controller.config._data import DeviceConfig


class BindingStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    PREPARING = "preparing"
    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    hardware: DeviceRef
    session_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class OperationId:
    session_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class PendingOperation:
    id: OperationId
    binding_id: str
    kind: str


@dataclass(slots=True)
class DeviceState:
    identity: DeviceIdentity
    device: DeviceDescriptor
    config: DeviceConfig
    navigation: PageSessionService
    hardware_claim: object | None = None
    attachments: ControlAttachmentState = field(default_factory=ControlAttachmentState)
    operations: dict[OperationId, PendingOperation] = field(default_factory=dict)
    binding_status: dict[str, BindingStatus] = field(default_factory=dict)
    operation_sequence: int = 0
    stopped: bool = False
    retired_bindings: deque[str] = field(default_factory=deque)

    def hardware_current(self, hardware) -> bool:
        return (
            not self.stopped
            and hardware.has_authority(self.config.id, self.identity.hardware)
            and (
                self.hardware_claim is None
                or hardware.claim_identity(self.config.id) == self.hardware_claim
            )
        )

    def begin_operation(self, binding_id: str, kind: str) -> PendingOperation:
        self.operation_sequence += 1
        operation = PendingOperation(
            OperationId(self.identity.session_id, self.operation_sequence),
            binding_id,
            kind,
        )
        self.operations[operation.id] = operation
        return operation

    def operation_current(self, operation: PendingOperation) -> bool:
        return not self.stopped and self.operations.get(operation.id) == operation

    def retire_binding(self, binding_id: str) -> None:
        self.binding_status[binding_id] = BindingStatus.RETIRED
        self.retired_bindings.append(binding_id)
        while len(self.retired_bindings) > 512:
            self.binding_status.pop(self.retired_bindings.popleft(), None)
        for operation in tuple(self.operations.values()):
            if operation.binding_id == binding_id:
                self.operations.pop(operation.id, None)
