from __future__ import annotations

from pathlib import Path

import pytest

from app.library import LibraryManager
from app.models import AppConfig, NormalizationResult, PlaylistEntry, entry_id_for
from app.settings import AppPaths, EventLog, StateStore


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_MEDIA_DIR", str(tmp_path / "media"))
    result = AppPaths.from_environment()
    result.ensure()
    return result


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(media={"base_url": "http://192.168.0.50:18198", "port": 18198})


@pytest.fixture
def library(paths: AppPaths) -> LibraryManager:
    result = LibraryManager(paths)
    result.load()
    return result


def make_entry(position: int = 0, *, title: str = "Faixa") -> PlaylistEntry:
    return PlaylistEntry(
        entry_id=entry_id_for(source_id=f"spotify:{position}", title=title, artist="Artista", duration_seconds=120),
        source_id=f"spotify:{position}",
        title=title,
        artist="Artista",
        album="Álbum",
        duration_seconds=120,
        position=position,
        source_url=f"https://open.spotify.com/track/{position:08d}",
    )


class FakeLoudness:
    def __init__(self, fail_title: str | None = None):
        self.fail_title = fail_title

    async def process(self, source, destination, config):
        import shutil

        from app.loudness import NormalizationOutput, ProbeInfo

        if self.fail_title and self.fail_title.encode() in source.read_bytes():
            raise RuntimeError("falha controlada")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        result = NormalizationResult(mode="fake", target_lufs=-18, true_peak_db=-2, duration_seconds=120, output_format=config.output_format, completed_at="2026-11-08T00:00:00+00:00")
        return NormalizationOutput(result=result, probe=ProbeInfo(duration_seconds=120, codec="mp3", audio_streams=1, format_name="mp3"))


class FakeAdapter:
    def __init__(self, paths: AppPaths, entries: list[PlaylistEntry]):
        self.paths = paths
        self.entries = entries
        self.executable = "spotdl"

    async def fetch_playlist(self, config):
        return self.entries

    async def download(self, config, entries):
        result = {}
        for entry in entries:
            source = self.paths.downloads / f"source-{entry.entry_id}.mp3"
            source.write_bytes(f"audio:{entry.title}".encode())
            result[entry.entry_id] = source
        return result


@pytest.fixture
def states(paths: AppPaths) -> tuple[StateStore, EventLog]:
    return StateStore(paths), EventLog(paths)

