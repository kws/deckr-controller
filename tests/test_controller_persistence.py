"""Unit tests for ControllerPersistence settings semantics."""

from tinydb import Query

from deckr.controller import _persistence
from deckr.controller._persistence import ControllerPersistence, PersistenceKey


def _patch_tmp_dirs(monkeypatch, tmp_path):
    class TmpDirs:
        user_data_dir = str(tmp_path)

    monkeypatch.setattr(_persistence, "dirs", TmpDirs())


def test_legacy_set_value_is_upsert(monkeypatch, tmp_path):
    _patch_tmp_dirs(monkeypatch, tmp_path)
    persistence = ControllerPersistence("dev-1")

    persistence.set_value("legacy-key", {"v": 1})
    persistence.set_value("legacy-key", {"v": 2})

    assert persistence.get_value("legacy-key") == {"v": 2}
    rows = persistence.db.search(Query().key == "legacy-key")
    assert len(rows) == 1


def test_settings_upsert_and_prune(monkeypatch, tmp_path):
    _patch_tmp_dirs(monkeypatch, tmp_path)
    persistence = ControllerPersistence("dev-1")

    key_active = PersistenceKey(
        device_id="dev-1",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action.a",
    )
    key_stale = PersistenceKey(
        device_id="dev-1",
        profile_id="default",
        page_id="1",
        slot_id="0,0",
        action_uuid="action.a",
    )

    persistence.set_settings(key_active, {"name": "active"})
    persistence.set_settings(key_stale, {"name": "stale"})
    persistence.set_settings(key_active, {"name": "active-v2"})

    assert persistence.get_settings(key_active) == {"name": "active-v2"}
    assert persistence.get_settings(key_stale) == {"name": "stale"}

    removed = persistence.prune_settings(
        device_id="dev-1",
        valid_keys={key_active.as_key()},
    )
    assert removed == 1
    assert persistence.get_settings(key_active) == {"name": "active-v2"}
    assert persistence.get_settings(key_stale) is None
