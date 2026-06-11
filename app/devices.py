"""
Device-Registry — die "EnOcean-Geräte" die wir kennen.
Wird aus data/devices.yaml geladen, persistent gespeichert, von der Web-UI verwaltet.

Konzept:
- Ein "Device" hat eine Hardware-Identität (Hersteller, Typ) und einen logischen Namen.
- Es kann MEHRERE Channels haben (Mehrkanal-Aktor: 2.1 Schaltaktor, 2.2 PTM-Sender, ...).
- Jeder Channel hat eine eigene Sender-ID (32-bit) und einen EEP.
- Räume sind hierarchisch ("Beispielwohnung/Wohnzimmer").
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


class ObserverBinding(BaseModel):
    """
    Beobachter-Binding mit optionalem Match-Filter (M-Observer-Split).

    Hintergrund: Ein FT55-Wippentaster sendet alle 4 Tasten (A_top, A_bottom,
    B_top, B_bottom) mit derselben Sender-ID. Wenn nur Wippe A im Aktor
    angelernt ist, soll auch nur ein Press auf Wippe A den Aktor-State
    optimistisch updaten — ein Press auf Wippe B darf ihn NICHT triggern.

    match-Werte:
      "any"            (default, legacy)  — jedes Telegramm matcht
      "rocker:A"       — decoded.rocker_side == 'A'
      "rocker:B"       — decoded.rocker_side == 'B'
      "event:A_top"    — UT 3 (links oben, Datenbyte 0x30)
      "event:A_bottom" — UT 4 (links unten, 0x10)
      "event:B_top"    — UT 1 (rechts oben, 0x70)
      "event:B_bottom" — UT 2 (rechts unten, 0x50)

    YAML-Form:
      observers:
        - "FE000001"                              # legacy = "any"
        - sender_id: "FE000001"
          match: "rocker:A"
    """

    sender_id: str = Field(..., pattern=r"^[0-9A-Fa-f]{8}$")
    match: str = Field(
        "any",
        description="any | rocker:A | rocker:B | event:A_top|A_bottom|B_top|B_bottom",
    )


def _normalize_observer(
    obs: "str | ObserverBinding | dict",
) -> tuple[str, str]:
    """Liefert (sender_id_upper, match) — legacy-string laeuft als 'any'."""
    if isinstance(obs, ObserverBinding):
        return obs.sender_id.upper(), obs.match
    if isinstance(obs, dict):
        sid = (obs.get("sender_id") or "").upper()
        return sid, obs.get("match", "any")
    return (obs or "").upper(), "any"


def observer_match(match: str, decoded: dict) -> bool:
    """
    Prueft ob das aktuelle Telegramm zum match-Filter passt.
    Legacy 'any' und unbekannte Werte matchen pauschal True (sicher fuer
    aeltere Bindings ohne Filter-Awareness).
    """
    if not match or match == "any":
        return True
    if match.startswith("rocker:"):
        wanted = match.split(":", 1)[1]
        return decoded.get("rocker_side") == wanted
    if match.startswith("event:"):
        wanted = match.split(":", 1)[1]
        return decoded.get("event") == wanted
    return True  # unbekannter match-Wert: nicht restriktiver werden


class SenderBinding(BaseModel):
    """
    Eine Sender-ID + Gateway-Zuordnung fuer einen Aktor-Channel (M59).

    Pro Channel koennen MEHRERE SenderBindings existieren (z.B. eine
    urspruengliche ID + eine neue Gateway-ID). Genau einer ist mit
    active=True markiert — der wird fuer TX (Schalt-Befehle) benutzt.
    Alle anderen werden automatisch als Beobachter behandelt: wenn ein
    Telegramm mit dieser Sender-ID reinkommt, aktualisieren wir
    optimistisch den State des Aktors (z.B. weil ein paralleles System den FLD61 noch
    schaltet waehrend wir hier mitlaufen).
    """

    sender_id: str = Field(
        ...,
        description="32-bit Sender-ID als 8-Hex (z.B. 'FF810055')",
        pattern=r"^[0-9A-Fa-f]{8}$",
    )
    via_gateway: str | None = Field(
        None,
        description="Optional: Gateway-Name ueber den TX mit dieser ID lauft",
    )
    label: str = Field(
        "",
        description="Optionaler Name, z.B. 'Primary', 'Failover'",
    )
    active: bool = Field(
        False,
        description="True = wird fuer TX verwendet. Genau einer pro Channel sollte True sein.",
    )


class DeviceChannel(BaseModel):
    """Ein logischer Kanal eines Geräts (z.B. '2.1 Schaltaktor 0')."""

    channel_id: str = Field(..., description="z.B. '1', '2.1', 'A', frei wählbar pro Device")
    name: str = Field(..., description="Anzeige-Name, z.B. 'Couch Schaltaktor'")
    # M95: Override fuer Multi-Channel-Aktoren (z.B. DALI-Dimmer 16 Ch). Das
    # Device sagt, WO der Aktor sitzt; der Channel sagt, welchen RAUM er schaltet.
    # Leer = erbt floor/room vom Device. Treibt App-Gruppierung UND MQTT-Topic.
    floor: str = Field("", description="Override: Etage des geschalteten Raums. Leer = erbt vom Device.")
    room: str = Field("", description="Override: Raum den DIESER Channel schaltet. Leer = erbt vom Device.")
    # M103: Farbleuchte aus 2 Kanaelen. Zwei Channels mit gleicher light_group
    # bilden EINE Leuchte; light_role bestimmt, welcher Kanal Helligkeit bzw.
    # Farbe (Hue) steuert. Leer = eigenstaendiger Aktor.
    light_group: str = Field("", description="Gruppenschluessel: zwei Channels gleicher Gruppe = eine Farbleuchte.")
    light_role: str = Field("", description="Rolle in der Leuchten-Gruppe: '' | 'brightness' | 'color'.")
    enocean_id: str | None = Field(
        None,
        description="32-bit Sender-ID als 8-Hex (z.B. 'FF80009C'). None = nicht angelernt.",
        pattern=r"^[0-9A-Fa-f]{8}$",
    )
    eep: str = Field(..., description="EEP wie 'A5-12-01' oder 'F6-02-01'")
    direction: str = Field(
        "rx",
        description="rx=nur empfangen, tx=nur senden, bi=bidirektional",
        pattern=r"^(rx|tx|bi)$",
    )
    # Multi-Sender-Konzept (M59): Liste von Sender-Bindings. Wenn leer
    # aber learned_pair_id gesetzt, wird automatisch ein Eintrag generiert
    # (Migration alter devices.yaml). Genau einer hat active=True; alle
    # anderen werden wie Beobachter behandelt.
    senders: list[SenderBinding] = Field(
        default_factory=list,
        description="Sender-IDs + Gateways fuer TX. Genau einer hat active=True.",
    )
    # DEPRECATED — wird automatisch aus dem aktiven SenderBinding abgeleitet.
    # Beim Laden alter YAML-Dateien wird dieser Wert in senders[0] migriert.
    # Beim Save synchron gehalten mit dem aktiven SenderBinding, damit
    # Lese-Code-Pfade weiter funktionieren.
    via_gateway: str | None = Field(
        None,
        description="DEPRECATED: aus senders[active].via_gateway abgeleitet.",
    )
    learned_pair_id: str | None = Field(
        None,
        description="DEPRECATED: aus senders[active].sender_id abgeleitet.",
        pattern=r"^[0-9A-Fa-f]{8}$",
    )
    # Schalter->Aktor-Referenz: Wenn dieser Channel ein PTM-Schalter ist, gibt
    # er die Liste der Aktor-Channels an, die er steuert. Format pro Eintrag:
    # "device_id/channel_id". Bei Scaffold nicht immer angelegt; kann
    # automatisch ermittelt werden, wenn ein anderer Channel
    # learned_pair_id == enocean_id dieses Channels hat (siehe Registry).
    controls: list[str] = Field(
        default_factory=list,
        description="Geraete/Channels die dieser Schalter steuert (Format 'device_id/channel_id').",
    )
    # Beobachter-Eingang: Sender-IDs (PTM, Bewegungsmelder, Zeitschaltuhr, ...)
    # die diesen Aktor-Channel direkt steuern. Bei Empfang wird der State
    # optimistisch geupdated (Schalter->Aktor-Verknuepfung ohne Zentrale).
    # Jede ID ist ein 8-stelliger Hex-Wert.
    observers: list[ObserverBinding | str] = Field(
        default_factory=list,
        description=(
            "Sender-Bindings die diesen Aktor direkt steuern. "
            "String = legacy (match='any'), ObserverBinding mit Filter (rocker/event)."
        ),
    )
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_senders_and_legacy(self) -> "DeviceChannel":
        """
        Hält senders und die legacy-Felder (learned_pair_id, via_gateway)
        konsistent. M59 - Drei Schritte:

        1. Migration: senders leer + learned_pair_id da → senders[0] erzeugen
        2. Genau einen active=True garantieren
        3. Aktiven Sender in legacy-Felder spiegeln (für alte Code-Pfade)
        """
        # Schritt 1: Migration aus legacy
        if not self.senders and self.learned_pair_id:
            self.senders = [
                SenderBinding(
                    sender_id=self.learned_pair_id.upper(),
                    via_gateway=self.via_gateway,
                    active=True,
                )
            ]
        if not self.senders:
            return self
        # Schritt 2: Genau einen active=True
        active_idx = next(
            (i for i, s in enumerate(self.senders) if s.active), -1,
        )
        if active_idx < 0:
            self.senders[0].active = True
            active_idx = 0
        for i, s in enumerate(self.senders):
            if i != active_idx and s.active:
                s.active = False
        # Schritt 3: Spiegele aktiven Sender in legacy
        active = self.senders[active_idx]
        self.learned_pair_id = active.sender_id.upper()
        self.via_gateway = active.via_gateway
        return self

    @property
    def active_sender(self) -> "SenderBinding | None":
        """Den als TX-aktiv markierten SenderBinding (M59)."""
        if not self.senders:
            return None
        for s in self.senders:
            if s.active:
                return s
        return self.senders[0]  # Fallback: erster Sender

    @property
    def active_sender_id(self) -> str | None:
        """Sender-ID die fuer TX verwendet wird (uppercase)."""
        s = self.active_sender
        return s.sender_id.upper() if s else None

    @property
    def active_via_gateway(self) -> str | None:
        """Gateway ueber das mit der aktiven Sender-ID gesendet wird."""
        s = self.active_sender
        return s.via_gateway if s else None

    @property
    def inactive_sender_ids(self) -> list[str]:
        """
        Sender-IDs aller NICHT-aktiven SenderBindings (uppercase).
        Werden vom Pipeline-Code wie Beobachter behandelt — damit der State
        sich aktualisiert, wenn der Aktor von einer anderen Stelle (z.B.
        ein paralleles System waehrend der Migration) geschaltet wird.
        """
        active = self.active_sender
        active_id = active.sender_id.upper() if active else None
        return [
            s.sender_id.upper()
            for s in self.senders
            if s.sender_id.upper() != active_id
        ]


class Device(BaseModel):
    """Ein physisches Gerät, das aus 1..N Channels besteht."""

    device_id: str = Field(..., description="Slug, eindeutig, z.B. 'wz_rollo_links'")
    manufacturer: str = ""
    model: str = ""
    name: str = Field(..., description="Anzeige-Name")
    room: str = Field("", description="Hierarchisch, z.B. 'Beispielwohnung/Wohnzimmer'")
    floor: str = Field("", description="Top-Level der Raum-Hierarchie für GW-Zuweisung")
    channels: list[DeviceChannel] = Field(default_factory=list)
    notes: str = ""


class DeviceRegistry:
    """
    In-Memory-Registry mit Index nach Sender-ID. Lädt/speichert YAML.

    Wichtig: Pro Sender-ID kann es MEHRERE Channels geben — z.B. Eltako FWZ14
    sendet kWh + W unter derselben Sender-ID, wird aber als 2 logische Channels
    angelegt (mit meta.field-Filter).
    """

    def __init__(self, devices: list[Device] | None = None) -> None:
        self._devices: dict[str, Device] = {}
        # Multi-Index: ein Sender kann mehrere (Device, Channel)-Tupel haben
        self._by_sender_id: dict[str, list[tuple[Device, DeviceChannel]]] = {}
        # PTM-Tx-Index: Sende-PTM-ID die im Aktor angelernt ist (max 1 pro Channel)
        self._by_pair_id: dict[str, list[tuple[Device, DeviceChannel]]] = {}
        # Beobachter-Index: alle Sender-IDs die diesen Aktor optimistisch steuern.
        # Bei PTM-Press / Bewegungsmelder-Tele: alle Aktoren updaten die diesen
        # Sender als Beobachter haben.
        self._by_observer_id: dict[str, list[tuple[Device, DeviceChannel]]] = {}
        if devices:
            for d in devices:
                self.upsert(d)

    @classmethod
    def load(cls, path: str | Path) -> "DeviceRegistry":
        p = Path(path)
        if not p.exists():
            log.info("Keine devices.yaml gefunden unter %s — starte leer", p)
            return cls()
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        devices = [Device.model_validate(d) for d in raw.get("devices", [])]
        log.info("DeviceRegistry: %d Devices geladen", len(devices))
        return cls(devices)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"devices": [d.model_dump(mode="json") for d in self._devices.values()]}
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        tmp.replace(p)

    def _remove_from_index(self, device_id: str) -> None:
        """Entfernt alle Channel-Eintraege dieses Geraets aus den Indizes."""
        d = self._devices.get(device_id)
        if not d:
            return

        def _drop(idx: dict[str, list], key: str) -> None:
            lst = idx.get(key, [])
            idx[key] = [(dv, c) for (dv, c) in lst if dv.device_id != device_id]
            if not idx[key]:
                del idx[key]

        for ch in d.channels:
            if ch.enocean_id:
                _drop(self._by_sender_id, ch.enocean_id.upper())
            # Aktiver Sender (M59): indexiert als Tx-PTM
            active_sid = ch.active_sender_id
            if active_sid:
                _drop(self._by_pair_id, active_sid)
            # Inaktive Sender + klassische Observers: alle als Beobachter
            for sid in ch.inactive_sender_ids:
                _drop(self._by_observer_id, sid)
            for obs in (ch.observers or []):
                obs_sid, _ = _normalize_observer(obs)
                if obs_sid:
                    _drop(self._by_observer_id, obs_sid)

    def upsert(self, device: Device) -> None:
        if device.device_id in self._devices:
            self._remove_from_index(device.device_id)
        self._devices[device.device_id] = device
        for ch in device.channels:
            if ch.enocean_id:
                self._by_sender_id.setdefault(ch.enocean_id.upper(), []).append((device, ch))
            # M59: aktiver Sender ist die einzige TX-Quelle
            active_sid = ch.active_sender_id
            if active_sid:
                self._by_pair_id.setdefault(active_sid, []).append((device, ch))
            # Inaktive Sender werden automatisch wie Beobachter behandelt —
            # damit Aktor-State sich auch aktualisiert wenn er von ausserhalb
            # geschaltet wird (z.B. ein paralleles System waehrend der Migration).
            for sid in ch.inactive_sender_ids:
                self._by_observer_id.setdefault(sid, []).append((device, ch))
            for obs in (ch.observers or []):
                obs_sid, _ = _normalize_observer(obs)
                if obs_sid:
                    self._by_observer_id.setdefault(obs_sid, []).append((device, ch))

    def remove(self, device_id: str) -> bool:
        if device_id not in self._devices:
            return False
        self._remove_from_index(device_id)
        del self._devices[device_id]
        return True

    def lookup_by_sender_id(self, sender_id_hex: str) -> tuple[Device, DeviceChannel] | None:
        """Erstes Match — Legacy-API, fuer Single-Channel-Geraete."""
        lst = self._by_sender_id.get(sender_id_hex.upper())
        return lst[0] if lst else None

    def lookup_all_by_sender_id(self, sender_id_hex: str) -> list[tuple[Device, DeviceChannel]]:
        """Alle (Device, Channel)-Paare zu einer Sender-ID — fuer Multi-Channel-Devices."""
        return list(self._by_sender_id.get(sender_id_hex.upper(), []))

    def lookup_actors_by_ptm(
        self, ptm_sender_id_hex: str
    ) -> list[tuple[Device, DeviceChannel]]:
        """
        Alle Aktor-Channels die diese Sender-ID als Sende-PTM (Tx) gelernt haben.
        """
        return list(self._by_pair_id.get(ptm_sender_id_hex.upper(), []))

    def lookup_actors_observing(
        self, sender_id_hex: str
    ) -> list[tuple[Device, DeviceChannel]]:
        """
        Alle Aktor-Channels die diese Sender-ID als Beobachter (Rx) haben.
        Beobachter sind Wand-PTMs, Bewegungsmelder, Zeitschaltuhren, etc.
        die direkt im Aktor angelernt sind und ihn auch ohne Zentrale steuern.
        """
        return list(self._by_observer_id.get(sender_id_hex.upper(), []))

    def all(self) -> list[Device]:
        return list(self._devices.values())

    def __len__(self) -> int:
        return len(self._devices)
