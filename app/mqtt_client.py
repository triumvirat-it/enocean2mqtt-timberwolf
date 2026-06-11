"""
MQTT-Client gegen den Timberwolf-internen Broker.

Topic-Layout:
    enocean/gw/<gw_name>/<sender_id>/state            (raw, immer)
    enocean/<floor>/<room>/<device_id>/<channel_id>/state   (named, wenn Device bekannt)
    enocean/<floor>/<room>/<device_id>/<channel_id>/set     (Commands)
    enocean/_/status                                  (LWT, retained, online/offline)
    enocean/_/teachin                                 (Anlern-Modus via Web-UI)

Wenn floor oder room leer sind, wird "ohne_etage" bzw. "ohne_raum" eingesetzt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

import asyncio_mqtt as aiomqtt

from .config import MQTTConfig

log = logging.getLogger(__name__)


CommandHandler = Callable[[str, str, dict], Awaitable[None]]
# device_id, channel_id, command_dict


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    """MQTT-safe slug — lowercase, underscores, ascii only."""
    out = (
        s.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("&", "und")
    )
    # ALLE non-alnum (Space, Em-Dash, /, ...) zu einem einzigen Underscore
    out = _SLUG_RE.sub("_", out).strip("_")
    return out or "unbenannt"


def room_segment(room: str) -> str:
    """
    Letztes Segment eines hierarchischen Raum-Pfads, slugified.
    "Beispielwohnung/Wohnzimmer" -> "wohnzimmer"
    Leer -> "" (Segment faellt im Topic-Aufbau weg)
    """
    if not room:
        return ""
    parts = [seg for seg in room.split("/") if seg.strip()]
    last = parts[-1] if parts else ""
    return slugify(last) if last else ""


def floor_segment(floor: str) -> str:
    """Floor slugifiziert. Leer -> "" (Segment faellt im Topic-Aufbau weg)."""
    if not floor:
        return ""
    return slugify(floor)


def name_segment(name: str, fallback: str = "") -> str:
    """
    Name eines Devices oder Channels slugifiziert.
    Wenn name leer ist, wird fallback genutzt (typisch: device_id bzw.
    channel_id). Wenn beides leer, "unbenannt".
    """
    if name and name.strip():
        return slugify(name)
    if fallback and fallback.strip():
        return slugify(fallback)
    return "unbenannt"


class MQTTPublisher:
    def __init__(self, cfg: MQTTConfig) -> None:
        self.cfg = cfg
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._cmd_handler: CommandHandler | None = None
        # DeviceRegistry — noetig um eingehende .../set-Topics (Name-Slugs) auf
        # (device_id, channel_id) aufzuloesen. Von main.py gesetzt.
        self.devices = None

    @property
    def base(self) -> str:
        return self.cfg.base_topic.rstrip("/")

    @property
    def is_connected(self) -> bool:
        """True wenn die MQTT-Verbindung aktuell steht (fuer /api/info-Status)."""
        return self._connected.is_set()

    def set_command_handler(self, handler: CommandHandler) -> None:
        """Wird in M5 verwendet, um .../set-Nachrichten abzuarbeiten."""
        self._cmd_handler = handler

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                will = aiomqtt.Will(
                    topic=f"{self.base}/_/status",
                    payload=b"offline",
                    qos=self.cfg.qos,
                    retain=True,
                )
                async with aiomqtt.Client(
                    hostname=self.cfg.host,
                    port=self.cfg.port,
                    username=self.cfg.username,
                    password=self.cfg.password,
                    client_id=self.cfg.client_id,
                    will=will,
                ) as client:
                    self._client = client
                    log.info("MQTT verbunden: %s:%d", self.cfg.host, self.cfg.port)
                    await client.publish(
                        f"{self.base}/_/status",
                        payload="online",
                        qos=self.cfg.qos,
                        retain=True,
                    )
                    self._connected.set()
                    backoff = 1.0
                    # asyncio-mqtt 0.16.2: client.messages() ist eine METHODE,
                    # die als async-Context-Manager den Nachrichten-Generator
                    # liefert (NICHT client.messages als Property — das war ein
                    # Bug, der erst sichtbar wurde als die Verbindung endlich
                    # stand: paho-Pin + bullseye-Base). Wir hoeren auf zwei
                    # Patterns:
                    #   - legacy: enocean/devices/<device>/<channel>/set
                    #   - neu:    enocean/<floor>/<room>/<device>/<channel>/set
                    async with client.messages() as messages:
                        for pattern in self._set_subscriptions():
                            await client.subscribe(pattern, qos=self.cfg.qos)
                        async for msg in messages:
                            await self._handle_incoming(msg)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT-Verbindungsfehler: %s — retry in %.1fs", exc, backoff)
                self._connected.clear()
                self._client = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Unerwarteter Fehler (z.B. Library-Inkompatibilitaet wie
                # paho-mqtt 2.x vs asyncio-mqtt 0.16.2 — Client.message_retry_set
                # fehlt). FRUEHER crashte der Task hier lautlos -> kein Publish,
                # kein Log. Jetzt loggen + retry, damit es sichtbar wird.
                log.error("MQTT-Client unerwarteter Fehler: %s — retry in %.1fs",
                          exc, backoff)
                self._connected.clear()
                self._client = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    def _set_subscriptions(self) -> list[str]:
        """
        MQTT-Abo-Patterns fuer eingehende .../set-Befehle.

        Die Topic-Tiefe variiert, weil floor/room wegfallen koennen UND
        Ein-Kanal-Geraete das Kanal-Segment weglassen ('+' matcht in MQTT
        GENAU eine Ebene, daher mehrere Abos statt einem):
          {base}/<device>/set                              (1)  floor+room leer, 1 Kanal
          {base}/<a>/<device>/set                          (2)
          {base}/<floor>/<room>/<device>/set               (3)  Ein-Kanal, gekuerzt
          {base}/<floor>/<room>/<device>/<channel>/set     (4)  Multi-Kanal
        Plus das Legacy-Format mit echten IDs:
          {base}/devices/<device_id>/<channel_id>/set
        """
        subs = [self.base + "/+" * depth + "/set" for depth in (1, 2, 3, 4)]
        subs.append(f"{self.base}/devices/+/+/set")
        return subs

    def _resolve_set_target(self, topic: str) -> tuple[str, str] | None:
        """
        Mappt ein eingehendes .../set-Topic auf (device_id, channel_id).

        Zwei Formate:
          - Legacy:  {base}/devices/<device_id>/<channel_id>/set  (IDs direkt)
          - Namens-basiert: {base}/<floor>/<room>/<name>/<name>/set
            Hier sind die Segmente NAMEN (slugified), NICHT die IDs! Darum
            matchen wir gegen die publizierten Topics (gleiche device_topic-
            Logik) und liefern die ECHTEN device_id/channel_id zurueck.
            (Frueher wurden die Name-Slugs faelschlich als IDs verwendet ->
            Befehl lief ins Leere.)
        """
        rel = topic[len(self.base) + 1:-len("/set")]
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "devices":
            return parts[1], parts[2]
        if self.devices is not None:
            for d in self.devices.all():
                for c in d.channels:
                    t = self.device_topic(d, c, "set")
                    if t == topic:
                        return d.device_id, c.channel_id
                    # Rueckwaerts-Kompatibilitaet: Ein-Kanal-Geraete kuerzen
                    # neuerdings das Kanal-Segment weg. Bestehende Automationen
                    # (oder retained Topics) mit der alten Lang-Form
                    # .../<device>/<channel>/set weiterhin aufloesen.
                    if len(d.channels) <= 1:
                        cseg = name_segment(
                            getattr(c, "name", "") or "",
                            fallback=getattr(c, "channel_id", ""),
                        )
                        if t[:-len("/set")] + "/" + cseg + "/set" == topic:
                            return d.device_id, c.channel_id
        return None

    async def _handle_incoming(self, msg: aiomqtt.Message) -> None:
        if not self._cmd_handler:
            return
        topic = msg.topic.value
        if not topic.endswith("/set"):
            return
        target = self._resolve_set_target(topic)
        if target is None:
            log.warning(
                "MQTT /set: kein Geraet/Kanal fuer Topic '%s' — die Name-Slugs "
                "muessen zu einem publizierten Topic passen (oder Legacy-Format "
                "%s/devices/<device_id>/<channel_id>/set nutzen)", topic, self.base,
            )
            return
        device_id, channel_id = target
        try:
            payload = json.loads(msg.payload.decode())
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"raw": msg.payload.hex()}
        try:
            await self._cmd_handler(device_id, channel_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("Command-Handler-Fehler für %s/%s: %s", device_id, channel_id, exc)

    async def reload(self, cfg: MQTTConfig) -> None:
        """
        Uebernimmt eine neue MQTT-Config und verbindet neu — ohne Container-
        Neustart. Der laufende Verbindungs-Task wird abgebrochen (trennt die
        alte Verbindung sauber via async-with-Exit) und mit der neuen Config
        neu gestartet. Der Command-Handler bleibt erhalten.
        """
        log.info("MQTT-Config neu laden: %s:%d", cfg.host, cfg.port)
        self.cfg = cfg
        self._connected.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # _stop bleibt ungesetzt -> _run() verbindet erneut mit der neuen Config
        self._task = asyncio.create_task(self._run(), name="mqtt")

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        self._stop.set()
        if self._client:
            try:
                await self._client.publish(
                    f"{self.base}/_/status",
                    payload="offline",
                    qos=self.cfg.qos,
                    retain=True,
                )
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def publish_raw(
        self, gw_name: str, sender_id: str, payload: dict[str, Any]
    ) -> None:
        if not self._client:
            return
        topic = f"{self.base}/gw/{slugify(gw_name)}/{sender_id.upper()}/state"
        await self._client.publish(
            topic,
            payload=json.dumps(payload, ensure_ascii=False),
            qos=self.cfg.qos,
            retain=self.cfg.retain_state,
        )

    async def publish_device(
        self, device, channel, payload: dict[str, Any]
    ) -> None:
        """
        Veroeffentlicht ein Device-Telegramm.

        Topic: {base}/{floor}/{room}/{device_name}/{channel_name}/state
        Leere floor/room werden ausgelassen.
        """
        if not self._client:
            return
        topic = self.device_topic(device, channel)
        await self._client.publish(
            topic,
            payload=json.dumps(payload, ensure_ascii=False),
            qos=self.cfg.qos,
            retain=self.cfg.retain_state,
        )

    def device_topic(self, device, channel, suffix: str = "state") -> str:
        """
        Topic-String zu einem Device/Channel.

        Schema: {base}/{floor}/{room}/{device_name}/{channel_name}/{suffix}
        - floor/room werden weggelassen wenn leer (kein doppelter Slash)
        - device_name/channel_name aus device.name / channel.name slugifiziert;
          Fallback auf device_id / channel_id wenn name leer
        """
        # M95: Channel-Override hat Vorrang (Raum den DIESER Channel schaltet),
        # sonst erbt er floor/room vom Device (Standort des Aktors).
        floor = floor_segment(
            (getattr(channel, "floor", "") or "").strip()
            or getattr(device, "floor", "")
        )
        room = room_segment(
            (getattr(channel, "room", "") or "").strip()
            or getattr(device, "room", "")
        )
        d = name_segment(
            getattr(device, "name", "") or "",
            fallback=getattr(device, "device_id", ""),
        )
        c = name_segment(
            getattr(channel, "name", "") or "",
            fallback=getattr(channel, "channel_id", ""),
        )
        # Ein-Kanal-Geraete sind nicht multi-dimensional — das Kanal-Segment
        # ist dort redundant und faellt IMMER weg:
        #   .../rollo_buero_1/state   statt   .../rollo_buero_1/<kanal>/state
        # (unabhaengig davon, ob Kanal- und Geraete-Name zufaellig gleich sind).
        # Erst Multi-Kanal-Geraete brauchen das Segment, um ihre Kanaele zu
        # unterscheiden — dort bleibt es erhalten.
        # Leere Segmente filtern (floor/room koennen leer sein).
        channels = getattr(device, "channels", None) or []
        parts = [self.base, floor, room, d]
        if len(channels) > 1 and c:
            parts.append(c)
        parts.append(suffix)
        return "/".join(p for p in parts if p)
