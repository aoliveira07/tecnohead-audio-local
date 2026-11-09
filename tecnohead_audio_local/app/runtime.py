from __future__ import annotations

import asyncio
import os
from typing import Any

from app.diagnostics import DiagnosticsService
from app.library import LibraryManager
from app.linkplay import LinkPlayClient
from app.loudness import LoudnessProcessor
from app.models import AppConfig, ConfigUpdate, Track
from app.mqtt import MqttService
from app.playback import PlaybackSupervisor
from app.scheduler import SyncScheduler
from app.settings import AppPaths, ConfigStore, EventLog, StateStore, public_config
from app.sync_service import SpotDLAdapter, SyncService


class Runtime:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.from_environment()
        self.paths.ensure()
        self.config_store = ConfigStore(self.paths)
        self.config: AppConfig = self.config_store.load()
        self.state_store = StateStore(self.paths)
        self.events = EventLog(self.paths)
        self.library = LibraryManager(self.paths)
        self.library.load()
        self.loudness = LoudnessProcessor()
        self.linkplay = LinkPlayClient(self.config)
        self.sync = SyncService(self.config, self.paths, self.library, self.loudness, self.state_store, self.events, SpotDLAdapter(self.paths))
        self.playback = PlaybackSupervisor(self.config, self.paths, self.library, self.linkplay, self.state_store, self.events)
        self.scheduler = SyncScheduler(self.config, self.sync, self.events)
        self.mqtt = MqttService(self.config, self.events, self._request_sync_from_mqtt)
        self.diagnostics_service = DiagnosticsService(self.config, self.paths, self.library, self.loudness, self.linkplay, self.sync, self.state_store, self.events, self.mqtt)
        self._tasks: list[asyncio.Task[Any]] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.sync.recover_interrupted()
        if os.environ.get("APP_DISABLE_BACKGROUND", "").casefold() not in {"1", "true", "yes"}:
            self._tasks = [asyncio.create_task(self.scheduler.run(), name="tecnohead-audio-scheduler")]
            if self.config.playback.supervise:
                self._tasks.append(asyncio.create_task(self.playback.run(), name="tecnohead-audio-playback"))
            self.mqtt.start(asyncio.get_running_loop())

    async def stop(self) -> None:
        self.mqtt.stop()
        self.scheduler.stop()
        self.playback.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.started = False

    def apply_config(self, config: AppConfig) -> AppConfig:
        self.config = self.config_store.save(config)
        self.linkplay.update_config(self.config)
        self.sync.update_config(self.config)
        self.playback.update_config(self.config)
        self.scheduler.update_config(self.config)
        self.diagnostics_service.update_config(self.config)
        self.mqtt.update_config(self.config)
        return self.config

    def public_config(self) -> dict[str, Any]:
        return public_config(self.config)

    def state_payload(self) -> dict[str, Any]:
        playback = self.state_store.load_playback()
        current = self.library.get(playback.current_track_id) if playback.current_track_id else None
        return {
            "app": {"version": __import__("app").__version__, "started": self.started, "timezone": self.config.timezone},
            "window": {
                "start": self.config.playback.window_start,
                "end": self.config.playback.window_end,
                "timezone": self.config.timezone,
                "open": playback.window_open,
            },
            "library": self.library.summary(),
            "sync": self.sync.current_state().model_dump(mode="json"),
            "playback": playback.model_dump(mode="json"),
            "current_track": current.model_dump(mode="json") if current else None,
            "devices": playback.devices,
            "alerts": [playback.last_error] if playback.last_error else [],
        }

    async def diagnostics(self) -> dict[str, Any]:
        return await self.diagnostics_service.run()

    def tracks(self, query: str = "", status: str | None = None) -> list[Track]:
        items = self.library.all()
        query = query.casefold().strip()
        if query:
            items = [item for item in items if query in " ".join((item.title, item.artist, item.album)).casefold()]
        if status:
            items = [item for item in items if item.status.value == status]
        return items

    async def save_public_config(self, payload: ConfigUpdate) -> AppConfig:
        current = self.config
        data = current.model_dump(mode="json")
        public_data = payload.model_dump(mode="json")
        data.update({key: value for key, value in public_data.items() if key != "media"})
        data["media"] = {**data.get("media", {}), **public_data["media"]}
        data["mqtt"] = {**data.get("mqtt", {}), **public_data["mqtt"], "password": current.mqtt.password}
        # The Supervisor publishes the dedicated media-only port from the
        # manifest. Keep it fixed; only the LAN base URL is user-configurable.
        data["media"]["port"] = 18198
        return self.apply_config(AppConfig.model_validate(data))

    def _request_sync_from_mqtt(self) -> None:
        try:
            task = self.sync.start(reason="mqtt")
        except Exception as exc:
            self.events.append("mqtt_sync_rejected", "solicitação MQTT não iniciou a sincronização", error=str(exc))
            self.mqtt.publish_sync_status("failed")
            return
        self.mqtt.publish_sync_status("running")
        self.events.append("sync_mqtt", "sincronização iniciada pelo botão MQTT")
        task.add_done_callback(self._finish_mqtt_sync)

    def _finish_mqtt_sync(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception:
            pass
        self.mqtt.publish_sync_status(self.sync.current_state().status)

