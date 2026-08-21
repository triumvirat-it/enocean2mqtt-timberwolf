"""
Verwurf-Protokoll (reject_audit.log) — JEDES von der Pipeline verworfene
Telegramm/Feld mit dem GRUND des Verwurfs.

Zweck: Wir verwerfen unplausible Werte (Bit-Fehler der Eltako-Zaehler:
"echte Last + n*65536" durch ein gekipptes Bit im obersten Datenbyte DB3,
Momentanwerte ueber der Obergrenze, Monotonie-Verletzungen bei Zaehlerstaenden).
Damit man nach Wochen pruefen kann, WARUM wir was verworfen haben — und ob die
Stoerung nur die Zaehler betrifft oder auch andere Sensoren — steht hier jede
Verwurf-Entscheidung strukturiert und dauerhaft.

Dauerhaft AN (kein Schalter): das Protokoll ist der eigentliche Zweck, und der
Fussabdruck ist winzig (nur verworfene Telegramme, wenige pro Monat).

Technik wie telegram_audit.py: gebuendelte Schreibvorgaenge (RAM-Puffer + Flush
alle paar Sekunden via to_thread), taegliche gzip-Rotation, Aufbewahrung
retention_days. Bewusst als eigenstaendige Klasse gehalten (nicht von
telegram_audit abgeleitet), damit der erprobte Voll-Mitschnitt unberuehrt bleibt.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("enocean2mqtt")


class RejectAuditWriter:
    def __init__(
        self,
        path: str | Path,
        enabled: bool = True,
        retention_days: int = 45,
        flush_interval: float = 5.0,
    ) -> None:
        self.path = Path(path)                 # .../reject_audit.log (aktiver Tag)
        self.enabled = bool(enabled)
        self.retention_days = int(retention_days)
        self.flush_interval = float(flush_interval)
        self._buf: list[str] = []
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ---- Hot-Path: nur RAM, kein IO -------------------------------------
    def record(
        self, rx, field, value, reason,
        eep: str | None = None, detail: str = "", last_good=None,
    ) -> None:
        """Legt eine Verwurf-Zeile in den Puffer. Wird aus der Pipeline an
        jeder Verwurf-Stelle aufgerufen — darf niemals werfen, macht kein IO."""
        if not self.enabled:
            return
        try:
            self._buf.append(
                self.format_line(rx, field, value, reason, eep, detail, last_good)
            )
        except Exception:  # noqa: BLE001 — Protokoll darf den Empfang nie stoeren
            pass

    @staticmethod
    def format_line(rx, field, value, reason, eep=None, detail="", last_good=None) -> str:
        t = rx.telegram
        ts_full = datetime.fromtimestamp(rx.received_at)
        ts = ts_full.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts_full.microsecond // 10000:02d}"
        gw = rx.gateway_name or "-"
        raw = t.payload.hex().upper()
        last = "-" if last_good is None else f"{last_good}"
        extra = f" detail={detail}" if detail else ""
        return (
            f"{ts} gw={gw} id={t.sender_id_hex} {t.rorg_name} rssi={t.rssi_dbm} "
            f"eep={eep or '-'} field={field} value={value} reason={reason} "
            f"last={last} raw={raw}{extra}"
        )

    # ---- Lifecycle ------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            log.info("reject_audit: deaktiviert")
            return
        with suppress(Exception):
            await asyncio.to_thread(self._rollover_if_stale)
        self._task = asyncio.create_task(self._run(), name="reject_audit")
        log.info(
            "reject_audit: schreibe nach %s (taeglich rotierend, %d Tage)",
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
        self._buf = []
        try:
            await asyncio.to_thread(self._write_sync, lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("reject_audit: Schreiben fehlgeschlagen: %s", exc)

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._flush()

    # ---- Datei-IO (laeuft im Worker-Thread) -----------------------------
    def _write_sync(self, lines: list[str]) -> None:
        self._rollover_if_stale()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _archive_name(self, day: date) -> Path:
        return self.path.with_name(
            f"{self.path.stem}.{day.isoformat()}{self.path.suffix}.gz"
        )

    def _rollover_if_stale(self) -> None:
        if not self.path.exists():
            return
        file_day = date.fromtimestamp(self.path.stat().st_mtime)
        if file_day >= date.today():
            return
        archive = self._archive_name(file_day)
        try:
            with self.path.open("rb") as src, gzip.open(archive, "wb") as dst:
                shutil.copyfileobj(src, dst)
            self.path.unlink()
        except Exception as exc:  # noqa: BLE001
            log.warning("reject_audit: Rotation fehlgeschlagen: %s", exc)
            return
        self._prune()

    def _prune(self) -> None:
        cutoff = date.today() - timedelta(days=self.retention_days)
        prefix = f"{self.path.stem}."          # "reject_audit."
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
