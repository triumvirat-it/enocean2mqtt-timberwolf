"""
M125: Getrennte Eichfahrt-Laufzeiten fuer Heben (100→0) und Senken (0→100).
Ein Rolladen-Motor braucht beim Heben (gegen Schwerkraft) laenger; mit nur
einer Laufzeit driftet die gerechnete Position pro Auf/Ab-Zyklus.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from app.actor_state import ActorState, ActorStateStore, estimate_position_now
from app.cascade import Cascade
from app.config import AppConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.gateway import GatewayManager
from app.tx_router import TXRouter


# ---------------------------------------------------------------------------
# Modell
# ---------------------------------------------------------------------------


def test_time_for_up_falls_back_to_down_when_unset():
    s = ActorState("d", "c", travel_time_s=20.0)
    assert s.time_for("down") == 20.0
    assert s.time_for(None) == 20.0
    assert s.time_for("up") == 20.0       # Heben nicht gesetzt -> Fallback Senken
    s.travel_time_up_s = 35.0
    assert s.time_for("up") == 35.0
    assert s.time_for("down") == 20.0     # Senken unveraendert


def test_estimate_position_uses_direction_specific_time():
    # Senken 20s, Heben 40s. Nach 4s: Senken +20%, Heben -10%.
    s = ActorState("d", "c", travel_time_s=20.0, travel_time_up_s=40.0)
    s.moving_started_at = 1000.0
    s.moving = "down"; s.position_percent = 50.0
    assert round(estimate_position_now(s, now=1004.0), 1) == 70.0
    s.moving = "up"; s.position_percent = 50.0
    assert round(estimate_position_now(s, now=1004.0), 1) == 40.0


def test_travel_time_up_persists_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "actor_state.yaml"
        store = ActorStateStore(p)
        st = store.get("d", "c")
        st.travel_time_s = 18.0
        st.travel_time_up_s = 27.0
        store.save()
        store2 = ActorStateStore(p)
        st2 = store2.get("d", "c")
        assert st2.travel_time_s == 18.0
        assert st2.travel_time_up_s == 27.0


# ---------------------------------------------------------------------------
# TXRouter: richtungsabhaengige Fahrtdauer im gesendeten Frame
# ---------------------------------------------------------------------------


class _CaptureGateway:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, packet) -> None:
        self.sent.append(packet)


def _router_with_gw(td: str, *, up: float, down: float):
    cfgdir = Path(td)
    (cfgdir / "gateways.yaml").write_text(
        yaml.safe_dump({"mqtt": {"host": "h"},
                        "gateways": [{"name": "GW1", "host": "1.2.3.4"}]}),
        encoding="utf-8",
    )
    cfg = AppConfig.load(cfgdir)
    store = ActorStateStore(cfgdir / "actor_state.yaml")
    manager = GatewayManager(cfg)
    gw = _CaptureGateway()
    manager.gateways["GW1"] = gw
    dev = Device(
        device_id="rollo", name="Rollo", floor="UG", room="B",
        channels=[DeviceChannel(channel_id="1", name="B", eep="A5-3F-7F",
                                learned_pair_id="FFAABBCC", via_gateway="GW1")],
    )
    router = TXRouter(cfg, manager, DeviceRegistry([dev]), Cascade(),
                      state_store=store)
    st = store.get("rollo", "1")
    st.travel_time_s = down
    st.travel_time_up_s = up
    return router, gw


def _frame_duration_and_cmd(packet):
    # ESP3-Data: [RORG][db3 db2 cmd db0][sender4][status]; Laufzeit in 100ms.
    payload = packet.data[1:5]
    return payload[0] * 256 + payload[1], payload[2]


def test_full_travel_up_uses_up_time():
    with tempfile.TemporaryDirectory() as td:
        router, gw = _router_with_gw(td, up=40.0, down=20.0)
        asyncio.run(router.handle_command("rollo", "1", {"command": "up", "full": True}))
        units, cmd = _frame_duration_and_cmd(gw.sent[0])
        assert units == 420   # Heben 40 + 2 = 42s -> 420 * 100ms
        assert cmd == 1        # 0x01 = up


def test_full_travel_down_uses_down_time():
    with tempfile.TemporaryDirectory() as td:
        router, gw = _router_with_gw(td, up=40.0, down=20.0)
        asyncio.run(router.handle_command("rollo", "1", {"command": "down", "full": True}))
        units, cmd = _frame_duration_and_cmd(gw.sent[0])
        assert units == 220   # Senken 20 + 2 = 22s
        assert cmd == 2        # 0x02 = down


def test_auto_adjust_writes_direction_specific_field():
    with tempfile.TemporaryDirectory() as td:
        router, _ = _router_with_gw(td, up=40.0, down=20.0)
        st = router.state_store.get("rollo", "1")
        # Fahrt aufwaerts: 30s fuer 60% -> hochgerechnet 50s volle Heben-Fahrt.
        st.moving = "up"
        st.moving_started_at = 1000.0
        st.position_percent = 60.0
        router._adjust_travel_time(st, target_position=0.0, now=1030.0, direction="up")
        assert st.travel_time_up_s == 42.0   # smoothing 0.8*40 + 0.2*50
        assert st.travel_time_s == 20.0      # Senken NICHT angefasst
