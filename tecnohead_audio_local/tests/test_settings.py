from __future__ import annotations

from app.settings import ConfigStore, redact_object


def test_default_config_contains_operational_values(config):
    assert config.timezone == "America/Sao_Paulo"
    assert config.sync.times == ["11:00", "15:00"]
    assert config.playback.window_start == "05:30"
    assert config.playback.window_end == "21:30"
    assert config.playback.volume_percent == 90
    assert [device.host for device in config.devices] == ["192.168.0.13", "192.168.0.14"]
    assert config.normalization.target_lufs == -18
    assert config.normalization.true_peak_db == -2


def test_config_store_is_atomic_and_redacted(paths):
    store = ConfigStore(paths)
    config = store.load()
    config.mqtt.password = "super-secret"
    config.playlist_url = "https://example.test/list?access_token=should-not-show"
    store.save(config)
    public = store.public()
    assert "super-secret" not in str(public)
    assert "should-not-show" not in str(public)
    assert paths.config_file.exists()
    if __import__("os").name != "nt":
        assert paths.config_file.stat().st_mode & 0o077 == 0


def test_config_store_migrates_legacy_spotify_and_default_schedule(paths):
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text('{"schema_version": 1, "spotify_client_id": "old", "spotify_client_secret": "old", "sync": {"times": ["03:00", "15:00"]}}', encoding="utf-8")
    config = ConfigStore(paths).load()
    assert config.schema_version == 2
    assert config.sync.times == ["11:00", "15:00"]
    assert not hasattr(config, "spotify_client_secret")


def test_redact_object_recurses():
    value = redact_object({"password": "abc", "nested": {"token": "def", "title": "ok"}})
    assert value == {"password": "[redigido]", "nested": {"token": "[redigido]", "title": "ok"}}

