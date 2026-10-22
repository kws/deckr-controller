from deckr.actions.endpoints import (
    action_provider_address,
)
from deckr.contracts.messages import controller_address

CONTROLLER_ID = "controller-main"


CONTROLLER_ADDR = controller_address(CONTROLLER_ID)


PROVIDER_INSTANCE_ID = "python"


PROVIDER_ID = "test.provider"


PROVIDER_ADDR = action_provider_address(PROVIDER_INSTANCE_ID)


PROVIDER_SESSION_ID = "action-provider-session"


UNSUPPORTED_ACTION_MESSAGE = "unsupportedActionMessage"


ACTION_X_UUID = "test.action.x"
