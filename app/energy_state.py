"""
Letzte gueltige Zaehlerstaende — ueber Container-Neustarts hinweg.

Warum es das gibt: Der Monotonie-Schutz hielt den letzten
Stand nur im Arbeitsspeicher. Bei jedem Neustart — also nach jedem Deploy — war
er weg, und der erste eintreffende Wert wurde bedingungslos als neue Referenz
uebernommen. Kam dabei ein 0-Wert oder ein Sub-Tarif-Stand zuerst, sprang der
Zaehler in der Visualisierung von 0 auf den echten Stand hoch — und die
Tagesstatistik rechnete den kompletten Zaehlerstand als Tagesverbrauch.
Genau so kamen 203.942 kWh "Tagesverbrauch" zustande.

Mit dieser Datei ueberlebt der Stand den Neustart: Der Schutz greift lueckenlos,
ein 0-Wert nach dem Neustart ist ein Rueckwaertssprung und wird verworfen.

Geschrieben wird gebuendelt (nicht bei jedem Telegramm) — der Timberwolf
schreibt auf eine SD-Karte, und Zaehler senden im Minutentakt.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class EnergyStateStore:
    """Haelt {(sender_id, tariff, feld): (wert, zeitpunkt)} und sichert es."""

    def __init__(self, path: str | Path, save_interval_s: float = 30.0) -> None:
        self.path = Path(path)
        self.save_interval_s = save_interval_s
        self._werte: dict[tuple[str, int, str], float] = {}
        self._zeiten: dict[tuple[str, int, str], float] = {}
        self._dirty = False
        self._last_save = 0.0

    # ---------------------------------------------------------------- Zugriff

    def get(self, key: tuple[str, int, str]) -> float | None:
        return self._werte.get(key)

    def get_ts(self, key: tuple[str, int, str]) -> float | None:
        return self._zeiten.get(key)

    def set(self, key: tuple[str, int, str], wert: float, ts: float | None = None) -> None:
        self._werte[key] = float(wert)
        self._zeiten[key] = float(ts if ts is not None else time.time())
        self._dirty = True

    def __contains__(self, key: tuple[str, int, str]) -> bool:
        return key in self._werte

    def __len__(self) -> int:
        return len(self._werte)

    # ------------------------------------------------------------- Persistenz

    @staticmethod
    def _key_to_str(key: tuple[str, int, str]) -> str:
        sender, tariff, feld = key
        return f"{sender}|{tariff}|{feld}"

    @staticmethod
    def _str_to_key(text: str) -> tuple[str, int, str] | None:
        teile = text.split("|")
        if len(teile) != 3:
            return None
        try:
            return (teile[0], int(teile[1]), teile[2])
        except ValueError:
            return None

    def load(self) -> None:
        if not self.path.exists():
            log.info("Kein gespeicherter Zaehlerstand (%s) — beim ersten "
                     "Telegramm je Zaehler wird neu begonnen.", self.path)
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                doc: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception as exc:
            log.warning("Zaehlerstaende nicht lesbar (%s): %s — es wird neu "
                        "begonnen.", self.path, exc)
            return

        for text, eintrag in (doc.get("zaehler") or {}).items():
            key = self._str_to_key(text)
            if key is None or not isinstance(eintrag, dict):
                continue
            wert = eintrag.get("wert")
            if not isinstance(wert, (int, float)):
                continue
            self._werte[key] = float(wert)
            ts = eintrag.get("ts")
            if isinstance(ts, (int, float)):
                self._zeiten[key] = float(ts)

        log.info("Zaehlerstaende geladen: %d Eintraege aus %s — der "
                 "Monotonie-Schutz greift ab dem ersten Telegramm.",
                 len(self._werte), self.path)

    def save(self) -> None:
        doc = {
            "zaehler": {
                self._key_to_str(k): {
                    "wert": round(v, 3),
                    "ts": round(self._zeiten.get(k, 0.0), 1),
                }
                for k, v in sorted(self._werte.items())
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
            tmp.replace(self.path)
            self._dirty = False
            self._last_save = time.time()
        except Exception as exc:
            log.warning("Zaehlerstaende konnten nicht gesichert werden (%s): %s",
                        self.path, exc)

    def save_if_due(self, now: float | None = None) -> bool:
        """Speichert hoechstens alle save_interval_s Sekunden. True = gespeichert."""
        if not self._dirty:
            return False
        jetzt = now if now is not None else time.time()
        if jetzt - self._last_save < self.save_interval_s:
            return False
        self.save()
        return True
