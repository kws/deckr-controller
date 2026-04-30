from __future__ import annotations

import uuid

_ACTION_INSTANCE_NAMESPACE = uuid.UUID("dcd72f2a-65cb-4d9f-b0e8-4e0ef3d334f1")


def derive_action_instance_id(
    *,
    controller_id: str,
    config_id: str,
    action_id: str,
    stable_id: str | None = None,
    profile_id: str | None = None,
    page_id: str | None = None,
    control_id: str | None = None,
) -> str:
    if stable_id:
        seed = "\x1f".join((controller_id, config_id, action_id, stable_id))
        return str(uuid.uuid5(_ACTION_INSTANCE_NAMESPACE, seed))
    required = {
        "profile_id": profile_id,
        "page_id": page_id,
        "control_id": control_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            "derived action instance id missing: " + ", ".join(sorted(missing))
        )
    seed = "\x1f".join(
        (
            controller_id,
            config_id,
            profile_id or "",
            page_id or "",
            control_id or "",
            action_id,
        )
    )
    return str(uuid.uuid5(_ACTION_INSTANCE_NAMESPACE, seed))
