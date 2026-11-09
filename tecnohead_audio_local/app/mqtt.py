from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - the production image installs paho-mqtt
    mqtt = None  # type: ignore[assignment]

from app import __version__
from app.models import AppConfig
from app.settings import EventLog, redact_text

LOGGER = logging.getLogger("tecnohead_audio_local.mqtt")


class MqttService:
    """Publishes Home Assistant MQTT discovery and receives the sync button."""

    def __init__(self, config: AppConfig, events: EventLog, on_sync: Callable[[], None], client_factory: Callable[..., Any] | None = None):
        self.config = config
        self.events = events
        self.on_sync = on_sync
        self.client_factory = client_factory
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any | None = None
        self.started = False
        self.connected = False
        self.last_error: str | None = None

    @property
    def mqtt_config(self):
        return self.config.mqtt

    @property
    def topic_prefix(self) -> str:
        return self.mqtt_config.topic_prefix.strip("/")

    @property
    def command_topic(self) -> str:
        return f"{self.topic_prefix}/sync/set"

    @property
    def state_topic(self) -> str:
        return f"{self.topic_prefix}/sync/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/availability"

    @property
    def device_id(self) -> str:
        return f"tecnohead_audio_local_{self.config.installation_id}"

    def _make_client(self) -> Any:
        if self.client_factory:
            return self.client_factory(client_id=self.device_id)
        if mqtt is None:
            raise RuntimeError("dependência paho-mqtt não instalada")
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311, client_id=self.device_id)

    def _discovery_payloads(self) -> list[tuple[str, dict[str, Any]]]:
        device = {
            "identifiers": [self.device_id],
            "name": "TecnoHead Áudio Local",
            "manufacturer": "TecnoHead",
            "model": "App nativo Home Assistant",
            "sw_version": __version__,
        }
        common = {
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        button = {
            **common,
            "name": "Sincronizar músicas",
            "unique_id": f"{self.device_id}_sync",
            "object_id": "tecnohead_audio_local_sync",
            "command_topic": self.command_topic,
            "payload_press": "SYNC",
            "icon": "mdi:music-sync",
        }
        status = {
            **common,
            "name": "Status da sincronização",
            "unique_id": f"{self.device_id}_sync_status",
            "object_id": "tecnohead_audio_local_sync_status",
            "state_topic": self.state_topic,
            "icon": "mdi:music-box-multiple",
        }
        discovery = self.mqtt_config.discovery_prefix.strip("/")
        return [
            (f"{discovery}/button/{self.device_id}/sync/config", button),
            (f"{discovery}/sensor/{self.device_id}/sync_status/config", status),
        ]

    def _publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        if not self.client or not self.connected:
            return
        try:
            self.client.publish(topic, payload, qos=1, retain=retain)
        except Exception as exc:  # pragma: no cover - broker/client specific
            self.last_error = redact_text(str(exc))
            LOGGER.warning("falha ao publicar MQTT: %s", self.last_error)

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        try:
            failed = bool(getattr(reason_code, "is_failure", False)) or int(reason_code) != 0
        except (TypeError, ValueError):
            failed = True
        if failed:
            self.connected = False
            self.last_error = redact_text(f"broker recusou conexão: {reason_code}")
            self.events.append("mqtt_connect_failed", "broker MQTT recusou a conexão", error=self.last_error)
            return
        self.connected = True
        self.last_error = None
        client.subscribe(self.command_topic, qos=1)
        self._publish(self.availability_topic, "online", retain=True)
        for topic, payload in self._discovery_payloads():
            self._publish(topic, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), retain=True)
        self.publish_sync_status("idle")
        self.events.append("mqtt_connected", "publicação MQTT e botão Home Assistant disponíveis", topic=self.command_topic)

    def _on_disconnect(self, _client: Any, _userdata: Any, _disconnect_flags: Any = None, reason_code: Any = None, _properties: Any = None) -> None:
        self.connected = False
        if self.started and reason_code not in (None, 0):
            self.last_error = redact_text(f"desconectado do broker MQTT: {reason_code}")
            LOGGER.warning("%s", self.last_error)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        if message.topic != self.command_topic:
            return
        try:
            payload = message.payload.decode("utf-8", errors="replace").strip().upper()
        except Exception:
            payload = ""
        if payload not in {"SYNC", "1", "ON", "TRUE"}:
            return
        self.events.append("mqtt_sync_requested", "sincronização solicitada pelo botão MQTT")
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.on_sync)

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self.started:
            return
        self.started = True
        self.loop = loop or asyncio.get_event_loop()
        if not self.mqtt_config.enabled:
            return
        try:
            client = self._make_client()
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            if self.mqtt_config.username:
                client.username_pw_set(self.mqtt_config.username, self.mqtt_config.password or None)
            client.will_set(self.availability_topic, payload="offline", qos=1, retain=True)
            client.connect_async(self.mqtt_config.host, self.mqtt_config.port, keepalive=60)
            client.loop_start()
            self.client = client
        except Exception as exc:  # pragma: no cover - network/client specific
            self.client = None
            self.last_error = redact_text(str(exc))
            self.events.append("mqtt_start_failed", "não foi possível iniciar o MQTT", error=self.last_error)
            LOGGER.warning("MQTT indisponível: %s", self.last_error)

    def stop(self) -> None:
        self.started = False
        self.connected = False
        client, self.client = self.client, None
        if not client:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception as exc:  # pragma: no cover - client specific
            LOGGER.debug("falha ao encerrar MQTT: %s", redact_text(str(exc)))

    def update_config(self, config: AppConfig) -> None:
        previous = self.config.mqtt.model_dump(mode="json")
        current = config.mqtt.model_dump(mode="json")
        should_restart = self.started and previous != current
        self.config = config
        if should_restart:
            self.stop()
            self.start(self.loop)

    def publish_sync_status(self, status: str) -> None:
        self._publish(self.state_topic, status, retain=True)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self.mqtt_config.enabled,
            "host": self.mqtt_config.host,
            "port": self.mqtt_config.port,
            "connected": self.connected,
            "command_topic": self.command_topic,
            "discovery_prefix": self.mqtt_config.discovery_prefix,
            "password_configured": bool(self.mqtt_config.password),
            "last_error": self.last_error,
        }
