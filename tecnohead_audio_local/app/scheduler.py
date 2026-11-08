from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import AppConfig, parse_hhmm
from app.settings import EventLog, utc_now
from app.sync_service import SyncBusyError, SyncService


def is_within_window(moment: datetime, start: str, end: str) -> bool:
    current = moment.hour * 60 + moment.minute
    start_minutes = parse_hhmm(start).hour * 60 + parse_hhmm(start).minute
    end_minutes = parse_hhmm(end).hour * 60 + parse_hhmm(end).minute
    if start_minutes < end_minutes:
        return start_minutes <= current < end_minutes
    if start_minutes > end_minutes:
        return current >= start_minutes or current < end_minutes
    return True


def next_sync_at(moment: datetime, config: AppConfig) -> datetime | None:
    zone = ZoneInfo(config.timezone)
    localized = moment.astimezone(zone)
    candidates: list[datetime] = []
    for day_offset in range(0, 3):
        day = (localized + timedelta(days=day_offset)).date()
        for value in config.sync.times:
            parsed = parse_hhmm(value)
            candidate = datetime.combine(day, parsed, tzinfo=zone)
            if candidate > localized:
                candidates.append(candidate)
    return min(candidates) if candidates else None


class SyncScheduler:
    def __init__(self, config: AppConfig, sync_service: SyncService, events: EventLog):
        self.config = config
        self.sync_service = sync_service
        self.events = events
        self.stop_event = asyncio.Event()
        self.last_trigger_key: str | None = None

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def trigger_key(self, moment: datetime) -> str:
        localized = moment.astimezone(ZoneInfo(self.config.timezone))
        return f"{localized.date().isoformat()}-{localized.strftime('%H:%M')}"

    async def tick(self, moment: datetime | None = None) -> bool:
        now = moment or datetime.now(ZoneInfo(self.config.timezone))
        localized = now.astimezone(ZoneInfo(self.config.timezone))
        key = self.trigger_key(localized)
        if not self.config.sync.enabled or localized.strftime("%H:%M") not in self.config.sync.times or key == self.last_trigger_key:
            return False
        self.last_trigger_key = key
        try:
            task = self.sync_service.start(reason="scheduled")
            task.add_done_callback(self._consume_task)
            self.events.append("sync_scheduled", "sincronização agendada disparada", at=utc_now())
            return True
        except SyncBusyError:
            self.events.append("sync_schedule_skipped", "sincronização agendada ignorada porque outra está ativa")
            return False

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except Exception:
            # The SyncService persists and logs the failure. Avoid an unhandled
            # task warning from the scheduler loop.
            return

    async def run(self) -> None:
        while not self.stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self.stop_event.set()

