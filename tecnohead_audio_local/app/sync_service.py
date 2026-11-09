from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from app.library import LibraryManager, playlist_diff
from app.loudness import LoudnessProcessor
from app.models import AppConfig, PlaylistEntry, SyncState, TrackStatus, playlist_entry_from_mapping
from app.settings import AppPaths, EventLog, StateStore, redact_text, utc_now

LOGGER = logging.getLogger("tecnohead_audio_local.sync")


class SyncBusyError(RuntimeError):
    pass


class SyncError(RuntimeError):
    pass


def _extract_song_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("songs", "tracks", "playlist", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_song_rows(value)
            if nested:
                return nested
    return []


def parse_spotdl_payload(text: str) -> list[PlaylistEntry]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError("arquivo .spotdl não contém JSON válido") from exc
    rows = _extract_song_rows(payload)
    if not rows:
        raise SyncError("arquivo .spotdl não contém faixas")
    entries: list[PlaylistEntry] = []
    for position, row in enumerate(rows):
        entries.append(playlist_entry_from_mapping(row, position))
    return entries


class SpotDLAdapter:
    def __init__(self, paths: AppPaths, *, executable: str = "spotdl"):
        self.paths = paths
        self.executable = executable

    @staticmethod
    def build_save_command(executable: str, playlist_url: str, save_file: Path) -> list[str]:
        return [executable, "save", playlist_url, "--save-file", str(save_file)]

    @staticmethod
    def build_sync_command(executable: str, save_file: Path, download_dir: Path, output_format: str) -> list[str]:
        output_template = str(download_dir / "{list-position}-{track-id}.{output-ext}")
        return [
            executable,
            "sync",
            str(save_file),
            "--output",
            output_template,
            "--format",
            output_format,
            "--overwrite",
            "skip",
            "--sync-without-deleting",
            "--print-errors",
        ]

    async def _run(self, command: list[str], config: AppConfig) -> str:
        environment = os.environ.copy()
        if config.spotify_client_id:
            environment["SPOTIPY_CLIENT_ID"] = config.spotify_client_id
            environment["SPOTIFY_CLIENT_ID"] = config.spotify_client_id
        if config.spotify_client_secret:
            environment["SPOTIPY_CLIENT_SECRET"] = config.spotify_client_secret
            environment["SPOTIFY_CLIENT_SECRET"] = config.spotify_client_secret
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.paths.cache),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except OSError as exc:
            raise SyncError(f"não foi possível executar spotDL: {redact_text(str(exc))}") from exc
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise SyncError(f"spotDL falhou com código {process.returncode}: {redact_text(output)}")
        return output

    async def fetch_playlist(self, config: AppConfig) -> list[PlaylistEntry]:
        if not config.playlist_url:
            raise SyncError("configure a URL da playlist antes de sincronizar")
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        save_file = self.paths.cache / "playlist.spotdl"
        await self._run(self.build_save_command(self.executable, config.playlist_url, save_file), config)
        try:
            return parse_spotdl_payload(save_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SyncError("spotDL não gerou o arquivo de metadados da playlist") from exc

    @staticmethod
    def _source_tokens(entry: PlaylistEntry) -> set[str]:
        source = entry.source_id.replace(":", "/")
        return {part.casefold() for part in source.split("/") if len(part) >= 4}

    async def download(self, config: AppConfig, entries: list[PlaylistEntry]) -> dict[str, Path]:
        self.paths.downloads.mkdir(parents=True, exist_ok=True)
        save_file = self.paths.cache / "playlist.spotdl"
        await self._run(self.build_sync_command(self.executable, save_file, self.paths.downloads, config.normalization.output_format), config)
        candidates = [path for path in self.paths.downloads.rglob("*") if path.is_file() and path.suffix.casefold().lstrip(".") in {"mp3", "m4a", "ogg", "opus", "flac", "wav"}]
        result: dict[str, Path] = {}
        unused = set(candidates)
        for entry in sorted(entries, key=lambda item: item.position):
            token_matches = [path for path in unused if any(token in path.stem.casefold() for token in self._source_tokens(entry))]
            position_matches = [path for path in unused if path.stem.split("-", 1)[0].strip() in {str(entry.position), str(entry.position + 1)}]
            choices = token_matches or position_matches
            if len(choices) == 1:
                result[entry.entry_id] = choices[0]
                unused.remove(choices[0])
        return result


class PersistentSyncLock:
    def __init__(self, path: Path, *, stale_after_seconds: int = 12 * 60 * 60):
        self.path = path
        self.stale_after_seconds = stale_after_seconds

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"started_at": utc_now(), "pid": os.getpid()}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            try:
                age = max(0, int(os.path.getmtime(self.path)))
                now = int(__import__("time").time())
                if now - age > self.stale_after_seconds:
                    self.path.unlink(missing_ok=True)
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                else:
                    raise SyncBusyError("já existe uma sincronização em andamento") from exc
            except FileNotFoundError:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


class SyncService:
    def __init__(self, config: AppConfig, paths: AppPaths, library: LibraryManager, loudness: LoudnessProcessor, state: StateStore, events: EventLog, adapter: SpotDLAdapter | None = None):
        self.config = config
        self.paths = paths
        self.library = library
        self.loudness = loudness
        self.state_store = state
        self.events = events
        self.adapter = adapter or SpotDLAdapter(paths)
        self.lock = PersistentSyncLock(paths.lock_file)
        self._async_lock = asyncio.Lock()
        self._task: asyncio.Task[SyncState] | None = None

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def current_state(self) -> SyncState:
        return self.state_store.load_sync()

    def recover_interrupted(self) -> None:
        state = self.state_store.load_sync()
        if state.status == "running":
            state.status = "failed"
            state.error = "sincronização interrompida por reinício; as faixas podem ser tentadas novamente"
            state.finished_at = utc_now()
            self.state_store.save_sync(state)
        self.paths.lock_file.unlink(missing_ok=True)

    def start(self, reason: str = "manual") -> asyncio.Task[SyncState]:
        if self._task and not self._task.done():
            raise SyncBusyError("já existe uma sincronização em andamento")
        if reason != "reprocess" and not self.config.playlist_url:
            raise SyncError("configure a URL da playlist antes de sincronizar")
        coroutine = self.reprocess() if reason == "reprocess" else self.run(reason)
        self._task = asyncio.create_task(coroutine, name="tecnohead-audio-reprocess" if reason == "reprocess" else "tecnohead-audio-sync")
        return self._task

    async def run(self, reason: str = "manual") -> SyncState:
        async with self._async_lock:
            try:
                self.lock.acquire()
            except SyncBusyError:
                raise
            state = SyncState(status="running", reason=reason, started_at=utc_now(), lock_started_at=utc_now())
            self.state_store.save_sync(state)
            self.events.append("sync_started", "sincronização iniciada", reason=reason)
            try:
                entries = await self.adapter.fetch_playlist(self.config)
                existing = self.library.all()
                diff = playlist_diff(existing, entries)
                for entry in entries:
                    self.library.upsert_entry(entry)
                for removed in diff.removed:
                    self.library.mark_removed(removed.entry_id)
                pending = list(diff.new) + list(diff.changed)
                for entry in diff.unchanged:
                    current = self.library.get(entry.entry_id)
                    if current and current.status is not TrackStatus.READY:
                        pending.append(entry)
                state.progress_total = len(pending)
                state.counts.removed = len(diff.removed)
                state.counts.skipped = len(diff.unchanged) - sum(1 for item in pending if item in diff.unchanged)
                self.state_store.save_sync(state)
                downloads = await self.adapter.download(self.config, pending) if pending else {}
                for index, entry in enumerate(pending, start=1):
                    state.progress_current = index
                    current = self.library.get(entry.entry_id)
                    if current is None:
                        current = self.library.upsert_entry(entry)
                    try:
                        self.library.mark_status(entry.entry_id, TrackStatus.DOWNLOADING, error=None, attempts=current.attempts + 1)
                        source = downloads.get(entry.entry_id)
                        if source is None or not source.is_file():
                            raise SyncError("spotDL não localizou o arquivo baixado para a faixa")
                        controlled_source = self.paths.downloads / f"{entry.entry_id}{source.suffix.lower()}"
                        controlled_source.parent.mkdir(parents=True, exist_ok=True)
                        if source.resolve() != controlled_source.resolve():
                            temp_source = controlled_source.with_suffix(controlled_source.suffix + ".tmp")
                            shutil.copy2(source, temp_source)
                            os.replace(temp_source, controlled_source)
                        self.library.set_download_path(entry.entry_id, controlled_source)
                        state.counts.downloaded += 1
                        self.library.mark_status(entry.entry_id, TrackStatus.NORMALIZING, error=None)
                        destination_relative = f"tracks/{entry.entry_id}.{self.config.normalization.output_format}"
                        destination = self.paths.library / destination_relative
                        normalized = await self.loudness.process(controlled_source, destination, self.config.normalization)
                        self.library.set_ready(entry.entry_id, relative_path=destination_relative, file_path=destination, normalization=normalized.result)
                        state.counts.normalized += 1
                    except Exception as exc:
                        error = redact_text(str(exc))
                        self.library.mark_status(entry.entry_id, TrackStatus.FAILED, error=error)
                        state.counts.failed += 1
                        self.events.append("track_failed", "faixa falhou na sincronização", entry_id=entry.entry_id, error=error)
                    self.state_store.save_sync(state)
                self.library.write_m3u()
                state.status = "finished" if state.counts.failed == 0 else "finished_with_errors"
                state.finished_at = utc_now()
                state.lock_started_at = None
                self.state_store.save_sync(state)
                self.events.append("sync_finished", "sincronização concluída", status=state.status, counts=state.counts.model_dump(mode="json"))
                return state
            except Exception as exc:
                state.status = "failed"
                state.error = redact_text(str(exc))
                state.finished_at = utc_now()
                state.lock_started_at = None
                self.state_store.save_sync(state)
                self.events.append("sync_failed", "sincronização falhou", error=state.error)
                LOGGER.exception("sincronização falhou")
                return state
            finally:
                self.lock.release()

    async def retry_track(self, entry_id: str) -> SyncState | None:
        track = self.library.retry(entry_id)
        if track.status is TrackStatus.DISCOVERED:
            return await self.run(reason=f"retry:{entry_id}")
        return None

    async def reprocess(self) -> SyncState:
        async with self._async_lock:
            self.lock.acquire()
            state = SyncState(status="running", reason="reprocess", started_at=utc_now(), lock_started_at=utc_now())
            self.state_store.save_sync(state)
            self.events.append("reprocess_started", "reprocessamento da biblioteca iniciado")
            try:
                tracks = [track for track in self.library.all() if track.status is TrackStatus.READY and track.download_relative_path]
                state.progress_total = len(tracks)
                self.state_store.save_sync(state)
                for index, track in enumerate(tracks, start=1):
                    state.progress_current = index
                    try:
                        source = self.library.path_for_download(track)
                        destination_relative = f"tracks/{track.entry_id}.{self.config.normalization.output_format}"
                        destination = self.paths.library / destination_relative
                        self.library.mark_status(track.entry_id, TrackStatus.NORMALIZING, error=None)
                        normalized = await self.loudness.process(source, destination, self.config.normalization)
                        self.library.set_ready(track.entry_id, relative_path=destination_relative, file_path=destination, normalization=normalized.result)
                        state.counts.normalized += 1
                    except Exception as exc:
                        self.library.mark_status(track.entry_id, TrackStatus.FAILED, error=redact_text(str(exc)))
                        state.counts.failed += 1
                    self.state_store.save_sync(state)
                self.library.write_m3u()
                state.status = "finished" if state.counts.failed == 0 else "finished_with_errors"
                state.finished_at = utc_now()
                state.lock_started_at = None
                self.state_store.save_sync(state)
                self.events.append("reprocess_finished", "reprocessamento da biblioteca concluído", status=state.status)
                return state
            except Exception as exc:
                state.status = "failed"
                state.error = redact_text(str(exc))
                state.finished_at = utc_now()
                state.lock_started_at = None
                self.state_store.save_sync(state)
                raise
            finally:
                self.lock.release()

