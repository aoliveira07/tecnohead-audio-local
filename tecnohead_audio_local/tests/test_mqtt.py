from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models import AppConfig
from app.mqtt import MqttService
from app.settings import EventLog


class FakeMqttClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def username_pw_set(self, username, password):
        self.username = username
        self.password = password

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, host, port, keepalive):
        self.connection = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, retain))


@pytest.mark.asyncio
async def test_mqtt_discovery_and_sync_button(paths):
    client = FakeMqttClient()
    requested = []
    service = MqttService(AppConfig(), EventLog(paths), lambda: requested.append(True), client_factory=lambda **kwargs: client)
    service.start(asyncio.get_running_loop())
    client.on_connect(client, None, {}, 0, None)

    assert client.subscriptions == ["tecnohead/audio_local/sync/set"]
    assert any(topic.endswith("/button/tecnohead_audio_local_" + service.config.installation_id + "/sync/config") for topic, _payload, _retain in client.published)
    assert ("tecnohead/audio_local/availability", "online", True) in client.published

    client.on_message(client, None, SimpleNamespace(topic=service.command_topic, payload=b"SYNC"))
    await asyncio.sleep(0)
    assert requested == [True]

    service.publish_sync_status("running")
    assert (service.state_topic, "running", True) in client.published
    service.stop()
