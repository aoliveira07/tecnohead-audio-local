from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from app.models import AppConfig, PlaybackState, SyncState, public_config

LOGGER = logging.getLogger("tecnohead_audio_local.settings")
T = TypeVar("T")
SENSITIVE_NAME_RE = re.compile(r"(?i)(token|secret|password|cookie|authorization|client_secret|api_key)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_text(value: str) -> str:
    if not value:
        return value
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redigido]", value)
    value = re.sub(r"(?i)((?:token|secret|password|cookie|authorization|api[_-]?key)[=:]\s*)[^\s,;]+", r"\1[redigido]", value)
    return value[:2_000]


def redact_object(value: Any, *, key: str = "") -> Any:
    if SENSITIVE_NAME_RE.search(key):
        return "[redigido]" if value else ""
    if isinstance(value, dict):
        return {str(k): redact_object(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_object(item, key=key) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@dataclass(frozen=True)
class AppPaths:
    data: Path
    media: Path
    state: Path
    cache: Path
    downloads: Path
    library: Path
    quarantine: Path
    tmp: Path
    config_file: Path
    library_index_file: Path
    sync_state_file: Path
    playback_state_file: Path
    events_file: Path
    lock_file: Path
    playlist_file: Path

    @classmethod
    def from_environment(cls) -> AppPaths:
        data = Path(os.environ.get("APP_DATA_DIR", "/data")).resolve()
        media = Path(os.environ.get("APP_MEDIA_DIR", "/media")).resolve()
        root = media / "tecnohead-audio-local"
        state = data / "state"
        cache = data / "cache"
        downloads = cache / "downloads"
        library = root / "library"
        quarantine = root / "quarantine"
        tmp = root / "tmp"
        return cls(
            data=data,
            media=media,
            state=state,
            cache=cache,
            downloads=downloads,
            library=library,
            quarantine=quarantine,
            tmp=tmp,
            config_file=data / "config.json",
            library_index_file=state / "library.json",
            sync_state_file=state / "sync.json",
            playback_state_file=state / "playback.json",
            events_file=state / "events.jsonl",
            lock_file=state / "sync.lock",
            playlist_file=library / "playlist.m3u8",
        )

    def ensure(self) -> None:
        for path in (
            self.data,
            self.state,
            self.cache,
            self.downloads,
            self.library,
            self.quarantine,
            self.tmp,
        ):
            path.mkdir(parents=True, exist_ok=True)


class JsonFile:
    def __init__(self, path: Path, default_factory: Callable[[], T]):
        self.path = path
        self.default_factory = default_factory
        self._lock = RLock()

    def read_raw(self) -> Any:
        with self._lock:
            if not self.path.exists():
                return self.default_factory()
            return json.loads(self.path.read_text(encoding="utf-8"))

    def write_raw(self, value: Any, *, mode: int | None = None) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f"{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
                if mode is not None:
                    os.chmod(self.path, mode)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)


class ConfigStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.file = JsonFile(paths.config_file, lambda: AppConfig().model_dump(mode="json"))
        self._lock = RLock()
        self._config: AppConfig | None = None

    def load(self) -> AppConfig:
        with self._lock:
            self.paths.ensure()
            if not self.paths.config_file.exists():
                config = AppConfig()
                self._config = config
                self.save(config)
                return config.model_copy(deep=True)
            try:
                raw = self.file.read_raw()
                if not isinstance(raw, dict):
                    raise ValueError("raiz da configuração deve ser um objeto")
                raw = self._migrate(raw)
                config = AppConfig.model_validate(raw)
            except (OSError, ValueError, TypeError, ValidationError) as exc:
                backup = self.paths.config_file.with_suffix(f".invalid-{secrets.token_hex(4)}.json")
                try:
                    self.paths.config_file.replace(backup)
                except OSError:
                    backup = None
                LOGGER.error("configuração inválida; defaults seguros foram carregados: %s", redact_text(str(exc)))
                if backup:
                    LOGGER.warning("configuração inválida movida para arquivo privado %s", backup.name)
                config = AppConfig()
            self._config = config
            self.save(config)
            return config.model_copy(deep=True)

    @staticmethod
    def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        data.setdefault("schema_version", 2)
        data.setdefault("installation_id", secrets.token_hex(16))
        data.setdefault("sync", {})
        data.setdefault("playback", {})
        data.setdefault("normalization", {})
        data.setdefault("linkplay", {})
        data.setdefault("media", {})
        data.setdefault("mqtt", {})
        data.setdefault("devices", AppConfig().model_dump(mode="json")["devices"])
        # v0.1.x exposed Spotify credentials even though playlist discovery
        # uses the public page/embed fallback. Drop those fields permanently.
        data.pop("spotify_client_id", None)
        data.pop("spotify_client_secret", None)
        # The first production default was 03:00/15:00. Migrate that untouched
        # default to the current 11:00/15:00 schedule; custom schedules stay
        # exactly as configured by the user.
        sync_data = data.get("sync")
        if isinstance(sync_data, dict) and sorted(sync_data.get("times") or []) == ["03:00", "15:00"]:
            sync_data["times"] = ["11:00", "15:00"]
        data["schema_version"] = 2
        return data

    def get(self) -> AppConfig:
        with self._lock:
            if self._config is None:
                return self.load()
            return self._config.model_copy(deep=True)

    def save(self, config: AppConfig) -> AppConfig:
        with self._lock:
            config.schema_version = 2
            self.file.write_raw(config.model_dump(mode="json"), mode=0o600)
            self._config = config.model_copy(deep=True)
            return self._config.model_copy(deep=True)

    def public(self) -> dict[str, Any]:
        return public_config(self.get())


class StateStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.sync = JsonFile(paths.sync_state_file, lambda: SyncState().model_dump(mode="json"))
        self.playback = JsonFile(paths.playback_state_file, lambda: PlaybackState().model_dump(mode="json"))

    def load_sync(self) -> SyncState:
        try:
            return SyncState.model_validate(self.sync.read_raw())
        except (ValueError, TypeError, ValidationError):
            return SyncState(status="failed", error="estado de sincronização inválido; reiniciado")

    def save_sync(self, state: SyncState) -> None:
        self.sync.write_raw(state.model_dump(mode="json"), mode=0o600)

    def load_playback(self) -> PlaybackState:
        try:
            return PlaybackState.model_validate(self.playback.read_raw())
        except (ValueError, TypeError, ValidationError):
            return PlaybackState(last_error="estado de reprodução inválido; reiniciado")

    def save_playback(self, state: PlaybackState) -> None:
        self.playback.write_raw(state.model_dump(mode="json"), mode=0o600)


class EventLog:
    def __init__(self, paths: AppPaths, *, max_entries: int = 500):
        self.path = paths.events_file
        self.max_entries = max_entries
        self._lock = RLock()

    def append(self, event: str, message: str, **details: Any) -> None:
        record = redact_object({"at": utc_now(), "event": event, "message": message, **details})
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()[-self.max_entries :]
                self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                os.chmod(self.path, 0o600)
            except OSError:
                LOGGER.warning("não foi possível compactar o log de eventos")

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self.max_entries))
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(redact_object(value))
            except json.JSONDecodeError:
                continue
        return result

