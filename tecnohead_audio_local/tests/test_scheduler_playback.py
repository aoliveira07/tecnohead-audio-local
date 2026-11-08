from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import make_entry

from app.models import AppConfig
from app.playback import PlaybackSupervisor
from app.scheduler import is_within_window, next_sync_at


def test_window_boundaries_are_explicit():
    zone = ZoneInfo("America/Sao_Paulo")
    assert is_within_window(datetime(2026, 11, 8, 5, 30, tzinfo=zone), "05:30", "21:30")
    assert is_within_window(datetime(2026, 11, 8, 21, 29, tzinfo=zone), "05:30", "21:30")
    assert not is_within_window(datetime(2026, 11, 8, 21, 30, tzinfo=zone), "05:30", "21:30")
    assert next_sync_at(datetime(2026, 11, 8, 15, 1, tzinfo=zone), AppConfig()).strftime("%H:%M") == "03:00"


class FakeLinkPlay:
    def __init__(self):
        self.calls = []

    async def status_all(self):
        return {
            "caixa": {"device_id": "caixa", "name": "Caixa", "host": "192.168.0.13", "online": True, "status": "stop", "position_ms": 0, "duration_ms": 0, "volume_percent": 90},
            "restaurante": {"device_id": "restaurante", "name": "Restaurante", "host": "192.168.0.14", "online": True, "status": "stop", "position_ms": 0, "duration_ms": 0, "volume_percent": 90},
        }

    async def play_group(self, *args, **kwargs):
        self.calls.append(("play_group", args, kwargs))
        return {"confirmed": True}

    async def stop(self, device_id):
        self.calls.append(("stop", device_id))

    async def pause(self, device_id):
        self.calls.append(("pause", device_id))

    async def set_group_volume(self, *args):
        self.calls.append(("volume", args))

    async def status(self, device_id):
        return {"device_id": device_id, "online": True}

    async def identity(self, device_id):
        return {"device_id": device_id}


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_outside_window(paths, config, library, states):
    entry = make_entry()
    library.upsert_entry(entry)
    destination = paths.library / "tracks" / f"{entry.entry_id}.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"audio")
    library.set_ready(entry.entry_id, relative_path=f"tracks/{entry.entry_id}.mp3", file_path=destination, normalization={"mode": "fake", "target_lufs": -18, "true_peak_db": -2, "output_format": "mp3", "completed_at": "now"})
    fake = FakeLinkPlay()
    supervisor = PlaybackSupervisor(config, paths, library, fake, states[0], states[1])
    outside = datetime(2026, 11, 8, 22, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    state = await supervisor.tick(outside)
    assert state.active is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_supervisor_starts_inside_window(paths, config, library, states):
    entry = make_entry()
    library.upsert_entry(entry)
    destination = paths.library / "tracks" / f"{entry.entry_id}.mp3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"audio")
    library.set_ready(entry.entry_id, relative_path=f"tracks/{entry.entry_id}.mp3", file_path=destination, normalization={"mode": "fake", "target_lufs": -18, "true_peak_db": -2, "output_format": "mp3", "completed_at": "now"})
    fake = FakeLinkPlay()
    supervisor = PlaybackSupervisor(config, paths, library, fake, states[0], states[1])
    inside = datetime(2026, 11, 8, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    state = await supervisor.tick(inside)
    assert state.active is True
    assert fake.calls and fake.calls[0][0] == "play_group"

