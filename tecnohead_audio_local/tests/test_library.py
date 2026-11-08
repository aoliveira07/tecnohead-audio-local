from __future__ import annotations

import pytest
from conftest import make_entry

from app.library import LibraryPathError, playlist_diff, safe_join, safe_relative_path, stable_library_relative_path
from app.models import TrackStatus


def test_safe_paths_reject_traversal(paths):
    assert safe_relative_path("tracks/a.mp3") == "tracks/a.mp3"
    with pytest.raises(LibraryPathError):
        safe_relative_path("../outside.mp3")
    with pytest.raises(LibraryPathError):
        safe_relative_path("C:\\outside.mp3")
    with pytest.raises(LibraryPathError):
        safe_join(paths.library, "tracks/../../secret")
    assert stable_library_relative_path("a" * 64, "mp3").startswith("tracks/")


def test_playlist_diff_detects_new_changed_removed_and_duplicates(library):
    first = make_entry(0, title="Uma")
    second = make_entry(1, title="Duas")
    library.upsert_entry(first)
    library.upsert_entry(second)
    changed = make_entry(1, title="Duas — nova versão")
    incoming = [first, changed, changed]
    diff = playlist_diff(library.all(), incoming)
    assert [item.title for item in diff.unchanged] == ["Uma"]
    assert [item.title for item in diff.changed] == ["Duas — nova versão"]
    assert len(diff.duplicates) == 1

    removed = make_entry(2, title="Três")
    library.upsert_entry(removed)
    diff = playlist_diff(library.all(), [first, changed])
    assert [item.title for item in diff.removed] == ["Três"]


def test_ready_file_is_persisted_and_removed_is_soft(library, paths):
    entry = make_entry()
    library.upsert_entry(entry)
    destination = paths.library / "tracks" / f"{entry.entry_id}.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"audio")
    library.set_ready(entry.entry_id, relative_path=f"tracks/{entry.entry_id}.mp3", file_path=destination, normalization={"mode": "fake", "target_lufs": -18, "true_peak_db": -2, "output_format": "mp3", "completed_at": "now"})
    assert library.get(entry.entry_id).status is TrackStatus.READY
    library.mark_removed(entry.entry_id)
    assert destination.exists()
    assert library.get(entry.entry_id).status is TrackStatus.REMOVED

