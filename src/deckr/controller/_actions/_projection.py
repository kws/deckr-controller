"""Controller-owned action runtime service coordination."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from deckr.actions.messages import (
    ActionAvailabilityEntry,
)

from deckr.controller._actions._models import (
    ActionMetadata,
    ProviderActionKey,
)

logger = logging.getLogger(__name__)


def metadata_for_entry(
    cache,
    *,
    key: ProviderActionKey,
    provider_id: str,
    provider_session_id: str | None,
    provider_labels: Mapping[str, str] | None,
    entry: ActionAvailabilityEntry,
) -> ActionMetadata | None:
    descriptor = entry.descriptor
    existing = cache.record_for(key)
    existing_metadata = existing.metadata if existing is not None else None
    if descriptor is None and existing_metadata is None:
        return None
    return ActionMetadata(
        action_id=key.action_id,
        descriptor=descriptor
        or (existing_metadata.descriptor if existing_metadata is not None else None),
        provider_instance_id=key.provider_instance_id,
        provider_id=provider_id,
        name=descriptor.name
        if descriptor is not None
        else (existing_metadata.name if existing_metadata is not None else None),
        provider_session_id=provider_session_id,
        provider_labels=dict(provider_labels or {}),
        settings_schema=(
            dict(descriptor.settings_schema)
            if descriptor is not None and descriptor.settings_schema is not None
            else None
        ),
        provider_settings_schema=(
            dict(descriptor.provider_settings_schema)
            if descriptor is not None
            and descriptor.provider_settings_schema is not None
            else None
        ),
    )
