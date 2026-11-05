"""Tests for ConfigStore — unified runtime config (whitelist + voice + flags)."""
import json
from pathlib import Path

from discord_fish_tts.config_store import ConfigStore


# ---- whitelist subset ----

def test_user_not_whitelisted_by_default(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.is_user_whitelisted(123) is False


def test_add_then_whitelisted(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.add_user(123) is True
    assert cfg.is_user_whitelisted(123) is True


def test_add_user_idempotent(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.add_user(123) is True
    assert cfg.add_user(123) is False


def test_remove_user(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    cfg.add_user(123)
    assert cfg.remove_user(123) is True
    assert cfg.is_user_whitelisted(123) is False


def test_remove_nonexistent_user(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.remove_user(123) is False


def test_list_users_sorted(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    cfg.add_user(5)
    cfg.add_user(2)
    cfg.add_user(8)
    assert cfg.list_users() == [2, 5, 8]


# ---- voice_id ----

def test_voice_id_default_none(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.get_voice_id() is None


def test_set_get_voice_id(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    cfg.set_voice_id("abc123")
    assert cfg.get_voice_id() == "abc123"


def test_voice_id_persists(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg1 = ConfigStore(path)
    cfg1.set_voice_id("xyz789")
    cfg2 = ConfigStore(path)
    assert cfg2.get_voice_id() == "xyz789"


# ---- first-run flag ----

def test_first_run_default_true(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    assert cfg.is_first_run() is True


def test_mark_first_run_done(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "config.json")
    cfg.mark_first_run_done()
    assert cfg.is_first_run() is False


def test_first_run_persists(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg1 = ConfigStore(path)
    cfg1.mark_first_run_done()
    cfg2 = ConfigStore(path)
    assert cfg2.is_first_run() is False


# ---- corruption / missing file ----

def test_handles_missing_file(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "missing.json")
    assert cfg.list_users() == []
    assert cfg.get_voice_id() is None
    assert cfg.is_first_run() is True


def test_handles_corrupt_json(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    cfg = ConfigStore(path)
    assert cfg.list_users() == []
    assert cfg.get_voice_id() is None
    assert cfg.is_first_run() is True


# ---- migration / serialization ----

def test_save_format(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg = ConfigStore(path)
    cfg.set_voice_id("v1")
    cfg.add_user(100)
    cfg.add_user(200)
    cfg.mark_first_run_done()

    data = json.loads(path.read_text())
    assert data == {
        "voice_id": "v1",
        "whitelist": [100, 200],
        "first_run_done": True,
        "user_voices": {},
        "auto_tts_users": [],
        "auto_tts_channels": [],
    }


def test_save_creates_parent_dir(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "config.json"
    cfg = ConfigStore(nested)
    cfg.add_user(1)
    assert nested.exists()


def test_legacy_whitelist_format_loads(tmp_path: Path):
    """If a previous version's whitelist.json content is renamed to
    config.json, the loader should still find allowed users."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"allowed_users": [1, 2, 3]}))
    cfg = ConfigStore(path)
    # Either field name accepted; we expose via list_users()
    assert cfg.list_users() == [1, 2, 3]
