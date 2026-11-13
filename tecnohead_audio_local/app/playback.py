from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.library import LibraryManager
from app.linkplay import LinkPlayClient, LinkPlayError
from app.media import media_url, playlist_url
from app.models import AppConfig, PlaybackState, TrackStatus
from app.scheduler import is_within_window
from app.settings import AppPaths, EventLog, StateStore, redact_text, utc_now

LOGGER = logging.getLogger("tecnohead_audio_local.playback")


class PlaybackError(RuntimeError):
    pass


class PlaybackSupervisor:
    def __init__(self, config: AppConfig, paths: AppPaths, library: LibraryManager, linkplay: LinkPlayClient, state_store: StateStore, events: EventLog):
        self.config = config
        self.paths = paths
        self.library = library
        self.linkplay = linkplay
        self.state_store = state_store
        self.events = events
        self.stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self.linkplay.update_config(config)

    def state(self) -> PlaybackState:
        return self.state_store.load_playback()

    def _save(self, state: PlaybackState) -> None:
        self.state_store.save_playback(state)

    def _in_window(self, moment: datetime | None = None) -> bool:
        now = moment or datetime.now(ZoneInfo(self.config.timezone))
        return self.config.playback.enabled and is_within_window(now.astimezone(ZoneInfo(self.config.timezone)), self.config.playback.window_start, self.config.playback.window_end)

    def _device_ids(self) -> tuple[str, list[str]]:
        enabled = [device.device_id for device in self.config.devices if device.enabled]
        if not enabled:
            raise PlaybackError("nenhum AudioCast está habilitado")
        return enabled[0], enabled[1:]

    def _media_url(self, entry_id: str) -> str:
        try:
            return media_url(self.config, entry_id)
        except ValueError as exc:
            raise PlaybackError(str(exc)) from exc

    def _playlist_url(self, start_entry_id: str) -> str:
        try:
            return playlist_url(self.config, start_entry_id=start_entry_id, shuffle=self.config.playback.shuffle)
        except ValueError as exc:
            raise PlaybackError(str(exc)) from exc

    def _current_track(self, state: PlaybackState):
        if state.current_track_id:
            track = self.library.get(state.current_track_id)
            if track and track.status is TrackStatus.READY and track.relative_path:
                return track
        tracks = self.library.ready_tracks(shuffle=self.config.playback.shuffle)
        return tracks[0] if tracks else None

    def _next_track(self, state: PlaybackState, direction: int = 1):
        tracks = self.library.ready_tracks(shuffle=False)
        if not tracks:
            return None
        index = next((idx for idx, item in enumerate(tracks) if item.entry_id == state.current_track_id), -1)
        if direction < 0:
            index = (index - 1) % len(tracks)
        else:
            index = (index + 1) % len(tracks)
        return tracks[index]

    async def _stop_devices(self, device_ids: list[str]) -> None:
        for device_id in device_ids:
            try:
                await self.linkplay.stop(device_id)
            except LinkPlayError as exc:
                self.events.append("playback_stop_failed", "falha ao encerrar AudioCast", device_id=device_id, error=redact_text(str(exc)))

    async def _start_track(self, state: PlaybackState, track, *, reason: str) -> PlaybackState:
        master_id, members = self._device_ids()
        ready_tracks = self.library.ready_tracks(shuffle=self.config.playback.shuffle)
        playlist = self._playlist_url(track.entry_id) if len(ready_tracks) > 1 else None
        result = await self.linkplay.play_group(
            master_id,
            members,
            self._media_url(track.entry_id),
            loop=self.config.playback.loop,
            shuffle=self.config.playback.shuffle,
            volume_percent=self.config.playback.volume_percent,
            playlist_url=playlist,
        )
        state.active = True
        state.playlist_mode = bool(playlist)
        state.current_track_id = track.entry_id
        state.current_position = 0
        state.last_action = f"play:{reason}"
        state.last_command_confirmed_at = utc_now()
        state.last_success_at = utc_now()
        state.last_error = None
        state.failure_count = 0
        state.group_confirmed = bool(result.get("confirmed"))
        self.events.append("playback_started", "faixa enviada ao grupo LinkPlay", entry_id=track.entry_id, reason=reason, group_confirmed=state.group_confirmed)
        return state

    async def tick(self, moment: datetime | None = None) -> PlaybackState:
        async with self._lock:
            now = moment or datetime.now(ZoneInfo(self.config.timezone))
            state = self.state()
            in_window = self._in_window(now)
            state.window_open = in_window
            if not in_window:
                if state.active:
                    await self._stop_devices([device.device_id for device in self.config.devices if device.enabled])
                    state.active = False
                    state.group_confirmed = False
                    state.playlist_mode = False
                    state.last_action = "window_closed"
                    state.last_command_confirmed_at = utc_now()
                    self.events.append("playback_window_closed", "reprodução encerrada fora da janela configurada")
                state.supervised = False
                self._save(state)
                return state
            state.supervised = self.config.playback.supervise
            tracks = self.library.ready_tracks(shuffle=False)
            if not tracks:
                state.active = False
                state.playlist_mode = False
                state.last_error = "a biblioteca não possui faixas prontas"
                self._save(state)
                return state
            track = self._current_track(state)
            if track is None:
                track = tracks[0]
            try:
                statuses = await self.linkplay.status_all()
                state.devices = statuses
                master_id, _members = self._device_ids()
                master_status = statuses.get(master_id, {})
                status_value = str(master_status.get("status", "unknown")).casefold()
                status_title = str(master_status.get("title", "")).strip().casefold()
                status_artist = str(master_status.get("artist", "")).strip().casefold()
                if state.playlist_mode and status_title:
                    matched = next(
                        (
                            item
                            for item in tracks
                            if item.title.casefold() == status_title and (not status_artist or item.artist.casefold() == status_artist)
                        ),
                        None,
                    )
                    if matched is None:
                        matched = next((item for item in tracks if item.title.casefold() == status_title), None)
                    if matched is not None:
                        state.current_track_id = matched.entry_id
                        track = matched
                duration = track.duration_seconds or 0
                position = int(master_status.get("position_ms") or 0) / 1000
                ended = (not state.playlist_mode and bool(duration and position >= max(0, duration - 2))) or status_value in {"stop", "stopped"}
                expected_devices = {device.device_id for device in self.config.devices if device.enabled}
                device_degraded = any(not statuses.get(device_id, {}).get("online", False) for device_id in expected_devices)
                needs_start = not state.active or not state.group_confirmed or state.current_track_id != track.entry_id or status_value in {"unknown", "pause", "paused"} or ended or device_degraded
                if ended and state.current_track_id == track.entry_id and len(tracks) > 1:
                    track = self._next_track(state) or track
                if needs_start:
                    state = await self._start_track(state, track, reason="supervisor")
                else:
                    state.current_position = int(master_status.get("position_ms") or 0)
                    state.last_error = None
                    state.last_success_at = utc_now()
                self._save(state)
            except (LinkPlayError, PlaybackError) as exc:
                state.failure_count += 1
                state.last_error = redact_text(str(exc))
                self.events.append("playback_supervision_failed", "supervisão não confirmou o grupo AudioCast", error=state.last_error, failure_count=state.failure_count)
                self._save(state)
            return state

    async def action(self, action: str) -> PlaybackState:
        async with self._lock:
            state = self.state()
            master_id, members = self._device_ids()
            if action == "play":
                if not self._in_window():
                    raise PlaybackError("a reprodução só pode ser iniciada entre 05:30 e 21:30")
                track = self._current_track(state)
                if not track:
                    raise PlaybackError("a biblioteca não possui faixas prontas")
                state = await self._start_track(state, track, reason="manual")
            elif action == "pause":
                await self.linkplay.pause(master_id)
                state.active = False
                state.playlist_mode = False
                state.last_action = "pause"
                state.last_command_confirmed_at = utc_now()
            elif action == "stop":
                await self._stop_devices([master_id, *members])
                state.active = False
                state.playlist_mode = False
                state.last_action = "stop"
                state.last_command_confirmed_at = utc_now()
            elif action in {"next", "previous"}:
                if not self._in_window():
                    raise PlaybackError("a reprodução só pode ser alterada fora da janela apenas para pausa/parada")
                track = self._next_track(state, -1 if action == "previous" else 1)
                if not track:
                    raise PlaybackError("a biblioteca não possui faixas prontas")
                state = await self._start_track(state, track, reason=action)
            else:
                raise PlaybackError("ação de reprodução não permitida")
            state.last_success_at = utc_now()
            state.last_error = None
            self._save(state)
            self.events.append("playback_action", "ação de reprodução confirmada pelo cliente LinkPlay", action=action)
            return state

    async def set_volume(self, volume_percent: int) -> PlaybackState:
        if not 0 <= volume_percent <= 100:
            raise PlaybackError("volume deve estar entre 0 e 100")
        master_id, members = self._device_ids()
        if volume_percent > min(device.volume_percent for device in self.config.devices if device.enabled):
            raise PlaybackError("volume acima do limite configurado de um AudioCast")
        await self.linkplay.set_group_volume(master_id, members, volume_percent)
        state = self.state()
        state.last_action = f"volume:{volume_percent}"
        state.last_command_confirmed_at = utc_now()
        state.last_success_at = utc_now()
        self._save(state)
        return state

    async def test_device(self, device_id: str) -> dict:
        return {"status": await self.linkplay.status(device_id), "identity": await self.linkplay.identity(device_id)}

    async def test_group(self) -> dict:
        master_id, members = self._device_ids()
        return await self.linkplay.check_group(master_id, members)

    async def run(self) -> None:
        while not self.stop_event.is_set():
            if self.config.playback.supervise:
                await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.config.playback.supervision_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self.stop_event.set()

