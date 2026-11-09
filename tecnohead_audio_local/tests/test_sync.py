from __future__ import annotations

import pytest
from conftest import FakeAdapter, FakeLoudness, make_entry

from app.library import LibraryManager
from app.models import TrackStatus
from app.sync_service import PersistentSyncLock, SpotDLAdapter, SyncBusyError, SyncError, SyncService, parse_spotdl_payload


def test_parse_spotdl_metadata():
    entries = parse_spotdl_payload('{"songs":[{"song_id":"abc123","name":"Título","artists":[{"name":"Artista"}],"album":{"name":"Álbum"},"duration":123}]}')
    assert entries[0].source_id == "abc123"
    assert entries[0].artist == "Artista"


def test_persistent_lock_rejects_concurrent(paths):
    first = PersistentSyncLock(paths.lock_file)
    second = PersistentSyncLock(paths.lock_file)
    first.acquire()
    with pytest.raises(SyncBusyError):
        second.acquire()
    first.release()


@pytest.mark.asyncio
async def test_sync_continues_after_track_failure(paths, config, states):
    entries = [make_entry(0, title="Boa"), make_entry(1, title="Falha")]
    library = LibraryManager(paths)
    library.load()
    adapter = FakeAdapter(paths, entries)
    service = SyncService(config, paths, library, FakeLoudness(fail_title="Falha"), states[0], states[1], adapter)
    # Make the fake failure inspect the copied source bytes.
    state = await service.run("test")
    assert state.status == "finished_with_errors"
    assert state.counts.normalized == 1
    assert state.counts.failed == 1
    assert sum(item.status is TrackStatus.READY for item in library.all()) == 1


@pytest.mark.asyncio
async def test_sync_failure_is_persisted_without_leaving_running_state(paths, config, states):
    library = LibraryManager(paths)
    library.load()
    service = SyncService(config, paths, library, FakeLoudness(), states[0], states[1], SpotDLAdapter(paths))

    state = await service.run("test")

    assert state.status == "failed"
    assert state.error == "configure a URL da playlist antes de sincronizar"
    assert states[0].load_sync().status == "failed"
    assert states[0].load_sync().finished_at is not None
    assert any(item["event"] == "sync_failed" for item in states[1].list())


def test_sync_start_rejects_missing_playlist_before_creating_task(paths, config, states):
    library = LibraryManager(paths)
    library.load()
    service = SyncService(config, paths, library, FakeLoudness(), states[0], states[1], SpotDLAdapter(paths))

    with pytest.raises(SyncError, match="configure a URL"):
        service.start()

