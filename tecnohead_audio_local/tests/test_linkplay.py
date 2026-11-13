from __future__ import annotations

import asyncio

import pytest

from app.linkplay import HttpResponse, LinkPlayClient, LinkPlayError


class FakeRequester:
    def __init__(self):
        self.commands = []

    async def __call__(self, url, params, timeout, *, verify):
        command = params["command"]
        self.commands.append((url, command))
        if command == "getPlayerStatus":
            return HttpResponse(200, '{"Status":"play","vol":"90","Curpos":"1000","Totlen":"120000"}')
        if command == "getStatus":
            return HttpResponse(200, '{"Firmware":"test-fw","Hardware":"AudioCast","DeviceName":"4361697861"}')
        if command == "multiroom:getSlaveList":
            return HttpResponse(200, '{"slaves":1,"wmrm_version":"4.2","slave_list":[{"ip":"192.168.0.14","volume":90}]}')
        return HttpResponse(200, "OK")


@pytest.mark.asyncio
async def test_linkplay_builds_safe_commands_and_confirms_group(config):
    requester = FakeRequester()
    client = LinkPlayClient(config, requester)
    status = await client.status("caixa")
    assert status["online"] is True
    assert status["volume_percent"] == 90
    group = await client.join_group("caixa", ["restaurante"])
    assert group["confirmed"] is True
    assert any(command.startswith("ConnectMasterAp:JoinGroupMaster:eth192.168.0.13") for _, command in requester.commands)
    await client.play_url("caixa", "http://192.168.0.50:18198/stream/abc?token=redacted-token")
    assert any(command.startswith("setPlayerCmd:play:http://") for _, command in requester.commands)
    await client.play_m3u("caixa", "http://192.168.0.50:18198/playlist.m3u8?token=redacted-token")
    assert any(command.startswith("setPlayerCmd:m3u:play:http://") for _, command in requester.commands)


@pytest.mark.asyncio
async def test_linkplay_timeout_is_not_success(config):
    async def fail(*args, **kwargs):
        raise asyncio.TimeoutError()

    config.linkplay.retry_count = 0
    client = LinkPlayClient(config, fail)
    with pytest.raises(LinkPlayError):
        await client.status("caixa")

