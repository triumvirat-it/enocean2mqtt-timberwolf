"""Tests fuer Einstellungen: globale Defaults (PTM-Polung) + MQTT-Persistenz."""
from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from app.cascade import Cascade
from app.config import AppConfig
from app.devices import DeviceChannel, DeviceRegistry
from app.gateway import GatewayManager
from app.tx_router import TXRouter


def _write_cfg(cfgdir: Path, extra: dict | None = None) -> None:
    raw = {"mqtt": {"host": "192.168.1.59"}, "gateways": []}
    if extra:
        raw.update(extra)
    (cfgdir / "gateways.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_defaults_ptm_polarity_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        _write_cfg(cfgdir)
        cfg = AppConfig.load(cfgdir)
        assert cfg.defaults.ptm_on_press == "I"  # Default
        cfg.defaults.ptm_on_press = "0"
        cfg.mqtt.host = "10.0.0.5"
        cfg.save(cfgdir)
        cfg2 = AppConfig.load(cfgdir)
        assert cfg2.defaults.ptm_on_press == "0"
        assert cfg2.mqtt.host == "10.0.0.5"
        raw = yaml.safe_load((cfgdir / "gateways.yaml").read_text(encoding="utf-8"))
        assert raw["defaults"]["ptm_on_press"] == "0"


def _switch_router(cfgdir: Path, polarity: str) -> tuple[TXRouter, DeviceChannel]:
    from app.actor_state import ActorStateStore

    cfg = AppConfig.load(cfgdir)
    cfg.defaults.ptm_on_press = polarity
    manager = GatewayManager(cfg)
    devices = DeviceRegistry()
    cascade = Cascade()
    store = ActorStateStore(cfgdir / "actor_state.yaml")
    router = TXRouter(cfg, manager, devices, cascade, state_store=store)
    ch = DeviceChannel(
        channel_id="1", name="Licht", eep="F6-02-01",
        meta={"device_type": "Switch_1"},
    )
    return router, ch


def test_ptm_polarity_global_default():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        _write_cfg(cfgdir)
        # Polung "I": oben (AI) schaltet EIN
        r, ch = _switch_router(cfgdir, "I")
        r._apply_ptm_press("dev", "1", ch, {"pressed": True, "rocker_1": "AI"})
        assert r.state_store.get("dev", "1").on is True

        # Polung "0": oben (AI) schaltet jetzt AUS
        r2, ch2 = _switch_router(cfgdir, "0")
        r2.state_store.get("dev", "1").on = True
        r2._apply_ptm_press("dev", "1", ch2, {"pressed": True, "rocker_1": "AI"})
        assert r2.state_store.get("dev", "1").on is False


def test_mqtt_set_topic_resolves_name_slugs_to_ids():
    """Namens-basiertes .../set-Topic (Slugs) muss auf die ECHTEN
    device_id/channel_id aufgeloest werden — nicht die Slugs als IDs nehmen."""
    from app.config import MQTTConfig
    from app.devices import Device
    from app.mqtt_client import MQTTPublisher

    pub = MQTTPublisher(MQTTConfig(base_topic="enocean"))
    dev = Device(
        device_id="eltako_fdg14_fdg14", name="Dimmer FDG14",
        floor="UG", room="Büro 1",
        channels=[DeviceChannel(channel_id="1.16", name="LED Decke Büro 1",
                                eep="A5-38-08")],
    )
    pub.devices = DeviceRegistry([dev])
    topic = "enocean/ug/buero_1/dimmer_fdg14/led_decke_buero_1/set"
    assert pub._resolve_set_target(topic) == ("eltako_fdg14_fdg14", "1.16")
    # Legacy-Format mit echten IDs
    assert pub._resolve_set_target("enocean/devices/foo/1/set") == ("foo", "1")
    # Unbekanntes Topic -> None (Befehl wird verworfen + geloggt)
    assert pub._resolve_set_target("enocean/a/b/c/d/set") is None


def _dimmer_router(cfgdir: Path):
    from app.actor_state import ActorStateStore

    cfg = AppConfig.load(cfgdir)
    router = TXRouter(
        cfg, GatewayManager(cfg), DeviceRegistry(), Cascade(),
        state_store=ActorStateStore(cfgdir / "actor_state.yaml"),
    )
    ch = DeviceChannel(
        channel_id="1.1", name="Dimmer", eep="A5-38-08",
        meta={"device_type": "Dimmer_16"},
    )
    return router, ch


def test_dimmer_onoff_uses_remembered_dim_value():
    """Eltako-Dimmer: {state:true} ohne Wert -> auf gemerkten actor_state-Wert,
    {state:false} -> 0, expliziter {dim:X} bleibt, kein Memory -> 100."""
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        _write_cfg(cfgdir)
        r, ch = _dimmer_router(cfgdir)
        r.state_store.get("dev", "1.1").dim_percent = 40
        assert r._apply_dimmer_onoff_memory("dev", "1.1", ch, {"state": True})["dim"] == 40
        assert r._apply_dimmer_onoff_memory("dev", "1.1", ch, {"state": False})["dim"] == 0
        # expliziter Dim-Wert wird NICHT angefasst
        assert r._apply_dimmer_onoff_memory("dev", "1.1", ch, {"dim": 70}) == {"dim": 70}
        # kein gemerkter Wert -> 100% Notnagel
        r.state_store.get("dev", "1.1").dim_percent = 0
        assert r._apply_dimmer_onoff_memory("dev", "1.1", ch, {"on": True})["dim"] == 100


def test_ptm_polarity_per_channel_override():
    with tempfile.TemporaryDirectory() as td:
        cfgdir = Path(td)
        _write_cfg(cfgdir)
        # Global "I", aber dieser Kanal ueberschreibt auf "0"
        r, ch = _switch_router(cfgdir, "I")
        ch.meta["ptm_on_press"] = "0"
        r.state_store.get("dev", "1").on = True
        r._apply_ptm_press("dev", "1", ch, {"pressed": True, "rocker_1": "AI"})
        assert r.state_store.get("dev", "1").on is False
