"""
EEPProfile — die Single Source of Truth pro EEP.

Bisher war EEP-Wissen ueber mehrere Module verstreut:
  - decoder         in app/eep/profiles.py
  - encoder         in app/encoders.py
  - splits          in app/import_csv.py (_multi_channel_definition)
  - ui_kind         in app/tx_router.py (_kind) und app/webui app.js (channelKind)
  - format-Hints    in app.js (formatDecoded mit if/else)

Hier konsolidiert: pro EEP-Id ein Profile-Objekt mit allen Eigenschaften.
Aenderung am EEP -> nur dieses Objekt anfassen.

Konvention:
  - decoded-dict liefert die rohen Felder (decoded[field.name] = value)
  - publish: pro topic_split_field ein eigenes MQTT-Topic mit {"value": ...}
  - UI: nimmt die FieldDefs fuer Label/Einheit/Icon, NICHT im Payload
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# Decoder-Signatur: (payload_bytes, status_byte, rorg_byte) -> dict
DecoderFn = Callable[[bytes, int, int], dict]

# Encoder-Signatur: (sender_id_int, command_dict) -> CommandFrame
# (Import vermieden um Zyklen zu sparen; Type-Hint nur Doku)
EncoderFn = Callable[..., Any]


FieldKind = Literal["number", "bool", "string", "enum"]
UIKind = Literal["rx", "switch", "dimmer", "shutter", "valve"]


@dataclass
class FieldDef:
    """
    Definition eines einzelnen Werts im decoded telegram.

    name             matcht den Key im decoded-dict (z.B. "current_w").
    label/unit/icon  werden NUR in der UI gezeigt — nicht in den MQTT-Payload.
    is_topic_split   True => bekommt eigenes MQTT-Topic, False => nur informativ.
    enum_labels      bei kind="bool"/"enum": Wert -> Anzeigetext, fuer formatDecoded.
    """

    name: str
    label: str
    unit: str = ""
    icon: str = ""
    kind: FieldKind = "number"
    decimals: Optional[int] = None
    is_topic_split: bool = True
    enum_labels: dict = field(default_factory=dict)
    # Bei Multi-Sub-Channel-Geraeten (z.B. DSZ14DRS Doppeltarif): wenn True,
    # wird dieses Feld NICHT pro Tarif verdoppelt, sondern nur 1x pro Device
    # angelegt. Beispiel: Seriennummer ist geraet-weit, nicht tarif-spezifisch.
    tariff_independent: bool = False
    # Trennung von "eigener MQTT-Sub-Topic" und "eigener UI-Channel im
    # Importer". Default = True (= alte Semantik). False fuer Felder die
    # zwar als MQTT-Sub-Topic published werden sollen (Logik-Bauer
    # subscriben), aber NICHT zu einem eigenen UI-Channel werden duerfen
    # (z.B. F6-02-01 press_duration_ms + last_press_event — Helper-Daten
    # am gleichen Telegramm, kein eigenes Sub-Geraet).
    creates_subchannel: bool = True

    def topic_segment(self) -> str:
        """Channel-ID-Segment im Topic (slugified-safe)."""
        return self.name.lower()

    def to_json(self) -> dict[str, Any]:
        """Frontend-tauglich serialisieren."""
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "unit": self.unit,
            "icon": self.icon,
            "kind": self.kind,
            "is_topic_split": self.is_topic_split,
        }
        if self.decimals is not None:
            out["decimals"] = self.decimals
        if self.enum_labels:
            # Bool/Enum enum_labels: stringify keys fuer JSON
            out["enum_labels"] = {str(k): v for k, v in self.enum_labels.items()}
        return out


@dataclass
class EEPProfile:
    """
    Vollstaendige Beschreibung eines EEP — Decoder + Encoder + UI-Felder.

    Felder die bei UI-Klassifikation helfen:
      ui_kind          "rx" (nur empfangen), "switch", "dimmer", "shutter", "valve"
      model_patterns   Substring-Liste fuer Fallback wenn EEP in DB nicht gepflegt
                       (z.B. FWS61 hat im Produktkatalog keinen EEP, aber Modellname
                       "FWS61" matcht hier auf A5-13-01)

    Felder die bei Encoding helfen:
      encoder          Callable(sender_id_int, command_dict) -> CommandFrame
      command_keys     Liste der akzeptierten command-keys (fuer UI-Schema)
    """

    eep_id: str
    name: str
    description: str = ""
    rorg: int = 0
    decoder: Optional[DecoderFn] = None
    encoder: Optional[EncoderFn] = None
    ui_kind: UIKind = "rx"
    fields: list[FieldDef] = field(default_factory=list)
    model_patterns: list[str] = field(default_factory=list)
    command_keys: list[str] = field(default_factory=list)
    # Optional: Name eines decoded-Felds das den Sub-Channel identifiziert.
    # Beispiel: A5-12-01 liefert "channel" (0..15) im DB0-Byte. Geraete wie
    # F3Z14D senden mehrere Sub-Kanaele unter EINER Sender-ID, jeder per
    # channel-Byte unterschieden. Wenn gesetzt UND product.channel_count > 1,
    # erzeugt der Importer N × topic_split_fields Channels mit
    # meta.tele_channel + meta.field, und die Pipeline filtert auf beide.
    telegram_channel_field: Optional[str] = None

    def get_field(self, name: str) -> Optional[FieldDef]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def topic_split_fields(self) -> list[FieldDef]:
        """Felder die einen eigenen UI-Sub-Channel + MQTT-Sub-Topic bekommen.
        Nur Felder mit is_topic_split=True UND creates_subchannel=True
        werden vom CSV-Importer als eigene Channels angelegt. Felder die
        zwar als MQTT-Sub-Topic published werden sollen, aber KEINEN
        eigenen UI-Channel rechtfertigen, setzen creates_subchannel=False."""
        return [
            f for f in self.fields
            if f.is_topic_split and f.creates_subchannel
        ]

    def needs_multi_channel(self) -> bool:
        """Hat dieser EEP mehr als 1 splittbares Feld?"""
        return len(self.topic_split_fields()) > 1

    def to_json(self) -> dict[str, Any]:
        return {
            "eep_id": self.eep_id,
            "name": self.name,
            "description": self.description,
            "ui_kind": self.ui_kind,
            "fields": [f.to_json() for f in self.fields],
            "model_patterns": self.model_patterns,
            "command_keys": self.command_keys,
            "rorg": f"0x{self.rorg:02X}" if self.rorg else "",
            "needs_multi_channel": self.needs_multi_channel(),
        }


class EEPProfileRegistry:
    """
    Sammelt alle bekannten EEPProfiles. Lookup nach EEP-Id oder Modellname.
    """

    def __init__(self) -> None:
        self._by_eep: dict[str, EEPProfile] = {}

    def register(self, profile: EEPProfile) -> None:
        self._by_eep[profile.eep_id.upper()] = profile

    def get(self, eep_id: Optional[str]) -> Optional[EEPProfile]:
        if not eep_id:
            return None
        return self._by_eep.get(eep_id.upper())

    def match_by_model(self, model: Optional[str]) -> Optional[EEPProfile]:
        """
        Fallback wenn das EEP nicht aus der ProductDB kommt:
        such einen Profile, dessen model_patterns auf den Modellnamen matchen.
        """
        if not model:
            return None
        m = model.upper()
        for p in self._by_eep.values():
            for pattern in p.model_patterns:
                if pattern.upper() in m:
                    return p
        return None

    def all(self) -> list[EEPProfile]:
        return list(self._by_eep.values())

    def __len__(self) -> int:
        return len(self._by_eep)
