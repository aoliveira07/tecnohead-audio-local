from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from app.models import AppConfig, DeviceConfig
from app.settings import redact_text

LOGGER = logging.getLogger("tecnohead_audio_local.linkplay")


class LinkPlayError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    text: str


class Requester(Protocol):
    async def __call__(self, url: str, params: dict[str, str], timeout: float, *, verify: bool) -> HttpResponse: ...


async def default_requester(url: str, params: dict[str, str], timeout: float, *, verify: bool) -> HttpResponse:
    async with httpx.AsyncClient(verify=verify, follow_redirects=False) as client:
        response = await client.get(url, params=params, timeout=timeout)
        return HttpResponse(status_code=response.status_code, text=response.text)


def _as_int(value: object, default: int | None = None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _decode_hex_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        if len(value) % 2 == 0 and value and all(char in "0123456789abcdefABCDEF" for char in value):
            decoded = bytes.fromhex(value).decode("utf-8", errors="replace")
            if decoded and any(char.isalnum() for char in decoded):
                return decoded
    except ValueError:
        pass
    return value


class LinkPlayClient:
    """Small, explicit LinkPlay HTTP client.

    The commands follow the public LinkPlay HTTP API shape used by common
    AudioCast/WiiM/Arylic firmware. Firmware-specific group semantics remain
    visible in diagnostics and are never reported as confirmed without a
    successful status read-back.
    """

    def __init__(self, config: AppConfig, requester: Requester | None = None):
        self.config = config
        self.requester = requester or default_requester

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def _device(self, device_id: str) -> DeviceConfig:
        for device in self.config.devices:
            if device.device_id == device_id and device.enabled:
                return device
        raise LinkPlayError(f"dispositivo não configurado: {device_id}")

    def _base_url(self, device: DeviceConfig) -> str:
        scheme = "https" if device.https else "http"
        return f"{scheme}://{device.host}:{device.port}"

    async def _send(self, device: DeviceConfig, command: str) -> Any:
        url = urljoin(self._base_url(device) + "/", self.config.linkplay.endpoint_path.lstrip("/"))
        attempts = self.config.linkplay.retry_count + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.requester(url, {"command": command}, device.timeout_seconds, verify=not device.https)
                if response.status_code < 200 or response.status_code >= 300:
                    raise LinkPlayError(f"HTTP {response.status_code} em {device.name}")
                body = response.text.strip()
                if not body:
                    raise LinkPlayError(f"resposta vazia de {device.name}")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = body
                if isinstance(payload, str) and payload.casefold() in {"error", "fail", "unknown command", "invalid"}:
                    raise LinkPlayError(f"AudioCast rejeitou o comando em {device.name}")
                return payload
            except (httpx.HTTPError, asyncio.TimeoutError, LinkPlayError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.config.linkplay.retry_delay_seconds)
        message = str(last_error or "falha desconhecida")
        raise LinkPlayError(f"AudioCast {device.name} indisponível: {redact_text(message)}") from last_error

    async def status(self, device_id: str) -> dict[str, Any]:
        device = self._device(device_id)
        payload = await self._send(device, "getPlayerStatus")
        if not isinstance(payload, dict):
            raise LinkPlayError(f"status inválido de {device.name}")
        return {
            "device_id": device.device_id,
            "name": device.name,
            "host": device.host,
            "online": True,
            "status": str(payload.get("Status", payload.get("status", "unknown"))),
            "title": _decode_hex_text(payload.get("Title", payload.get("title", ""))),
            "artist": _decode_hex_text(payload.get("Artist", payload.get("artist", ""))),
            "album": _decode_hex_text(payload.get("Album", payload.get("album", ""))),
            "position_ms": _as_int(payload.get("Curpos", payload.get("curpos")), 0),
            "duration_ms": _as_int(payload.get("Totlen", payload.get("totlen")), 0),
            "volume_percent": _as_int(payload.get("vol", payload.get("Vol", payload.get("Flight")))),
            "loop_mode": _as_int(payload.get("Loop", payload.get("loop"))),
            "mute": str(payload.get("Mute", payload.get("mute", "0"))) in {"1", "true", "True"},
            "mode": _as_int(payload.get("Mode", payload.get("mode"))),
            "raw": {key: value for key, value in payload.items() if str(key).casefold() not in {"token", "password", "cookie"}},
        }

    async def status_all(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for device in self.config.devices:
            if not device.enabled:
                results[device.device_id] = {"device_id": device.device_id, "name": device.name, "online": False, "error": "desabilitado"}
                continue
            try:
                results[device.device_id] = await self.status(device.device_id)
            except LinkPlayError as exc:
                results[device.device_id] = {
                    "device_id": device.device_id,
                    "name": device.name,
                    "host": device.host,
                    "online": False,
                    "error": redact_text(str(exc)),
                }
        return results

    async def identity(self, device_id: str) -> dict[str, Any]:
        device = self._device(device_id)
        payload = await self._send(device, "getStatus")
        if not isinstance(payload, dict):
            raise LinkPlayError(f"identidade inválida de {device.name}")
        def value(*keys: str) -> object:
            for key in keys:
                if key in payload:
                    return payload[key]
            return ""

        return {
            "device_id": device.device_id,
            "name": device.name,
            "host": device.host,
            "firmware": str(value("Firmware", "firmware")),
            "hardware": str(value("Hardware", "hardware")),
            "device_name": _decode_hex_text(value("DeviceName", "device_name")),
            "uuid": str(value("Uuid", "uuid"))[:64],
            "group": _as_int(value("Group", "group")),
            "master_uuid": str(value("MasterUuid", "master_uuid", "masterUuid"))[:64],
            "wmrm_version": str(value("wmrm_version", "WmrmVersion", "wmrmVersion")),
            "network": {
                "eth2": str(value("eth2", "Eth2")),
                "apcli0": str(value("apcli0", "Apcli0")),
            },
            "raw_keys": sorted(str(key) for key in payload),
        }

    async def command(self, device_id: str, command: str) -> Any:
        # Commands are built only by typed methods below. This method remains
        # private in the API layer and never accepts arbitrary browser input.
        return await self._send(self._device(device_id), command)

    async def set_volume(self, device_id: str, volume_percent: int) -> Any:
        if not 0 <= volume_percent <= 100:
            raise ValueError("volume deve estar entre 0 e 100")
        return await self.command(device_id, f"setPlayerCmd:vol:{volume_percent}")

    async def set_loop(self, device_id: str, enabled: bool, shuffle: bool = False) -> Any:
        # LinkPlay combines shuffle and repeat in one mode value:
        # 0 = repeat all, 1 = repeat one, 2 = repeat all + shuffle,
        # 3 = shuffle without repeat, 4 = sequence without repeat.
        # In particular, mode 1 must not be used for a playlist: it repeats
        # the current track indefinitely on AudioCast firmware.
        if shuffle:
            mode = 2 if enabled else 3
        else:
            mode = 0 if enabled else 4
        return await self.command(device_id, f"setPlayerCmd:loopmode:{mode}")

    async def play_url(self, device_id: str, url: str) -> Any:
        if not url.startswith(("http://", "https://")) or any(char in url for char in "\r\n"):
            raise ValueError("URL de mídia inválida")
        return await self.command(device_id, f"setPlayerCmd:play:{url}")

    async def play_m3u(self, device_id: str, url: str) -> Any:
        if not url.startswith(("http://", "https://")) or any(char in url for char in "\r\n"):
            raise ValueError("URL de playlist inválida")
        return await self.command(device_id, f"setPlayerCmd:m3u:play:{url}")

    async def pause(self, device_id: str) -> Any:
        return await self.command(device_id, "setPlayerCmd:pause")

    async def resume(self, device_id: str) -> Any:
        return await self.command(device_id, "setPlayerCmd:resume")

    async def stop(self, device_id: str) -> Any:
        return await self.command(device_id, "setPlayerCmd:stop")

    async def next(self, device_id: str) -> Any:
        return await self.command(device_id, "setPlayerCmd:next")

    async def previous(self, device_id: str) -> Any:
        return await self.command(device_id, "setPlayerCmd:prev")

    async def get_slave_list(self, master_id: str) -> dict[str, Any]:
        payload = await self.command(master_id, "multiroom:getSlaveList")
        if not isinstance(payload, dict):
            raise LinkPlayError("lista de grupo inválida")
        entries = payload.get("slave_list", payload.get("Slave_list", []))
        if not isinstance(entries, list):
            entries = []
        return {
            "slaves": _as_int(payload.get("slaves", payload.get("Slaves")), len(entries)) or 0,
            "wmrm_version": str(payload.get("wmrm_version", payload.get("Version", ""))),
            "slave_list": [
                {
                    "name": str(item.get("name", item.get("Name", ""))),
                    "ip": str(item.get("ip", item.get("Ip", ""))),
                    "volume": _as_int(item.get("volume", item.get("Volume"))),
                    "mute": _as_int(item.get("mute", item.get("Mute"))),
                    "version": str(item.get("version", item.get("Version", ""))),
                }
                for item in entries
                if isinstance(item, dict)
            ],
        }

    async def check_group(self, master_id: str, member_ids: list[str]) -> dict[str, Any]:
        master = self._device(master_id)
        expected = {self._device(item).host for item in member_ids}
        result = await self.get_slave_list(master_id)
        actual = {str(item.get("ip")) for item in result["slave_list"]}
        missing = sorted(expected - actual)
        return {"confirmed": not missing, "master_id": master.device_id, "expected_hosts": sorted(expected), "member_hosts": sorted(actual), "missing_hosts": missing, "raw": result}

    async def join_group(self, master_id: str, member_ids: list[str]) -> dict[str, Any]:
        master = self._device(master_id)
        commands = await self._send_join_commands(master, member_ids, attempt="initial")
        if self.config.linkplay.verify_group_after_join:
            await self._wait_for_group_settle()
            verified = await self.check_group(master_id, member_ids)
        else:
            verified = {"confirmed": False, "reason": "read-back desabilitado"}

        recovery: list[dict[str, Any]] = []
        if not verified.get("confirmed") and self.config.linkplay.verify_group_after_join:
            missing_hosts = set(verified.get("missing_hosts") or [])
            missing_ids = [member_id for member_id in member_ids if self._device(member_id).host in missing_hosts]
            # Some AudioCast firmware keeps a standalone/Line-In state after a
            # previous grouping attempt. Reset only members that the master
            # says are missing, then retry the documented join command.
            for member_id in missing_ids:
                for command in ("multiroom:Ungroup", "setMultiroomLogic:1"):
                    try:
                        await self.command(member_id, command)
                        recovery.append({"member_id": member_id, "command": command, "accepted": True})
                    except LinkPlayError as exc:
                        recovery.append({"member_id": member_id, "command": command, "accepted": False, "error": redact_text(str(exc))})
            if missing_ids:
                await self._wait_for_group_settle()
                commands.extend(await self._send_join_commands(master, missing_ids, attempt="recovery"))
                await self._wait_for_group_settle()
                verified = await self.check_group(master_id, member_ids)
        if not verified.get("confirmed"):
            missing = ", ".join(verified.get("missing_hosts") or []) or "membro desconhecido"
            raise LinkPlayError(f"grupo não confirmado pelo status do mestre; ausentes: {missing}")
        return {"ok": True, "confirmed": True, "commands": commands, "recovery": recovery, "group": verified}

    async def _send_join_commands(self, master: DeviceConfig, member_ids: list[str], *, attempt: str) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for member_id in member_ids:
            member = self._device(member_id)
            command = self.config.linkplay.join_command_template.format(master_host=master.host, member_host=member.host)
            await self.command(member.device_id, command)
            commands.append({"member_id": member.device_id, "attempt": attempt, "command": command.replace(master.host, "[host]").replace(member.host, "[host]")})
        return commands

    async def _wait_for_group_settle(self) -> None:
        delay = max(0.1, min(1.0, self.config.linkplay.retry_delay_seconds))
        await asyncio.sleep(delay)

    async def ungroup(self, master_id: str) -> Any:
        return await self.command(master_id, "multiroom:Ungroup")

    async def set_group_volume(self, master_id: str, member_ids: list[str], volume_percent: int) -> dict[str, Any]:
        device_ids = [master_id, *member_ids]
        targets = {
            device_id: min(volume_percent, self._device(device_id).volume_percent)
            for device_id in device_ids
        }

        # LinkPlay has two different volume controls in a multiroom zone:
        # slave_vol controls the host, while SlaveVolume targets a guest by
        # IP. Sending setPlayerCmd:vol directly to a guest is not reliable on
        # firmware that exposes the guest only through the host.
        results: dict[str, Any] = {
            master_id: await self.command(master_id, f"setPlayerCmd:slave_vol:{targets[master_id]}")
        }
        for member_id in member_ids:
            member = self._device(member_id)
            results[member_id] = await self.command(
                master_id,
                f"multiroom:SlaveVolume:{member.host}:{targets[member_id]}",
            )
        return {
            "ok": True,
            "requested_volume_percent": volume_percent,
            "volume_percent": targets[master_id],
            "device_volumes": targets,
            "devices": results,
        }

    async def set_individual_volumes(self, device_ids: list[str], volume_percent: int) -> dict[str, Any]:
        """Apply each zone's effective volume without using WMRM guests."""

        targets = {
            device_id: min(volume_percent, self._device(device_id).volume_percent)
            for device_id in device_ids
        }
        results: dict[str, Any] = {}
        for device_id, target in targets.items():
            results[device_id] = await self.set_volume(device_id, target)
        return {
            "ok": True,
            "requested_volume_percent": volume_percent,
            "device_volumes": targets,
            "devices": results,
        }

    async def play_individual(
        self,
        device_ids: list[str],
        url: str,
        *,
        loop: bool,
        shuffle: bool,
        volume_percent: int,
        playlist_url: str | None = None,
    ) -> dict[str, Any]:
        """Start the same source on each AudioCast as a standalone player.

        The master is explicitly ungrouped first so a previous WMRM session
        cannot silently redirect commands sent to the individual zones.
        """

        if not device_ids:
            raise LinkPlayError("nenhum AudioCast habilitado para reprodução individual")
        await self.ungroup(device_ids[0])
        results: dict[str, Any] = {}
        targets = {
            device_id: min(volume_percent, self._device(device_id).volume_percent)
            for device_id in device_ids
        }
        for device_id in device_ids:
            source_result = await (self.play_m3u(device_id, playlist_url) if playlist_url else self.play_url(device_id, url))
            loop_result = await self.set_loop(device_id, loop, shuffle)
            volume_result = await self.set_volume(device_id, targets[device_id])
            status_result = await self.status(device_id)
            results[device_id] = {
                "source": source_result,
                "loop": loop_result,
                "volume": volume_result,
                "status": status_result,
            }
        return {
            "ok": True,
            "mode": "individual",
            "confirmed": True,
            "device_volumes": targets,
            "devices": results,
        }

    async def play_group(self, master_id: str, member_ids: list[str], url: str, *, loop: bool, shuffle: bool, volume_percent: int, playlist_url: str | None = None) -> dict[str, Any]:
        group = await self.check_group(master_id, member_ids)
        if not group.get("confirmed"):
            group = await self.join_group(master_id, member_ids)
        command_result = await (self.play_m3u(master_id, playlist_url) if playlist_url else self.play_url(master_id, url))
        # Some AudioCast firmware resets the repeat mode when a new M3U is
        # loaded. Apply it after the source is selected so playlist playback
        # cannot fall back to repeat-one (mode 1).
        await self.set_loop(master_id, loop, shuffle)
        # Loading a new source can restore the device's persisted volume. Set
        # the per-zone levels after the source and loop mode are selected so
        # the next track starts with the configured levels as well.
        volume_result = await self.set_group_volume(master_id, member_ids, volume_percent)
        return {"ok": True, "confirmed": True, "group": group, "command": command_result, "volume": volume_result}

