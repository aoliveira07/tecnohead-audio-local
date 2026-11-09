from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

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


class _SpotifyPublicPlaylistParser(HTMLParser):
    """Extract the public track rows rendered in Spotify's playlist page."""

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tracks: list[dict[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._row_depth = 0
        self._artist_active = False
        self._artist_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._row is None:
            if attributes.get("data-testid") == "track-row":
                self._row = {"title": (attributes.get("aria-label") or "").strip(), "source_id": None, "artists": []}
                self._row_depth = 1
            return

        if tag not in self._VOID_TAGS:
            self._row_depth += 1
        href = attributes.get("href") or ""
        if self._row["source_id"] is None and href.startswith("/track/"):
            source_id = href.split("/track/", 1)[1].split("?", 1)[0].split("#", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9]+", source_id):
                self._row["source_id"] = source_id
        if tag == "a" and href.startswith("/artist/"):
            self._artist_active = True
            self._artist_buffer = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if tag == "a" and self._artist_active:
            artist = " ".join("".join(self._artist_buffer).split())
            if artist:
                self._row["artists"].append(artist)
            self._artist_active = False
            self._artist_buffer = []
        if tag not in self._VOID_TAGS:
            self._row_depth -= 1
        if self._row_depth <= 0:
            if self._row["source_id"] and self._row["title"]:
                self.tracks.append(self._row)
            self._row = None
            self._row_depth = 0
            self._artist_active = False
            self._artist_buffer = []

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._artist_active:
            self._artist_buffer.append(data)


def parse_spotify_public_playlist(text: str) -> list[PlaylistEntry]:
    parser = _SpotifyPublicPlaylistParser()
    parser.feed(text)
    entries: list[PlaylistEntry] = []
    seen: set[str] = set()
    for item in parser.tracks:
        source_id = str(item["source_id"])
        if source_id in seen:
            continue
        seen.add(source_id)
        source_url = f"https://open.spotify.com/track/{source_id}"
        entries.append(
            playlist_entry_from_mapping(
                {
                    "song_id": source_id,
                    "name": item["title"],
                    "artists": item["artists"],
                    "url": source_url,
                },
                len(entries),
            )
        )
    if not entries:
        raise SyncError("a página pública do Spotify não contém faixas reconhecíveis")
    return entries


class SpotDLAdapter:
    METADATA_TIMEOUT_SECONDS = 120
    DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
    PUBLIC_PAGE_TIMEOUT_SECONDS = 30
    YOUTUBE_SEARCH_TIMEOUT_SECONDS = 45

    def __init__(self, paths: AppPaths, *, executable: str = "spotdl"):
        self.paths = paths
        self.executable = executable
        self._fallback_song_rows: dict[str, dict[str, Any]] = {}

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

    async def _run(
        self,
        command: list[str],
        config: AppConfig,
        *,
        operation: str,
        timeout_seconds: float,
        tool_name: str = "spotDL",
    ) -> str:
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
        except OSError as exc:
            raise SyncError(f"não foi possível executar spotDL: {redact_text(str(exc))}") from exc
        LOGGER.info("%s %s iniciado (limite=%ss)", tool_name, operation, int(timeout_seconds))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            stdout, stderr = await process.communicate()
            output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
            LOGGER.error("%s %s excedeu o limite de %ss: %s", tool_name, operation, int(timeout_seconds), redact_text(output[-1_500:]))
            raise SyncError(f"{tool_name} excedeu o tempo limite de {int(timeout_seconds)}s na etapa {operation}") from exc
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        if process.returncode != 0:
            LOGGER.error("%s %s falhou com código %s: %s", tool_name, operation, process.returncode, redact_text(output[-1_500:]))
            raise SyncError(f"{tool_name} falhou com código {process.returncode}: {redact_text(output)}")
        LOGGER.info("%s %s concluído", tool_name, operation)
        return output

    @staticmethod
    def _fallback_song_row(entry: PlaylistEntry, youtube_url: str | None, playlist_url: str, playlist_length: int) -> dict[str, Any]:
        return {
            "name": entry.title,
            "artists": [entry.artist] if entry.artist else [],
            "artist": entry.artist,
            "genres": [],
            "disc_number": 1,
            "disc_count": 1,
            "album_name": entry.album,
            "album_artist": entry.artist,
            "duration": int(entry.duration_seconds or 0),
            "year": 0,
            "date": "",
            "track_number": entry.position + 1,
            "tracks_count": playlist_length,
            "song_id": entry.source_id,
            "explicit": False,
            "publisher": "",
            "url": entry.source_url or f"https://open.spotify.com/track/{entry.source_id}",
            "isrc": None,
            "cover_url": None,
            "copyright_text": None,
            "download_url": youtube_url,
            "lyrics": None,
            "popularity": None,
            # The spotDL downloader reinitializes songs with missing album data.
            # A stable local value keeps the already-resolved YouTube URL and
            # avoids a second Spotify API lookup in the public-page fallback.
            "album_id": entry.source_id,
            "list_name": "TecnoHead Áudio Local",
            "list_url": playlist_url,
            "list_position": entry.position,
            "list_length": playlist_length,
            "artist_id": None,
            "album_type": None,
        }

    async def _youtube_url_for_entry(self, entry: PlaylistEntry, config: AppConfig) -> str | None:
        query = " - ".join(part for part in (entry.artist, entry.title) if part).strip()
        if not query:
            return None
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            f"ytsearch1:{query}",
            "--flat-playlist",
            "--print",
            "webpage_url",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout",
            "20",
        ]
        output = await self._run(
            command,
            config,
            operation=f"busca YouTube da faixa {entry.position + 1}",
            timeout_seconds=self.YOUTUBE_SEARCH_TIMEOUT_SECONDS,
            tool_name="yt-dlp",
        )
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if candidate.startswith(("https://www.youtube.com/", "https://youtu.be/")):
                return candidate
        return None

    async def _fetch_public_fallback(self, config: AppConfig, save_file: Path) -> list[PlaylistEntry]:
        try:
            timeout = httpx.Timeout(self.PUBLIC_PAGE_TIMEOUT_SECONDS, connect=10.0)
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "TecnoHead-Audio-Local/0.1"},
            ) as client:
                response = await client.get(config.playlist_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SyncError(f"não foi possível ler a página pública do Spotify: {redact_text(str(exc))}") from exc

        entries = parse_spotify_public_playlist(response.text)
        semaphore = asyncio.Semaphore(max(1, config.sync.max_parallel_tracks))

        async def resolve(entry: PlaylistEntry) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    return entry.entry_id, await self._youtube_url_for_entry(entry, config)
                except SyncError as exc:
                    LOGGER.warning("não foi possível resolver a faixa %s no fallback público: %s", entry.position + 1, redact_text(str(exc)))
                    return entry.entry_id, None

        resolved = dict(await asyncio.gather(*(resolve(entry) for entry in entries)))
        rows = {
            entry.entry_id: self._fallback_song_row(entry, resolved.get(entry.entry_id), config.playlist_url, len(entries))
            for entry in entries
        }
        payload = {
            "type": "sync",
            "query": [
                f"{row['download_url']}|{row['url']}" if row["download_url"] else row["url"]
                for row in rows.values()
            ],
            "songs": list(rows.values()),
        }
        temporary = save_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, save_file)
        self._fallback_song_rows = rows
        LOGGER.info("fallback público do Spotify preparado com %s faixas", len(entries))
        return entries

    async def fetch_playlist(self, config: AppConfig) -> list[PlaylistEntry]:
        if not config.playlist_url:
            raise SyncError("configure a URL da playlist antes de sincronizar")
        self.paths.cache.mkdir(parents=True, exist_ok=True)
        save_file = self.paths.cache / "playlist.spotdl"
        try:
            await self._run(
                self.build_save_command(self.executable, config.playlist_url, save_file),
                config,
                operation="descoberta da playlist",
                timeout_seconds=self.METADATA_TIMEOUT_SECONDS,
            )
            try:
                entries = parse_spotdl_payload(save_file.read_text(encoding="utf-8"))
            except OSError as exc:
                raise SyncError("spotDL não gerou o arquivo de metadados da playlist") from exc
            self._fallback_song_rows = {}
            return entries
        except SyncError as primary_error:
            LOGGER.warning("spotDL não conseguiu descobrir a playlist; usando fallback público: %s", redact_text(str(primary_error)))
            try:
                return await self._fetch_public_fallback(config, save_file)
            except SyncError as fallback_error:
                raise SyncError(f"spotDL falhou e o fallback público também falhou: {redact_text(str(fallback_error))}") from primary_error

    @staticmethod
    def _source_tokens(entry: PlaylistEntry) -> set[str]:
        source = entry.source_id.replace(":", "/")
        return {part.casefold() for part in source.split("/") if len(part) >= 4}

    async def download(self, config: AppConfig, entries: list[PlaylistEntry]) -> dict[str, Path]:
        self.paths.downloads.mkdir(parents=True, exist_ok=True)
        if entries and self._fallback_song_rows and all(entry.entry_id in self._fallback_song_rows for entry in entries):
            return await self._download_fallback(config, entries)
        save_file = self.paths.cache / "playlist.spotdl"
        await self._run(
            self.build_sync_command(self.executable, save_file, self.paths.downloads, config.normalization.output_format),
            config,
            operation="download da playlist",
            timeout_seconds=self.DOWNLOAD_TIMEOUT_SECONDS,
        )
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

    async def _download_fallback(self, config: AppConfig, entries: list[PlaylistEntry]) -> dict[str, Path]:
        input_file = self.paths.tmp / "spotdl-fallback-input.json"
        result_file = self.paths.tmp / "spotdl-fallback-result.json"
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text(
            json.dumps({"songs": [self._fallback_song_rows[entry.entry_id] for entry in entries]}, ensure_ascii=False),
            encoding="utf-8",
        )
        result_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(Path(__file__).with_name("spotdl_bridge.py")),
            "--input",
            str(input_file),
            "--output-dir",
            str(self.paths.downloads),
            "--output-format",
            config.normalization.output_format,
            "--bitrate",
            config.normalization.bitrate,
            "--result",
            str(result_file),
        ]
        try:
            await self._run(
                command,
                config,
                operation="download da playlist pelo fallback",
                timeout_seconds=self.DOWNLOAD_TIMEOUT_SECONDS,
                tool_name="spotDL",
            )
            result_payload = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError("spotDL não retornou o resultado do fallback") from exc
        finally:
            input_file.unlink(missing_ok=True)
            result_file.unlink(missing_ok=True)
        result: dict[str, Path] = {}
        for item in result_payload.get("results", []):
            if item.get("path") and item.get("song_id"):
                result[str(item["song_id"])] = Path(str(item["path"]))
        return {entry.entry_id: result[entry.source_id] for entry in entries if entry.source_id in result}


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

