from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.runtime import Runtime


@pytest.mark.asyncio
async def test_ingress_api_is_relative_and_redacts_config(paths, monkeypatch):
    monkeypatch.setenv("APP_DISABLE_BACKGROUND", "1")
    runtime = Runtime(paths)
    runtime.config.spotify_client_secret = "do-not-return"
    runtime.config.playlist_url = "https://example.test/list?token=do-not-return"
    runtime.config_store.save(runtime.config)
    app.state.runtime = runtime
    await runtime.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver/") as client:
            config_response = await client.get("/api/config")
            assert config_response.status_code == 200
            assert "do-not-return" not in config_response.text
            credential_response = await client.put("/api/credentials", json={"spotify_client_secret": "temporary-secret"})
            assert credential_response.status_code == 200
            assert "temporary-secret" not in credential_response.text
            assert credential_response.json()["credentials"]["spotify_client_secret_configured"] is True
            clear_response = await client.put("/api/credentials", json={"clear": True})
            assert clear_response.status_code == 200
            assert clear_response.json()["credentials"]["spotify_client_secret_configured"] is False
            page = await client.get("/")
            assert page.status_code == 200
            assert "new URL(`api/${path" in (await client.get("/static/app.js")).text
            assert "https://" not in (await client.get("/static/app.js")).text.split("apiUrl")[1].split(";", 1)[0]
    finally:
        await runtime.stop()

