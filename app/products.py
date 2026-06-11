"""
Geraete-Datenbank: Hersteller + Modell -> EEP-Profil + Konfig.

Quellen:
- app/data/products.yaml (im Image enthalten)
- /data/products_custom.yaml (User-Erweiterungen im Volume)

Format products.yaml:
    meta:
      description: ...
      count: 630
    products:
      - manufacturer: Eltako
        model: FSR14-2x
        com_profile: A5-38-08(01)
        device_type: Switch_2
        config: CommandEEP=EEP_A53808_01_CentralCommand_Switching;ReceiveEEP=...
        description: 2-Kanal Schaltaktor
        order_nr: "..."

Lookup ist toleranter Match (Modellname mit/ohne Suffix wie "(ab 41/11)").
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ProductInfo:
    manufacturer: str
    model: str
    eep: str | None = None
    device_type: str | None = None
    config_str: str | None = None
    description: str | None = None
    order_nr: str | None = None
    # Zusatz-Sub-Channels jenseits der channel_count-Sequenz. Beispiel: Eltako
    # DSZ14DRS-3x65A hat channel_count=2 (Doppeltarif 0/1), sendet aber
    # zusaetzlich channel=8 als Gesamt-Wirkleistung ueber alle 3 Phasen.
    # Format: Liste von Dicts mit "channel" (tele-channel-Index) und optional
    # "label" (Name-Praefix fuer den UI-Channel).
    #   extra_telegram_channels:
    #     - channel: 8
    #       label: "Gesamt"
    extra_telegram_channels: list[dict] = field(default_factory=list)
    # Whitelist auf topic_split_fields des EEP-Profils. Wenn gesetzt, werden
    # NUR diese Felder als Channels generiert; andere Felder des EEP werden
    # ignoriert. Beispiel: OPUS RTS55 (ohne SP/Slider) hat zwar EEP A5-10-03,
    # nutzt aber nur die Temperatur-Information — Sollwert-Channel waere
    # immer 0%. Mit `included_fields: [temperature_c]` wird nur der
    # Temperatur-Channel angelegt. Leere Liste bedeutet "alle Felder".
    included_fields: list[str] = field(default_factory=list)
    # CSV-Import-Verhalten:
    #   "auto" (Default) -> normal als Device + Channels importieren
    #   "skip"           -> beim CSV-Import komplett ueberspringen; das Modell
    #                       bleibt in der DB fuer spaetere TX-Konfiguration
    #                       (z.B. reine Aktoren wie Thermokon SRC-DO8 die
    #                       keine Status-Telegramme senden, also als RX-Channel
    #                       keinen Sinn machen).
    csv_import: str = "auto"
    # Wie werden bei einem Multi-Channel-Geraet (channel_count > 1) die Sub-
    # Kanaele unterschieden?
    #   "tariff_byte"  -> 1 Sender-ID, DB0-Bit-Feld als Sub-Selector (z.B.
    #                     DSZ14DRS Doppeltarif via DB0-Bit4).
    #                     Importer erzeugt N*M Channels auto, alle mit gleicher
    #                     enocean_id und unterschiedlichem tele_channel.
    #   "separate_ids" -> N separate Sender-IDs, je eine pro Sub-Zaehler (z.B.
    #                     F3Z14D 3-Eingang). Importer macht KEINE Auto-Gen
    #                     der Sub-Channels; stattdessen werden die CSV-Channel-
    #                     Zeilen mit eigener Sender-ID zu Channels.
    #   ""             -> Default (keine Markierung). Aktuelles Verhalten —
    #                     wenn telegram_channel_field am EEP gesetzt ist, wird
    #                     tariff_byte angenommen.
    multi_channel_kind: str = ""

    # M65: kurze Anleitung wie der Aktor in den Anlern-Modus zu versetzen ist —
    # wird in der UI direkt im Sender-Block angezeigt, bevor der User auf 📡
    # klickt. Pro Modell konfigurierbar in products.yaml.
    teach_in_procedure: str = ""

    # M72: True wenn das Modul ein FAM14-Bus-Modul ist (DIN-Schienen-Aktor mit
    # RS485-Bus, der seine Sender-IDs aus dem FAM14-Block bezieht). Bei diesen
    # Modulen kann der Add-Dialog die RX-IDs aus PCT14-Bus-Adresse + FAM14-Base
    # auto-generieren.
    # Betrifft: FDG14, F3Z14D, FSR14-*, FUD14, FWZ14-*, DSZ14DRS-*, FSB14,
    # FAE14, FMS14, F4HK14, FSDG14, FSS12, FTS14EM ...
    fam14_bus: bool = False

    # M79: Manche Produkte sind physisch ein Gehaeuse mit zwei unabhaengigen
    # Funkteilen — z.B. OPUS Bridge UP-eSchalter (1 VLD-Aktor + 2-fach PTM-
    # Doppelwippe). Wir bilden das im Channel-Modell ab: nach den Aktor-
    # Channels werden bundled_ptm_count weitere Channels mit bundled_ptm_eep
    # generiert (rx-only). Der User traegt die echten PTM-Sender-IDs händisch
    # nach (nur der Installateur weiss welcher PTM physisch wo eingelernt ist).
    bundled_ptm_count: int = 0
    bundled_ptm_eep: str = "F6-02-01"

    # RX-only-Sende-Aktor (z.B. Thermokon STC-DO8 Heizregler): das Geraet
    # gibt KEINEN Status, wird aber von uns BESENDET. Scaffold legt dann
    # channel_count schlichte TX-Kanaele an (eep=com_profile, direction=tx,
    # leere Sender — User traegt die TX-IDs manuell ein). KEINE Feld-Auf-
    # teilung (kein temp/setpoint-Sub-Split wie bei RX-Sensoren).
    actuator_tx: bool = False

    # M77: Encoding-Konvention fuer PCT14-Bus-Adresse → Sender-ID-Endung.
    # Eltako-FAM14 verwendet einheitlich HEX-direct (live verifiziert mit
    # 5 Modul-Typen am realen System):
    #   FDG14    Adr 10 → 0x0A → FF80008A
    #   DSZ14DRS Adr 26 → 0x1A → FF80009A
    #   FWZ14    Adr 27 → 0x1B → FF80009B
    #   FSR14    Adr 28 → 0x1C → FF80009C  (Pool-Pumpe)
    #   FSR14    Adr 51 → 0x33 → FF8000B3  (Couch)
    #
    # M76 BCD-Hypothese (basierend auf falscher Live-Log-Interpretation)
    # wurde mit M77 verworfen. "bcd" bleibt als Library-Option fuer evtl.
    # andere Hersteller — Default ist "hex".
    fam14_addressing: str = "hex"

    @property
    def channel_count(self) -> int:
        """
        Liest die Kanal-Anzahl aus dem device_type.

        Konvention:
          "Dimmer_16" -> 16 Kanaele
          "Schalter_4" / "Switch_4" -> 4 Kanaele
          "Jalousie_2" / "Shutter_2" -> 2 Kanaele
          "EnergyMeter_Bi" -> 2 Tarife (Doppeltarif HT/NT)
          "Dimmer" / "Switch" (ohne Suffix) -> 1 Kanal
        """
        if not self.device_type:
            return 1
        if self.device_type.lower().endswith("_bi"):
            return 2  # Bi-Tariff (HT/NT)
        m = re.search(r"_(\d+)$", self.device_type)
        if m:
            n = int(m.group(1))
            return max(1, min(64, n))  # sanity-clamp
        return 1

    @property
    def ui_kind(self) -> str:
        """Hint fuer UI-Klassifikation (rx/switch/dimmer/shutter/valve)."""
        dt = (self.device_type or "").lower()
        if "jalousie" in dt or "shutter" in dt or "rollade" in dt:
            return "shutter"
        if "dimmer" in dt:
            return "dimmer"
        if "switch" in dt or "schalter" in dt:
            return "switch"
        if "valve" in dt or "heating" in dt or "ventil" in dt:
            return "valve"
        return "rx"

    @property
    def channel_name_template(self) -> str:
        """
        Naming-Pattern fuer die n auto-generierten Channels.
        '{idx}' wird durch 1..n ersetzt, '{idx0}' durch 0..n-1.
        """
        dt = (self.device_type or "").lower()
        if dt.endswith("_bi"):
            # Eltako-Konvention A5-12-01 DB0 bit 4:
            #   tariff=0 -> Normaltarif (Hochtarif/HT)
            #   tariff=1 -> Nachttarif  (Niedrigtarif/NT)
            return "Tarif {idx0}"
        if "dimmer" in dt:
            return "Dimmer {idx0}"
        if "shutter" in dt or "jalousie" in dt or "rollade" in dt:
            return "Rolladen {idx}"
        if "switch" in dt or "schalter" in dt:
            return "Schaltaktor {idx0}"
        if "valve" in dt or "ventil" in dt or "heating" in dt:
            return "Heizkreis {idx}"
        if "energymeter" in dt:
            return "Phase {idx}"
        return "Kanal {idx}"


# ---------------------------------------------------------------------------
# Toleranter Modell-Match
# ---------------------------------------------------------------------------


def _modelname_variants(model: str) -> list[str]:
    """
    Erzeugt mehrere Schreibweisen eines Modellnamens fuer DB-Lookup.

    Beispiel:
        "FSB61NP (ab 41/11)" -> [
            "FSB61NP (ab 41/11)",
            "FSB61NP",
            "FSB61NP ($ab 41/11)"
        ]
    """
    variants = [model]
    # Suffix "(ab XX/YY)" abschneiden
    stripped = re.sub(r"\s*\(ab\s+\d+/\d+\)", "", model).strip()
    if stripped != model:
        variants.append(stripped)
    # Dollar-Variante (manche Exporte nutzen manchmal "$ab" statt "ab")
    if "(ab " in model:
        variants.append(model.replace("(ab ", "($ab "))
    # "(frueher)" / "(früher)"
    if "(frueher" in model.lower() or "(früher" in model.lower():
        variants.append(
            re.sub(r"\(fr[uü]her\)", "($frueher)", model, flags=re.IGNORECASE)
        )
    # Ohne Klammerausdruck
    stripped2 = re.sub(r"\s*\([^)]*\)", "", model).strip()
    if stripped2 not in variants:
        variants.append(stripped2)
    return variants


def _manufacturer_variants(mfr: str) -> list[str]:
    """Toleranter Match fuer Hersteller-Bezeichnungen."""
    variants = {mfr, mfr.lower()}
    if "ä" in mfr:
        variants.add(mfr.replace("ä", "ae"))
    if "Jäger" in mfr or "Jaeger" in mfr:
        variants.update({"Jäger Direkt - OPUS", "Jaeger Direkt - OPUS"})
    return [v for v in variants if v]


def _normalize_eep(raw: str | None) -> str | None:
    """A5-38-08(02) -> A5-38-08 (Sub-Variante separat in config_str)."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    base = re.sub(r"\s*\(\d+\)\s*$", "", raw)
    return base


# ---------------------------------------------------------------------------
# ProductDB
# ---------------------------------------------------------------------------


class ProductDB:
    """In-Memory Lookup auf Hersteller+Modell -> ProductInfo."""

    def __init__(self) -> None:
        # Index: (manufacturer_lower, model_lower) -> Liste von ProductInfo.
        # Ein Modell kann MEHRERE Funktions-Zeilen haben (Multi-Funktions-
        # Geraete wie Thermokon thanos: Sensor + Magnetkontakt + Tasten, oder
        # SRC-ADO BCS: Valve + Jalousie + Dimmer + Switch). Die erste Zeile ist
        # die Primaerfunktion (lookup()); functions() liefert alle.
        # Frueher war das ein Single-Value-Dict -> alle Funktionen ausser der
        # letzten gingen verloren ("DB-Kollaps").
        self._index: dict[tuple[str, str], list[ProductInfo]] = {}
        # Public stats
        self.count_builtin = 0
        self.count_custom = 0

    @classmethod
    def from_yaml_files(
        cls,
        builtin_path: str | Path | None = None,
        custom_path: str | Path | None = None,
    ) -> "ProductDB":
        """
        Laedt builtin + optional custom YAML.

        Custom-Eintraege ueberschreiben builtin (wenn gleicher Hersteller+Modell).
        """
        db = cls()
        if builtin_path and Path(builtin_path).exists():
            n = db._load_yaml(builtin_path, source="builtin")
            db.count_builtin = n
            log.info("ProductDB: %d Geraete aus builtin %s", n, builtin_path)
        if custom_path and Path(custom_path).exists():
            n = db._load_yaml(custom_path, source="custom")
            db.count_custom = n
            log.info("ProductDB: %d Geraete aus custom %s", n, custom_path)
        return db

    @classmethod
    def load_default(cls) -> "ProductDB":
        """Laedt builtin (im Image) + custom (/data/products_custom.yaml)."""
        builtin = Path(__file__).parent / "data" / "products.yaml"
        config_dir = Path(os.environ.get("CONFIG_DIR", "/data"))
        custom = config_dir / "products_custom.yaml"
        return cls.from_yaml_files(builtin, custom)

    def _load_yaml(self, path: str | Path, source: str = "?") -> int:
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        products = doc.get("products", []) or []
        # Custom-Override-Semantik: die erste Custom-Zeile pro Modell ERSETZT
        # die komplette Builtin-Funktionsliste dieses Modells; weitere Custom-
        # Zeilen desselben Modells werden angehaengt (= Custom definiert die
        # Funktionen voll). Builtin-Zeilen werden immer angehaengt (Multi-
        # Funktion). So bleibt "custom overridet builtin" erhalten, ohne dass
        # mehrere Builtin-Funktionen einer gleichen Modell-ID kollabieren.
        replaced_keys: set[tuple[str, str]] = set()
        for raw in products:
            if not isinstance(raw, dict):
                continue
            mfr = (raw.get("manufacturer") or "").strip()
            model = (raw.get("model") or "").strip()
            if not mfr or not model:
                continue
            key = (mfr.lower(), model.lower())
            if source == "custom" and key not in replaced_keys:
                self._index[key] = []
                replaced_keys.add(key)
            info = ProductInfo(
                manufacturer=mfr,
                model=model,
                eep=_normalize_eep(raw.get("com_profile") or raw.get("eep")),
                device_type=raw.get("device_type"),
                config_str=raw.get("config") or raw.get("config_str"),
                description=raw.get("description"),
                order_nr=raw.get("order_nr"),
                extra_telegram_channels=list(raw.get("extra_telegram_channels") or []),
                included_fields=list(raw.get("included_fields") or []),
                csv_import=str(raw.get("csv_import") or "auto"),
                multi_channel_kind=str(raw.get("multi_channel_kind") or ""),
                teach_in_procedure=str(raw.get("teach_in_procedure") or ""),
                fam14_bus=bool(raw.get("fam14_bus", False)),
                fam14_addressing=str(raw.get("fam14_addressing") or "hex").lower(),
                bundled_ptm_count=int(raw.get("bundled_ptm_count") or 0),
                bundled_ptm_eep=str(raw.get("bundled_ptm_eep") or "F6-02-01"),
                actuator_tx=bool(raw.get("actuator_tx", False)),
            )
            # Exakte Dubletten (gleiches eep+device_type+config) NICHT als
            # zusaetzliche "Funktion" zaehlen — sonst erzeugt der Scaffold
            # Phantom-Kanaele (z.B. "STC-DO8 type2" stand 2x identisch drin
            # und ergab 2 statt 1 Kanal). Echte Multi-Funktion (Thanos:
            # Kontakt+Sensor+Tasten) bleibt erhalten, weil die Signatur dort
            # je Zeile unterschiedlich ist.
            bucket = self._index.setdefault(key, [])
            sig = (info.eep, info.device_type, info.config_str)
            if any((e.eep, e.device_type, e.config_str) == sig for e in bucket):
                continue
            bucket.append(info)
        return len(products)

    def lookup(self, manufacturer: str, model: str) -> ProductInfo | None:
        """
        Toleranter Match auf Hersteller+Modell. Liefert die PRIMAERfunktion
        (erste Zeile in der DB). Fuer alle Funktionen eines Multi-Funktions-
        Geraets siehe functions().
        """
        for mfr in _manufacturer_variants(manufacturer):
            for m in _modelname_variants(model):
                hit = self._index.get((mfr.lower(), m.lower()))
                if hit:
                    return hit[0]
        return None

    def functions(self, manufacturer: str, model: str) -> list[ProductInfo]:
        """
        Alle Funktions-Zeilen eines Modells (Multi-Funktions-Geraete wie der
        thanos: Sensor + Magnetkontakt + Tasten). Die erste ist die
        Primaerfunktion (== lookup()). Leere Liste wenn unbekannt.
        """
        for mfr in _manufacturer_variants(manufacturer):
            for m in _modelname_variants(model):
                hit = self._index.get((mfr.lower(), m.lower()))
                if hit:
                    return list(hit)
        return []

    def all_products(self) -> list[ProductInfo]:
        """Primaerfunktion je Modell (eine pro Hersteller+Modell)."""
        return [rows[0] for rows in self._index.values() if rows]

    def manufacturers(self) -> list[str]:
        """Eindeutige Hersteller-Liste, sortiert."""
        seen: set[str] = set()
        out: list[str] = []
        for rows in self._index.values():
            info = rows[0]
            if info.manufacturer not in seen:
                seen.add(info.manufacturer)
                out.append(info.manufacturer)
        return sorted(out)

    def models_for(self, manufacturer: str) -> list[ProductInfo]:
        """Alle Modelle eines Herstellers (Primaerfunktion je Modell), sortiert."""
        out = [
            rows[0]
            for rows in self._index.values()
            if rows and rows[0].manufacturer.lower() == manufacturer.lower()
        ]
        out.sort(key=lambda p: p.model.lower())
        return out

    def __len__(self) -> int:
        return len(self._index)
