from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs
from tinydb import Query, TinyDB

dirs = PlatformDirs("deckr", "deckr", version="1.0")


@dataclass(frozen=True, slots=True, kw_only=True)
class PersistenceKey:
    """Composite identity for persisted settings. Uniquely identifies a binding: device, profile, page, slot, action; optional dynamic_page_uuid for plugin-defined pages."""

    device_id: str
    profile_id: str
    page_id: str
    slot_id: str
    action_uuid: str
    dynamic_page_uuid: str | None = None

    def as_key(self) -> str:
        parts = [
            f"device={self.device_id}",
            f"profile={self.profile_id}",
            f"page={self.page_id}",
            f"slot={self.slot_id}",
            f"action={self.action_uuid}",
        ]
        if self.dynamic_page_uuid is not None:
            parts.append(f"dynamic_page={self.dynamic_page_uuid}")
        return "|".join(parts)


class ControllerPersistence:
    def __init__(self, device_id: str):
        self.db_path = Path(dirs.user_data_dir)
        self.db_path.mkdir(parents=True, exist_ok=True)

        safe_filename = device_id.replace("/", "_").replace(":", "_")
        self.db = TinyDB(self.db_path / f"{safe_filename}.json")

    def get_value(self, key: str) -> Any:
        row = self.db.get(Query().key == key)
        if row is None:
            return None
        return row.get("value")

    def set_value(self, key: str, value: Any):
        self.db.upsert({"key": key, "value": value}, Query().key == key)

    def delete_value(self, key: str) -> int:
        removed = self.db.remove(Query().key == key)
        return len(removed)

    def get_settings(self, key: PersistenceKey) -> dict | None:
        row = self.db.get((Query().kind == "settings") & (Query().key == key.as_key()))
        if row is None:
            return None

        value = row.get("value")
        if isinstance(value, dict):
            return dict(value)
        return None

    def set_settings(self, key: PersistenceKey, value: dict) -> None:
        payload = {
            "kind": "settings",
            "key": key.as_key(),
            "value": dict(value),
            "device_id": key.device_id,
            "profile_id": key.profile_id,
            "page_id": key.page_id,
            "slot_id": key.slot_id,
            "action_uuid": key.action_uuid,
            "dynamic_page_uuid": key.dynamic_page_uuid,
        }
        self.db.upsert(
            payload, (Query().kind == "settings") & (Query().key == key.as_key())
        )

    def prune_settings(self, *, device_id: str, valid_keys: set[str]) -> int:
        """Remove settings rows for this device whose key is not in valid_keys (from config + dynamic-page registry). Returns count removed."""
        rows = self.db.search(
            (Query().kind == "settings") & (Query().device_id == device_id)
        )
        stale_ids = [row.doc_id for row in rows if row.get("key") not in valid_keys]
        if not stale_ids:
            return 0

        removed = self.db.remove(doc_ids=stale_ids)
        return len(removed)
