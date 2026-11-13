from __future__ import annotations

import pytest
from conftest import make_entry
from httpx import ASGITransport, AsyncClient

from app.main import app, media_app
from app.runtime import Runtime


@pytest.mark.asyncio
async def test_ingress_api_is_relative_and_redacts_config(paths, monkeypatch):
    monkeypatch.setenv("APP_DISABLE_BACKGROUND", "1")
    runtime = Runtime(paths)
    runtime.config.mqtt.password = "do-not-return"
    runtime.config.playlist_url = "https://example.test/list?token=do-not-return"
    runtime.config_store.save(runtime.config)
    app.state.runtime = runtime
    await runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver/") as client:
            config_response = await client.get("/api/config")
            assert config_response.status_code == 200
            assert "do-not-return" not in config_response.text
            credential_response = await client.put("/api/mqtt/credentials", json={"mqtt_password": "temporary-secret"})
            assert credential_response.status_code == 200
            assert "temporary-secret" not in credential_response.text
            assert credential_response.json()["mqtt"]["password_configured"] is True
            clear_response = await client.put("/api/mqtt/credentials", json={"clear": True})
            assert clear_response.status_code == 200
            assert clear_response.json()["mqtt"]["password_configured"] is False
            page = await client.get("/")
            assert page.status_code == 200
            assert "new URL(`api/${path" in (await client.get("/static/app.js")).text
            assert "await saveSettings({ notify: false })" in (await client.get("/static/app.js")).text
            assert "spotify_client_id" not in (await client.get("/static/app.js")).text
            assert "https://" not in (await client.get("/static/app.js")).text.split("apiUrl")[1].split(";", 1)[0]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_sync_rejects_missing_playlist_before_background_task(paths, monkeypatch):
    monkeypatch.setenv("APP_DISABLE_BACKGROUND", "1")
    runtime = Runtime(paths)
    runtime.config.playlist_url = ""
    runtime.config_store.save(runtime.config)
    app.state.runtime = runtime
    await runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver/") as client:
            response = await client.post("/api/sync", json={})
        assert response.status_code == 400
        assert response.json()["detail"] == "configure a URL da playlist antes de sincronizar"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_media_playlist_endpoint_returns_ready_tracks(paths, monkeypatch):
    monkeypatch.setenv("APP_DISABLE_BACKGROUND", "1")
    runtime = Runtime(paths)
    runtime.config.media.base_url = "http://192.168.0.50"
    runtime.config_store.save(runtime.config)
    for position in range(2):
        entry = make_entry(position, title=f"Faixa {position + 1}")
        runtime.library.upsert_entry(entry)
        destination = paths.library / "tracks" / f"{entry.entry_id}.mp3"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")
        runtime.library.set_ready(
            entry.entry_id,
            relative_path=f"tracks/{entry.entry_id}.mp3",
            file_path=destination,
            normalization={"mode": "fake", "target_lufs": -18, "true_peak_db": -2, "output_format": "mp3", "completed_at": "now"},
        )
    app.state.runtime = runtime
    media_app.state.runtime = runtime
    await runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=media_app), base_url="http://media.test:18198/") as client:
            response = await client.get("/playlist.m3u8", params={"token": runtime.config.media.stream_token, "start": runtime.library.ready_tracks()[1].entry_id})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/x-mpegurl")
        lines = response.text.splitlines()
        assert lines[0] == "#EXTM3U"
        assert lines[1].startswith("#EXTINF:120,Artista - Faixa 2")
        assert "/stream/" + runtime.library.ready_tracks()[1].entry_id in lines[2]
        assert "/stream/" + runtime.library.ready_tracks()[0].entry_id in lines[4]
        assert ":18198/stream/" in response.text
    finally:
        await runtime.stop()

