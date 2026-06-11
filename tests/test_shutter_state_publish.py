"""
M121: Rolladen-Aktor-State auf MQTT.

- device_topic kuerzt das doppelte Kanal-Segment wenn Kanal-Name == Geraete-Name
- TXRouter publiziert fuer Rolladen Position + getrennte moving_up/moving_down-Flags
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from app.actor_state import ActorStateStore
from app.cascade import Cascade
from app.config import AppConfig, MQTTConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.gateway import GatewayManager
from app.mqtt_client import MQTTPublisher
from app.tx_router import TXRouter, classify_channel_kind


# ---------------------------------------------------------------------------
# Topic-Kuerzung
# ---------------------------------------------------------------------------


def _shutter_device() -> Device:
    return Device(
        device_id="rollo_buero_1",
        name="Rollo Büro 1",
        floor="UG",
        room="Büro 1",
        channels=[
            DeviceChannel(channel_id="rollo_buero_1", name="Rollo Büro 1",
                          eep="A5-3F-7F"),
        ],
    )


def test_topic_collapses_when_channel_equals_device():
    pub = MQTTPublisher(MQTTConfig())
    dev = _shutter_device()
    t = pub.device_topic(dev, dev.channels[0])
    # Kein doppeltes "rollo_buero_1/rollo_buero_1"
    assert "rollo_buero_1/rollo_buero_1" not in t
    assert t.endswith("/ug/buero_1/rollo_buero_1/state")


def test_topic_collapses_single_channel_even_with_distinct_name():
    # Nicht multi-dimensional (1 Kanal) -> Kanal-Segment faellt IMMER weg,
    # auch wenn der Kanal anders heisst als das Geraet.
    pub = MQTTPublisher(MQTTConfig())
    dev = Device(
        device_id="rollo", name="Rollo Büro 1", floor="UG", room="Büro 1",
        channels=[DeviceChannel(channel_id="1", name="Behang", eep="A5-3F-7F")],
    )
    t = pub.device_topic(dev, dev.channels[0])
    assert t.endswith("/ug/buero_1/rollo_buero_1/state")
    assert "behang" not in t


def test_set_topic_resolves_both_short_and_legacy_long_form():
    # Ein-Kanal-Geraet: das gekuerzte /set-Topic UND die alte Lang-Form
    # (mit Kanal-Segment) muessen beide auf (device_id, channel_id) aufloesen.
    pub = MQTTPublisher(MQTTConfig(base_topic="enocean"))
    dev = Device(
        device_id="rollo", name="Rollo Büro 1", floor="UG", room="Büro 1",
        channels=[DeviceChannel(channel_id="1", name="Behang", eep="A5-3F-7F")],
    )
    pub.devices = DeviceRegistry([dev])
    short = "enocean/ug/buero_1/rollo_buero_1/set"
    long = "enocean/ug/buero_1/rollo_buero_1/behang/set"
    assert pub._resolve_set_target(short) == ("rollo", "1")
    assert pub._resolve_set_target(long) == ("rollo", "1")


def test_topic_keeps_distinct_channel_segment():
    pub = MQTTPublisher(MQTTConfig())
    dev = Device(
        device_id="dev", name="Rollos", floor="UG", room="Büro 1",
        channels=[
            DeviceChannel(channel_id="1", name="Rollo Links", eep="A5-3F-7F"),
            DeviceChannel(channel_id="2", name="Rollo Rechts", eep="A5-3F-7F"),
        ],
    )
    t = pub.device_topic(dev, dev.channels[0])
    assert t.endswith("/rollos/rollo_links/state")


# ---------------------------------------------------------------------------
# Aktor-State-Publish
# ---------------------------------------------------------------------------


class _CapturePublisher:
    """Faengt publish_device-Aufrufe ab statt zu senden."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def publish_device(self, device, channel, payload) -> None:
        self.calls.append((device.device_id, channel.channel_id, payload))


def _router(cfgdir: Path) -> TXRouter:
    (cfgdir / "gateways.yaml").write_text(
        yaml.safe_dump({"mqtt": {"host": "h"}, "gateways": []}), encoding="utf-8",
    )
    cfg = AppConfig.load(cfgdir)
    store = ActorStateStore(cfgdir / "actor_state.yaml")
    router = TXRouter(cfg, GatewayManager(cfg), DeviceRegistry(), Cascade(),
                      state_store=store)
    router.publisher = _CapturePublisher()
    return router


class _CaptureGateway:
    """Faengt gesendete ESP3-Pakete ab statt sie ueber TCP zu schicken."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, packet) -> None:
        self.sent.append(packet)


def test_full_travel_command_sends_long_duration_frame():
    # M124: {"command": "down", "full": True} (Langbefehl der Visu) muss eine
    # Fahrt mit Laufzeit+Puffer ausloesen, NICHT im "bereits am Ziel"-Kurzschluss
    # verschwinden. Default-Laufzeit 25s + 2s Puffer = 27s -> 270 * 100ms.
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        (cfgdir / "gateways.yaml").write_text(
            yaml.safe_dump({
                "mqtt": {"host": "h"},
                "gateways": [{"name": "GW1", "host": "1.2.3.4"}],
            }), encoding="utf-8",
        )
        cfg = AppConfig.load(cfgdir)
        store = ActorStateStore(cfgdir / "actor_state.yaml")
        manager = GatewayManager(cfg)
        gw = _CaptureGateway()
        manager.gateways["GW1"] = gw
        dev = Device(
            device_id="rollo", name="Rollo", floor="UG", room="Büro",
            channels=[DeviceChannel(channel_id="1", name="Behang",
                                    eep="A5-3F-7F",
                                    learned_pair_id="FFAABBCC",
                                    via_gateway="GW1")],
        )
        router = TXRouter(cfg, manager, DeviceRegistry([dev]), Cascade(),
                          state_store=store)

        asyncio.run(router.handle_command(
            "rollo", "1", {"command": "down", "full": True}))

        assert len(gw.sent) == 1
        payload = gw.sent[0].data[1:5]   # [RORG][db3 db2 cmd db0][sender..][status]
        assert payload[0] == 1 and payload[1] == 14   # 270 * 100ms = 27s
        assert payload[2] == 2                          # 0x02 = Ab/down


def test_shutter_payload_has_position_and_split_moving_flags():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        router = _router(cfgdir)
        dev = _shutter_device()
        ch = dev.channels[0]
        assert classify_channel_kind(ch) == "shutter"

        # Faehrt gerade ab
        st = router.state_store.get(dev.device_id, ch.channel_id)
        st.moving = "down"
        st.position_percent = 42.3
        st.calibrated = True

        asyncio.run(router._publish_actor_state(dev, ch))

        assert len(router.publisher.calls) == 1
        _, _, payload = router.publisher.calls[0]
        assert payload["position"] == 42        # 0=auf, 100=zu (interne Konvention)
        assert payload["moving_down"] is True
        assert payload["moving_up"] is False
        assert payload["calibrated"] is True


def test_shutter_payload_flags_false_when_stopped():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        router = _router(cfgdir)
        dev = _shutter_device()
        ch = dev.channels[0]
        st = router.state_store.get(dev.device_id, ch.channel_id)
        st.moving = None
        st.position_percent = 0.0

        asyncio.run(router._publish_actor_state(dev, ch))

        _, _, payload = router.publisher.calls[0]
        assert payload["moving_up"] is False
        assert payload["moving_down"] is False
        assert payload["position"] == 0


def test_set_subscriptions_cover_collapsed_and_full_depth():
    # Regression: durch die Ein-Kanal-Kuerzung ist das Set-Topic nur noch 3
    # Ebenen tief (floor/room/device) — das Abo muss diese Tiefe abdecken,
    # nicht nur die alte 4-Ebenen-Form (floor/room/device/channel).
    pub = MQTTPublisher(MQTTConfig(base_topic="enocean"))
    subs = pub._set_subscriptions()
    assert "enocean/+/+/+/set" in subs        # Ein-Kanal, gekuerzt (3 Ebenen)
    assert "enocean/+/+/+/+/set" in subs      # Multi-Kanal (4 Ebenen)
    assert "enocean/devices/+/+/set" in subs  # Legacy mit IDs


def test_bare_value_maps_to_position_for_shutter():
    with tempfile.TemporaryDirectory() as td:
        router = _router(Path(td))
        ch = _shutter_device().channels[0]
        # _handle_incoming verpackt eine reine Zahl-Payload zu {"value": N}
        assert router._normalize_value_command(ch, {"value": 30}) == {"position": 30.0}
        # Ueberlauf wird geklemmt
        assert router._normalize_value_command(ch, {"value": 150})["position"] == 100.0
        # Zusammengesetzte Commands bleiben unangetastet
        assert router._normalize_value_command(ch, {"command": "up"}) == {"command": "up"}


def test_bare_value_maps_to_dim_and_state():
    with tempfile.TemporaryDirectory() as td:
        router = _router(Path(td))
        dim_ch = DeviceChannel(channel_id="1", name="Licht", eep="A5-38-08",
                               meta={"device_type": "Dimmer_16"})
        sw_ch = DeviceChannel(channel_id="1", name="Licht", eep="A5-38-08",
                              meta={"device_type": "Switch_1"})
        assert router._normalize_value_command(dim_ch, {"value": 55}) == {"dim": 55}
        assert router._normalize_value_command(sw_ch, {"value": 1}) == {"state": True}
        assert router._normalize_value_command(sw_ch, {"value": 0}) == {"state": False}


def test_non_shutter_does_not_publish_actor_state():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        router = _router(cfgdir)
        dev = Device(
            device_id="licht", name="Licht", floor="EG", room="Kueche",
            channels=[DeviceChannel(channel_id="1", name="Licht",
                                    eep="A5-38-08-01")],
        )
        ch = dev.channels[0]
        asyncio.run(router._publish_actor_state(dev, ch))
        assert router.publisher.calls == []  # nur Rolladen publishen Aktor-State
