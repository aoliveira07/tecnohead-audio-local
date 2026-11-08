from __future__ import annotations

import json
import shutil
from typing import Any

from app.library import LibraryManager
from app.linkplay import LinkPlayClient
from app.loudness import LoudnessProcessor
from app.models import AppConfig, public_config
from app.settings import AppPaths, EventLog, StateStore, redact_object
from app.sync_service import SyncService


class DiagnosticsService:
    def __init__(self, config: AppConfig, paths: AppPaths, library: LibraryManager, loudness: LoudnessProcessor, linkplay: LinkPlayClient, sync: SyncService, state: StateStore, events: EventLog):
        self.config = config
        self.paths = paths
        self.library = library
        self.loudness = loudness
        self.linkplay = linkplay
        self.sync = sync
        self.state = state
        self.events = events

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def _space(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.paths.media)
            return {"state": "ok", "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
        except OSError as exc:
            return {"state": "error", "error": str(exc)}

    def _write_checks(self) -> dict[str, Any]:
        checks = {}
        for name, path in {"data": self.paths.data, "library": self.paths.library, "quarantine": self.paths.quarantine, "tmp": self.paths.tmp}.items():
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".tecnohead-write-check"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                checks[name] = {"state": "ok"}
            except OSError as exc:
                checks[name] = {"state": "error", "error": str(exc)}
        return checks

    async def run(self) -> dict[str, Any]:
        status = await self.linkplay.status_all()
        group = None
        enabled = [device.device_id for device in self.config.devices if device.enabled]
        if enabled:
            try:
                group = await self.linkplay.check_group(enabled[0], enabled[1:])
            except Exception as exc:
                group = {"confirmed": False, "error": str(exc)}
        payload = {
            "app": {"state": "ok", "version": __import__("app").__version__},
            "configuration": public_config(self.config),
            "paths": {
                "data": str(self.paths.data),
                "library": str(self.paths.library),
                "quarantine": str(self.paths.quarantine),
                "tmp": str(self.paths.tmp),
            },
            "tools": {
                "spotdl": {"state": "available" if shutil.which(self.sync.adapter.executable) else "missing"},
                "ffmpeg": {"state": "available" if self.loudness.available() else "missing"},
            },
            "storage": {"space": self._space(), "permissions": self._write_checks()},
            "devices": status,
            "group": group,
            "supervisor": {"state": "not_used", "reason": "este App não depende da API do Supervisor para operar"},
            "library": self.library.summary(),
            "sync": self.sync.current_state().model_dump(mode="json"),
            "playback": self.state.load_playback().model_dump(mode="json"),
            "notes": [
                "A resposta LinkPlay confirma apenas a API HTTP; áudio audível exige homologação física.",
                "O endpoint de mídia é separado do Ingress e não oferece API administrativa.",
            ],
        }
        return redact_object(payload)

    def export_json(self, payload: dict[str, Any]) -> bytes:
        return (json.dumps(redact_object(payload), ensure_ascii=False, indent=2) + "\n").encode("utf-8")

