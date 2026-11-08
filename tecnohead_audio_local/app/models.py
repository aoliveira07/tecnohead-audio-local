from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from datetime import time
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}[A-Za-z0-9]$|^[A-Za-z0-9]$")
SECRET_QUERY_KEYS = {"token", "access_token", "client_secret", "secret", "password", "cookie", "auth"}


class TrackStatus(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    NORMALIZING = "normalizing"
    READY = "ready"
    FAILED = "failed"
    REMOVED = "removed"


class WindowEndAction(StrEnum):
    STOP = "stop"
    PAUSE = "pause"


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError("horário deve usar o formato HH:MM, por exemplo 05:30")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def validate_host(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 253 or any(char in value for char in "/?#\\\t\r\n"):
        raise ValueError("host inválido")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if not HOST_RE.fullmatch(value):
            raise ValueError("host deve ser um IP ou nome DNS simples") from None
        return value


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("URL base de mídia deve ser HTTP(S), sem query ou fragmento")
    return value


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[redigido]"
    if not parsed.scheme or not parsed.netloc:
        return value
    query = [(key, "[redigido]" if key.casefold() in SECRET_QUERY_KEYS else val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class NormalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    target_lufs: float
    true_peak_db: float
    input_lufs: float | None = None
    input_lra: float | None = None
    input_true_peak_db: float | None = None
    input_threshold: float | None = None
    target_offset: float | None = None
    duration_seconds: float | None = None
    output_format: str
    completed_at: str


class PlaylistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str = Field(min_length=16, max_length=64)
    source_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    artist: str = Field(default="", max_length=512)
    album: str = Field(default="", max_length=512)
    duration_seconds: float | None = Field(default=None, ge=0, le=24 * 60 * 60)
    position: int = Field(ge=0, le=100_000)
    source_url: str | None = Field(default=None, max_length=2048)

    @field_validator("source_url")
    @classmethod
    def source_url_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url inválida")
        return value


class Track(PlaylistEntry):
    status: TrackStatus = TrackStatus.DISCOVERED
    relative_path: str | None = None
    download_relative_path: str | None = None
    file_sha256: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    normalization: NormalizationResult | None = None
    last_synced_at: str | None = None
    error: str | None = Field(default=None, max_length=1_000)
    attempts: int = Field(default=0, ge=0, le=100_000)


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    host: str
    port: int = Field(default=80, ge=1, le=65_535)
    enabled: bool = True
    volume_percent: int = Field(default=90, ge=0, le=100)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    https: bool = False

    _validate_host = field_validator("host")(validate_host)


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    times: list[str] = Field(default_factory=lambda: ["03:00", "15:00"], min_length=1, max_length=8)
    max_parallel_tracks: int = Field(default=1, ge=1, le=2)

    @field_validator("times")
    @classmethod
    def times_must_be_valid(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        for value in normalized:
            parse_hhmm(value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("os horários de sincronização não podem se repetir")
        return sorted(normalized)


class PlaybackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    window_start: str = "05:30"
    window_end: str = "21:30"
    loop: bool = True
    shuffle: bool = False
    volume_percent: int = Field(default=90, ge=0, le=100)
    supervise: bool = True
    supervision_interval_seconds: int = Field(default=60, ge=15, le=900)
    window_end_action: WindowEndAction = WindowEndAction.STOP

    @field_validator("window_start", "window_end")
    @classmethod
    def window_time_must_be_valid(cls, value: str) -> str:
        parse_hhmm(value)
        return value


class NormalizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target_lufs: float = Field(default=-18.0, ge=-40.0, le=-5.0)
    true_peak_db: float = Field(default=-2.0, ge=-6.0, le=0.0)
    lra: float = Field(default=11.0, gt=0.0, le=50.0)
    output_format: str = Field(default="mp3", pattern="^(mp3|m4a|ogg|opus|flac|wav)$")
    bitrate: str = Field(default="192k", pattern=r"^(\d{2,3}k|auto)$")


class LinkPlayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_path: str = Field(default="/httpapi.asp", pattern=r"^/[A-Za-z0-9_./-]+$")
    retry_count: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=0.3, ge=0.0, le=5.0)
    verify_group_after_join: bool = True
    join_command_template: str = "ConnectMasterAp:JoinGroupMaster:eth{master_host}:wifi0.0.0.0"


class MediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    port: int = Field(default=18198, ge=1, le=65_535)
    stream_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32), min_length=32, max_length=128)

    _validate_base_url = field_validator("base_url")(validate_base_url)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    installation_id: str = Field(default_factory=lambda: secrets.token_hex(16), min_length=16, max_length=64)
    timezone: str = "America/Sao_Paulo"
    playlist_url: str = Field(default="", max_length=2048)
    spotify_client_id: str = Field(default="", max_length=256)
    spotify_client_secret: str = Field(default="", max_length=512)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)
    linkplay: LinkPlayConfig = Field(default_factory=LinkPlayConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    devices: list[DeviceConfig] = Field(
        default_factory=lambda: [
            DeviceConfig(device_id="caixa", name="Caixa", host="192.168.0.13"),
            DeviceConfig(device_id="restaurante", name="Restaurante", host="192.168.0.14"),
        ],
        min_length=1,
        max_length=8,
    )

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:  # pragma: no cover - platform tzdata failure
            raise ValueError(f"fuso horário inválido: {value}") from exc
        return value

    @field_validator("playlist_url")
    @classmethod
    def playlist_url_must_be_http(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("playlist deve ser uma URL HTTP(S)")
        return value

    @model_validator(mode="after")
    def validate_devices(self) -> AppConfig:
        ids = [device.device_id for device in self.devices]
        hosts = [f"{device.host}:{device.port}" for device in self.devices]
        if len(set(ids)) != len(ids):
            raise ValueError("device_id dos AudioCast não pode se repetir")
        if len(set(hosts)) != len(hosts):
            raise ValueError("os AudioCast não podem repetir host e porta")
        if self.playback.volume_percent > max(device.volume_percent for device in self.devices):
            raise ValueError("volume da reprodução excede o limite de pelo menos um dispositivo")
        return self


class PublicMediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    port: int = Field(ge=1, le=65_535)

    _validate_base_url = field_validator("base_url")(validate_base_url)


class ConfigUpdate(BaseModel):
    """Public configuration shape. Secret fields intentionally do not exist."""

    model_config = ConfigDict(extra="forbid")

    timezone: str
    playlist_url: str
    sync: SyncConfig
    playback: PlaybackConfig
    normalization: NormalizationConfig
    linkplay: LinkPlayConfig
    media: PublicMediaConfig
    devices: list[DeviceConfig] = Field(min_length=1, max_length=8)


class CredentialUpdate(BaseModel):
    """Write-only credential fields exposed through the Ingress UI."""

    model_config = ConfigDict(extra="forbid")

    spotify_client_id: str | None = Field(default=None, max_length=256)
    spotify_client_secret: str | None = Field(default=None, max_length=512)
    clear: bool = False

    @field_validator("spotify_client_id", "spotify_client_secret")
    @classmethod
    def blank_values_preserve_existing(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SyncCounts(BaseModel):
    downloaded: int = 0
    normalized: int = 0
    skipped: int = 0
    failed: int = 0
    removed: int = 0


class SyncState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "idle"
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    progress_current: int = 0
    progress_total: int = 0
    counts: SyncCounts = Field(default_factory=SyncCounts)
    error: str | None = None
    lock_started_at: str | None = None


class PlaybackState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = False
    window_open: bool = False
    supervised: bool = False
    current_track_id: str | None = None
    current_position: int = 0
    last_action: str | None = None
    last_command_confirmed_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    failure_count: int = 0
    group_confirmed: bool = False
    devices: dict[str, dict[str, Any]] = Field(default_factory=dict)


def entry_id_for(*, source_id: str, title: str, artist: str, duration_seconds: float | None) -> str:
    normalized_source = source_id.strip().casefold()
    # A real playlist/provider identifier is stable even when title metadata
    # changes. The position fallback is the only case where metadata is part
    # of the identity because there is no provider key to use.
    if normalized_source.startswith("position:"):
        canonical = "|".join([normalized_source, title.strip().casefold(), artist.strip().casefold(), "" if duration_seconds is None else f"{duration_seconds:.3f}"])
    else:
        canonical = normalized_source
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def public_config(config: AppConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    data.pop("installation_id", None)
    data.pop("schema_version", None)
    data.pop("spotify_client_id", None)
    data.pop("spotify_client_secret", None)
    data["playlist_url"] = redact_url(config.playlist_url) if config.playlist_url else ""
    data["credentials"] = {
        "spotify_client_id_configured": bool(config.spotify_client_id),
        "spotify_client_secret_configured": bool(config.spotify_client_secret),
    }
    data["media"] = {"base_url": config.media.base_url, "port": config.media.port, "stream_token_configured": True}
    return data


def playlist_entry_from_mapping(raw: dict[str, Any], position: int) -> PlaylistEntry:
    """Normalize the common .spotdl JSON shapes without trusting filenames."""

    nested = raw.get("song") if isinstance(raw.get("song"), dict) else raw
    artists = nested.get("artists") or nested.get("artist") or ""
    if isinstance(artists, list):
        parts = []
        for artist in artists:
            parts.append(str(artist.get("name", artist) if isinstance(artist, dict) else artist))
        artists = ", ".join(parts)
    title = str(nested.get("name") or nested.get("title") or "Faixa sem título").strip()
    artist = str(artists).strip()
    album_value = nested.get("album") or nested.get("album_name") or ""
    album = str(album_value.get("name", "") if isinstance(album_value, dict) else album_value).strip()
    raw_duration = nested.get("duration_seconds")
    if raw_duration is None:
        raw_duration = nested.get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    source_id = str(nested.get("song_id") or nested.get("track_id") or nested.get("spotify_id") or nested.get("id") or nested.get("url") or f"position:{position}").strip()
    source_url = nested.get("url") or nested.get("spotify_url")
    source_url = str(source_url) if source_url else None
    return PlaylistEntry(
        entry_id=entry_id_for(source_id=source_id, title=title, artist=artist, duration_seconds=duration),
        source_id=source_id,
        title=title,
        artist=artist,
        album=album,
        duration_seconds=duration,
        position=position,
        source_url=source_url,
    )

