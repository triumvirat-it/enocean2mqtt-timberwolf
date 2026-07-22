"""
Voll-Mitschnitt ALLER empfangenen Telegramme (telegram_audit.log).

Zweck: Wenn Wochen spaeter ein unplausibler Wert auffaellt (z.B. ein Gas-
Durchfluss von 524288, der aus einem einzelnen gestoerten Funktelegramm kam),
soll das ausloesende Roh-Telegramm noch nachschlagbar sein — der Live-Log im
RAM (500 Eintraege) reicht dafuer nicht.

Eigenschaften, zugeschnitten auf den knappen Timberwolf/SD-Karten-Speicher:
  * Eine kompakte Zeile pro Telegramm mit Roh-Payload (hex) + dekodierten Feldern.
  * GEBUENDELTE Schreibvorgaenge: record() legt die Zeile nur in einen RAM-Puffer;
    ein asyncio-Task schreibt alle paar Sekunden gesammelt raus (schont die
    SD-Karte, haelt den Async-Empfangspfad frei) — dieselbe Idee wie bei
    observed_senders.yaml.
  * Der eigentliche Datei-IO (inkl. gzip beim Tageswechsel) laeuft in einem
    Worker-Thread (asyncio.to_thread), damit der Event-Loop nie blockiert.
  * TAEGLICHE Rotation: abgeschlossene Tage werden zu telegram_audit.<datum>.log.gz
    komprimiert, Archive aelter als retention_days automatisch geloescht.
    Default 45 Tage (~6,5 Wochen). Roh ~5 MB/Tag, komprimiert ~0,5 MB/Tag →
    Gesamt-Fussabdruck ~25-30 MB.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("enocean2mqtt")

# Interne Decoder-Keys (fuehrender _) tauchen nicht im Protokoll auf.
def _clean(decoded: dict | None) -> dict:
    if not decoded:
        return {}
    return {k: v for k, v in decoded.items() if not str(k).startswith("_")}


class TelegramAuditWriter:
    def __init__(
        self,
        path: str | Path,
        enabled: bool = True,
        retention_days: int = 45,
        flush_interval: float = 5.0,
    ) -> None:
        self.path = Path(path)                 # .../telegram_audit.log (aktiver Tag)
        self.enabled = bool(enabled)
        self.retention_days = int(retention_days)
        self.flush_interval = float(flush_interval)
        self._buf: list[str] = []
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ---- Hot-Path: nur RAM, kein IO -------------------------------------
    def record(self, rx, decoded: dict | None, device_name: str | None = None) -> None:
        """Formatiert eine Zeile und legt sie in den Puffer. Wird pro Telegramm
        aus der Pipeline aufgerufen — darf niemals werfen und macht kein IO."""
        if not self.enabled:
            return
        try:
            self._buf.append(self.format_line(rx, decoded, device_name))
        except Exception:  # noqa: BLE001 — Protokoll darf den Empfang nie stoeren
            pass

    @staticmethod
    def format_line(rx, decoded: dict | None, device_name: str | None = None) -> str:
        t = rx.telegram
        ts_full = datetime.fromtimestamp(rx.received_at)
        # Sekunden + Hundertstel (reicht zur Zuordnung, spart Platz)
        ts = ts_full.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts_full.microsecond // 10000:02d}"
        gw = rx.gateway_name or "-"
        raw = t.payload.hex().upper()
        dev = f'"{device_name}"' if device_name else "-"
        dec = json.dumps(_clean(decoded), ensure_ascii=False, separators=(",", ":"))
        return (
            f"{ts} gw={gw} id={t.sender_id_hex} {t.rorg_name} "
            f"rssi={t.rssi_dbm} raw={raw} dev={dev} {dec}"
        )

    # ---- Lifecycle ------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            log.info("telegram_audit: deaktiviert (defaults.telegram_audit_enabled=false)")
            return
        # Falls die aktive Datei noch von einem frueheren Tag stammt (App war
        # ueber Mitternacht aus), erst archivieren, bevor neue Zeilen reinlaufen.
        with suppress(Exception):
            await asyncio.to_thread(self._rollover_if_stale)
        self._task = asyncio.create_task(self._run(), name="telegram_audit")
        log.info(
            "telegram_audit: schreibe nach %s (taeglich rotierend, %d Tage)",
            self.path, self.retention_days,
        )

    async def _run(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if not self._buf:
            return
        lines = self._buf
        self._buf = []                          # atomar im Loop-Thread
        try:
            await asyncio.to_thread(self._write_sync, lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_audit: Schreiben fehlgeschlagen: %s", exc)

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._flush()                     # letzte Zeilen sichern

    # ---- Datei-IO (laeuft im Worker-Thread) -----------------------------
    def _write_sync(self, lines: list[str]) -> None:
        self._rollover_if_stale()               # Mitternacht koennte gerade vorbei sein
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _archive_name(self, day: date) -> Path:
        # telegram_audit.log -> telegram_audit.2026-07-16.log.gz
        return self.path.with_name(
            f"{self.path.stem}.{day.isoformat()}{self.path.suffix}.gz"
        )

    def _rollover_if_stale(self) -> None:
        """Wenn die aktive Datei von einem frueheren Tag stammt: gzippen,
        wegarchivieren, alte Archive aufraeumen."""
        if not self.path.exists():
            return
        file_day = date.fromtimestamp(self.path.stat().st_mtime)
        if file_day >= date.today():
            return
        archive = self._archive_name(file_day)
        try:
            # An bestehendes Archiv anhaengen waere aufwaendig — bei einem
            # zweiten Rollover am selben Tag (sollte nicht vorkommen) einfach
            # ueberschreiben ist ok.
            with self.path.open("rb") as src, gzip.open(archive, "wb") as dst:
                shutil.copyfileobj(src, dst)
            self.path.unlink()
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_audit: Rotation fehlgeschlagen: %s", exc)
            return
        self._prune()

    def _prune(self) -> None:
        """Archive aelter als retention_days loeschen."""
        cutoff = date.today() - timedelta(days=self.retention_days)
        prefix = f"{self.path.stem}."          # "telegram_audit."
        suffix = f"{self.path.suffix}.gz"      # ".log.gz"
        for gz in self.path.parent.glob(f"{prefix}*{suffix}"):
            day_str = gz.name[len(prefix):-len(suffix)]
            try:
                d = date.fromisoformat(day_str)
            except ValueError:
                continue
            if d < cutoff:
                with suppress(OSError):
                    gz.unlink()
