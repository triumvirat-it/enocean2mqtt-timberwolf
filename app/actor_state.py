"""
Aktor-State-Verwaltung — Position/Dim-Wert/An-Aus pro Channel.

Hauptzweck: Bei Eltako-Aktoren die Position eines Rolladens (0-100%) verfolgen
obwohl der Aktor selbst keine Position kennt. Wir tracken das softwareseitig:

- Eichfahrt liefert Laufzeit (Sekunden für 0→100%)
- Bei jedem gesendeten Befehl: Position-Berechnung über verstrichene Zeit
- Bei Endlagen-Feedback: Position auf 0% bzw 100% korrigieren

Persistiert in /data/actor_state.yaml damit State über Container-Restart bleibt.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class ActorState:
    """Aktuelle Werte eines Kanals."""
    device_id: str
    channel_id: str
    # Allgemein
    on: bool = False              # An/Aus-Status
    # Dimmer
    dim_percent: int = 0          # 0..100
    # Rolladen
    position_percent: float = 0.0  # 0=oben, 100=unten (Eltako-Konvention)
    moving: str | None = None      # "up", "down", oder None (still)
    moving_started_at: float | None = None  # Zeitstempel des Bewegungs-Beginns
    moving_target: float | None = None       # Ziel-Position (falls "fahre zu X%")
    # Konfiguration (Eichfahrt-Ergebnis)
    # Getrennte Laufzeiten: ein Rolladen-Motor braucht beim HEBEN (100→0,
    # gegen die Schwerkraft) typisch laenger als beim SENKEN (0→100). Mit nur
    # einer Laufzeit driftet die gerechnete Position bei jedem Auf/Ab-Zyklus.
    travel_time_s: float = 25.0    # Senken (0→100), Default 25s
    travel_time_up_s: float = 0.0  # Heben (100→0); 0 = wie Senken (Fallback)
    calibrated: bool = False       # Ob Eichfahrt durchgeführt wurde
    # Metadaten
    last_command: str | None = None
    last_command_at: float | None = None
    last_feedback_at: float | None = None

    def time_for(self, direction: str | None) -> float:
        """
        Laufzeit (Sekunden für volle Fahrt) in der gegebenen Richtung.
        'up' = Heben (100→0) nutzt travel_time_up_s, faellt aber auf
        travel_time_s zurueck wenn die Heben-Zeit nicht separat eingemessen
        wurde (0). Alles andere ('down'/None) = Senken (travel_time_s).
        """
        if direction == "up" and self.travel_time_up_s and self.travel_time_up_s > 0:
            return self.travel_time_up_s
        return self.travel_time_s

    def to_dict(self) -> dict[str, Any]:
        d = {
            "device_id": self.device_id,
            "channel_id": self.channel_id,
            "on": self.on,
            "dim_percent": self.dim_percent,
            "position_percent": round(self.position_percent, 1),
            "moving": self.moving,
            "travel_time_s": self.travel_time_s,
            "travel_time_up_s": self.travel_time_up_s,
            "calibrated": self.calibrated,
            "last_command": self.last_command,
            "last_command_at": self.last_command_at,
            "last_feedback_at": self.last_feedback_at,
        }
        return d


class ActorStateStore:
    """
    Persistente Verwaltung aller ActorStates.

    Updates passieren live im Container; persistiert wird periodisch und
    bei expliziten Aktionen (Eichfahrt-Ende, Befehl).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._states: dict[tuple[str, str], ActorState] = {}
        self._dirty = False
        self.load()

    def _key(self, device_id: str, channel_id: str) -> tuple[str, str]:
        return (device_id, channel_id)

    def get(self, device_id: str, channel_id: str) -> ActorState:
        """Holt State, legt an wenn nicht vorhanden."""
        k = self._key(device_id, channel_id)
        if k not in self._states:
            self._states[k] = ActorState(device_id=device_id, channel_id=channel_id)
            self._dirty = True
        return self._states[k]

    def all(self) -> list[ActorState]:
        return list(self._states.values())

    def load(self) -> None:
        if not self.path.exists():
            log.info("ActorStateStore: keine bestehende Datei %s", self.path)
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("ActorStateStore: Lese-Fehler %s: %s", self.path, exc)
            return
        for raw in doc.get("states", []):
            try:
                s = ActorState(
                    device_id=raw["device_id"],
                    channel_id=raw["channel_id"],
                    on=raw.get("on", False),
                    dim_percent=int(raw.get("dim_percent", 0)),
                    position_percent=float(raw.get("position_percent", 0.0)),
                    travel_time_s=float(raw.get("travel_time_s", 25.0)),
                    travel_time_up_s=float(raw.get("travel_time_up_s", 0.0)),
                    calibrated=bool(raw.get("calibrated", False)),
                    last_command=raw.get("last_command"),
                    last_command_at=raw.get("last_command_at"),
                    last_feedback_at=raw.get("last_feedback_at"),
                )
                self._states[self._key(s.device_id, s.channel_id)] = s
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("ActorStateStore: Ungueltiger Eintrag: %s", exc)
        log.info("ActorStateStore: %d Aktor-States geladen", len(self._states))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"states": [s.to_dict() for s in self._states.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
        tmp.replace(self.path)
        self._dirty = False

    def mark_dirty(self) -> None:
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty


# ---------------------------------------------------------------------------
# Position-Berechnung für Rolladen
# ---------------------------------------------------------------------------


def estimate_position_now(state: ActorState, now: float | None = None) -> float:
    """
    Berechnet die aktuelle Position basierend auf:
    - state.position_percent (Position bei Bewegungs-Start)
    - state.moving (Richtung)
    - state.moving_started_at (Beginn)
    - state.travel_time_s (Gesamtlaufzeit)
    """
    now = now if now is not None else time.time()
    if not state.moving or not state.moving_started_at:
        return state.position_percent
    elapsed = now - state.moving_started_at
    delta_pct = (elapsed / max(0.1, state.time_for(state.moving))) * 100.0
    if state.moving == "down":
        new_pos = state.position_percent + delta_pct
    elif state.moving == "up":
        new_pos = state.position_percent - delta_pct
    else:
        return state.position_percent
    # Bei Ziel-Vorgabe: nicht über das Ziel hinaus
    if state.moving_target is not None:
        if state.moving == "down":
            new_pos = min(new_pos, state.moving_target)
        elif state.moving == "up":
            new_pos = max(new_pos, state.moving_target)
    return max(0.0, min(100.0, new_pos))


def commit_movement(state: ActorState, now: float | None = None) -> None:
    """
    Schreibt die aktuelle berechnete Position in state.position_percent
    und stoppt die Bewegung. Aufzurufen bei Stop-Befehl oder Stop-Feedback.
    """
    state.position_percent = estimate_position_now(state, now=now)
    state.moving = None
    state.moving_started_at = None
    state.moving_target = None
