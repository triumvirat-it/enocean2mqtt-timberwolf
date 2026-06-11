"""
M124: Rolladen-Befehlsschema der Timberwolf-Visu auf dem EINEN .../set-Topic.
Ein JSON-Key je Connector, Wert = Richtung (0=Auf/up, 1=Ab/down):

  {"move": 0|1}  Langbefehl -> volle Fahrt bis Endanschlag
  {"step": 0|1}  Kurzbefehl -> kurzer Tipp (Lamelle)
  {"stop": true} -> Stopp,  {"stop": false} -> nichts
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from app.cascade import Cascade
from app.config import AppConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.gateway import GatewayManager
from app.tx_router import SHUTTER_STEP_S, TXRouter


def _router_with_shutter(cfgdir: Path):
    (cfgdir / "gateways.yaml").write_text(
        yaml.safe_dump({"mqtt": {"host": "h"}, "gateways": []}), encoding="utf-8",
    )
    cfg = AppConfig.load(cfgdir)
    from app.actor_state import ActorStateStore
    store = ActorStateStore(cfgdir / "actor_state.yaml")
    dev = Device(
        device_id="rollo", name="Rollo Büro 1", floor="UG", room="Büro 1",
        channels=[DeviceChannel(channel_id="1", name="Rollo Büro 1", eep="A5-3F-7F")],
    )
    router = TXRouter(cfg, GatewayManager(cfg), DeviceRegistry([dev]), Cascade(),
                      state_store=store)
    return router


def _translate(cmd: dict) -> dict | None:
    """Nur die Schema-Uebersetzung pruefen (ohne Senden)."""
    with tempfile.TemporaryDirectory() as td:
        router = _router_with_shutter(Path(td))
        return router._shutter_visu_command(cmd)


def test_move_is_full_travel():
    assert _translate({"move": 0}) == {"command": "up", "full": True}     # Auf
    assert _translate({"move": 1}) == {"command": "down", "full": True}   # Ab


def test_step_is_short_tip():
    assert _translate({"step": 0}) == {"command": "up", "duration_s": SHUTTER_STEP_S}
    assert _translate({"step": 1}) == {"command": "down", "duration_s": SHUTTER_STEP_S}


def test_stop_only_on_truthy():
    assert _translate({"stop": True}) == {"command": "stop"}
    assert _translate({"stop": 1}) == {"command": "stop"}
    assert _translate({"stop": False}) is None
    assert _translate({"stop": 0}) is None


def test_string_values_tolerated():
    # JSON aus der Visu koennte auch "0"/"1" als String liefern
    assert _translate({"move": "0"}) == {"command": "up", "full": True}
    assert _translate({"move": "1"}) == {"command": "down", "full": True}


def test_non_shutter_command_passes_through():
    assert _translate({"position": 40}) == {"position": 40}
