from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable

from pydantic import ValidationError

from app.models import NormalizationResult, PlaylistEntry, Track, TrackStatus
from app.settings import AppPaths, JsonFile, utc_now


class LibraryPathError(ValueError):
    pass


def safe_relative_path(value: str) -> str:
    """Return a normalized relative path or reject traversal/absolute input."""

    if not isinstance(value, str) or not value.strip():
        raise LibraryPathError("caminho vazio")
    candidate = value.replace("\\", "/").strip()
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate) or "\x00" in candidate:
        raise LibraryPathError("caminho absoluto não permitido")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise LibraryPathError("traversal não permitido")
    if not parts:
        raise LibraryPathError("caminho vazio")
    return "/".join(parts)


def safe_join(root: Path, relative: str) -> Path:
    safe = safe_relative_path(relative)
    root = root.resolve()
    target = (root / Path(*safe.split("/"))).resolve()
    if target != root and root not in target.parents:
        raise LibraryPathError("caminho fora do diretório gerenciado")
    return target


def stable_library_relative_path(entry_id: str, output_format: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16,64}", entry_id):
        raise LibraryPathError("identificador de faixa inválido")
    return f"tracks/{entry_id}.{output_format}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_fingerprint(entry: PlaylistEntry) -> str:
    fields = ("entry_id", "source_id", "title", "artist", "album", "duration_seconds", "position", "source_url")
    raw = json.dumps({field: getattr(entry, field) for field in fields}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlaylistDiff:
    new: list[PlaylistEntry]
    changed: list[PlaylistEntry]
    unchanged: list[PlaylistEntry]
    removed: list[Track]
    duplicates: list[PlaylistEntry]


def playlist_diff(existing: Iterable[Track], incoming: Iterable[PlaylistEntry]) -> PlaylistDiff:
    existing_by_id = {item.entry_id: item for item in existing}
    seen: set[str] = set()
    new: list[PlaylistEntry] = []
    changed: list[PlaylistEntry] = []
    unchanged: list[PlaylistEntry] = []
    duplicates: list[PlaylistEntry] = []
    for entry in sorted(incoming, key=lambda item: item.position):
        if entry.entry_id in seen:
            duplicates.append(entry)
            continue
        seen.add(entry.entry_id)
        current = existing_by_id.get(entry.entry_id)
        if current is None:
            new.append(entry)
        elif entry_fingerprint(current) == entry_fingerprint(entry):
            unchanged.append(entry)
        else:
            changed.append(entry)
    removed = [item for item in existing_by_id.values() if item.entry_id not in seen and item.status is not TrackStatus.REMOVED]
    return PlaylistDiff(new=new, changed=changed, unchanged=unchanged, removed=sorted(removed, key=lambda item: item.position), duplicates=duplicates)


class LibraryManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.file = JsonFile(paths.library_index_file, lambda: {"schema_version": 1, "tracks": []})
        self._lock = RLock()
        self._tracks: dict[str, Track] = {}

    def load(self) -> list[Track]:
        with self._lock:
            self.paths.ensure()
            try:
                raw = self.file.read_raw()
                values = raw.get("tracks", []) if isinstance(raw, dict) else []
                self._tracks = {track.entry_id: track for track in (Track.model_validate(item) for item in values)}
            except (OSError, ValueError, TypeError, ValidationError):
                self._tracks = {}
            return self.all()

    def all(self) -> list[Track]:
        with self._lock:
            return [track.model_copy(deep=True) for track in sorted(self._tracks.values(), key=lambda item: item.position)]

    def get(self, entry_id: str) -> Track | None:
        with self._lock:
            item = self._tracks.get(entry_id)
            return item.model_copy(deep=True) if item else None

    def _save_locked(self) -> None:
        payload = {"schema_version": 1, "updated_at": utc_now(), "tracks": [item.model_dump(mode="json") for item in self.all()]}
        self.file.write_raw(payload, mode=0o600)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def upsert_entry(self, entry: PlaylistEntry) -> Track:
        with self._lock:
            current = self._tracks.get(entry.entry_id)
            if current is None:
                track = Track(**entry.model_dump(), status=TrackStatus.DISCOVERED)
            else:
                changed = entry_fingerprint(current) != entry_fingerprint(entry)
                track = current.model_copy(update=entry.model_dump())
                if changed:
                    track.status = TrackStatus.DISCOVERED
                    track.normalization = None
                    track.error = None
                    track.attempts = 0
            self._tracks[entry.entry_id] = track
            self._save_locked()
            return track.model_copy(deep=True)

    def mark_status(self, entry_id: str, status: TrackStatus, *, error: str | None = None, **updates: object) -> Track:
        with self._lock:
            track = self._tracks[entry_id]
            values = {"status": status, "error": error, "last_synced_at": utc_now(), **updates}
            self._tracks[entry_id] = track.model_copy(update=values)
            self._save_locked()
            return self._tracks[entry_id].model_copy(deep=True)

    def mark_removed(self, entry_id: str) -> Track:
        return self.mark_status(entry_id, TrackStatus.REMOVED, error="item removido da playlist; arquivo preservado")

    def retry(self, entry_id: str) -> Track:
        with self._lock:
            track = self._tracks[entry_id]
            updated = track.model_copy(update={"status": TrackStatus.DISCOVERED, "error": None})
            self._tracks[entry_id] = updated
            self._save_locked()
            return updated.model_copy(deep=True)

    def path_for_track(self, track: Track) -> Path:
        if not track.relative_path:
            raise LibraryPathError("faixa sem caminho normalizado")
        return safe_join(self.paths.library, track.relative_path)

    def path_for_download(self, track: Track) -> Path:
        if not track.download_relative_path:
            raise LibraryPathError("faixa sem caminho de download")
        return safe_join(self.paths.data, track.download_relative_path)

    def set_download_path(self, entry_id: str, path: Path) -> Track:
        relative = path.resolve().relative_to(self.paths.data.resolve()).as_posix()
        return self.mark_status(entry_id, TrackStatus.DOWNLOADED, download_relative_path=safe_relative_path(relative))

    def set_ready(self, entry_id: str, *, relative_path: str, file_path: Path, normalization: object) -> Track:
        safe = safe_relative_path(relative_path)
        resolved = safe_join(self.paths.library, safe)
        if resolved != file_path.resolve():
            raise LibraryPathError("arquivo final não está dentro da biblioteca")
        normalized = normalization if isinstance(normalization, NormalizationResult) else NormalizationResult.model_validate(normalization)
        return self.mark_status(
            entry_id,
            TrackStatus.READY,
            error=None,
            relative_path=safe,
            file_sha256=sha256_file(resolved),
            file_size=resolved.stat().st_size,
            normalization=normalized,
        )

    def ready_tracks(self, *, shuffle: bool = False) -> list[Track]:
        import random

        items = [track for track in self.all() if track.status is TrackStatus.READY and track.relative_path]
        if shuffle:
            random.Random(0).shuffle(items)
        return items

    def summary(self) -> dict[str, int]:
        result = {status.value: 0 for status in TrackStatus}
        for track in self.all():
            result[track.status.value] += 1
        result["total"] = len(self._tracks)
        result["ready_bytes"] = sum(track.file_size or 0 for track in self._tracks.values() if track.status is TrackStatus.READY)
        return result

    def write_m3u(self) -> None:
        with self._lock:
            self.paths.library.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U"]
            for track in self.ready_tracks():
                if track.relative_path:
                    lines.append(f"#EXTINF:{int(track.duration_seconds or -1)},{track.artist} - {track.title}")
                    lines.append(track.relative_path)
            fd, temp_name = tempfile.mkstemp(prefix="playlist.", suffix=".tmp", dir=self.paths.library)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.paths.playlist_file)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

