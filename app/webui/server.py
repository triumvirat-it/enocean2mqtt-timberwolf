"""
Web-UI Backend mit FastAPI.

Routen:
- GET  /                      -> SPA (Single-Page-App)
- GET  /assets/*              -> Static files
- GET  /api/health
- GET  /api/info              -> Version, Brand-Daten
- GET  /api/gateways          -> Gateway-Status
- GET  /api/devices           -> Geraete-Liste
- POST /api/devices           -> neues Geraet anlegen
- PUT  /api/devices/{id}      -> Geraet aktualisieren
- DELETE /api/devices/{id}    -> Geraet entfernen
- POST /api/devices/import    -> Upload CSV oder devices.yaml
- POST /api/devices/{id}/channels/{cid}/test -> Aktor-Test
- GET  /api/products          -> Hersteller-DB (fuer Anlern-UI)
- GET  /api/log/recent        -> letzte N Telegramme
- WS   /api/ws/telegrams      -> Live-Stream
- GET  /api/diagnostics       -> RSSI-Tabelle, Cascade-Stats
- POST /api/teach/start       -> Anlern-Modus aktivieren
- POST /api/teach/stop        -> Anlern-Modus beenden
- GET  /api/teach/captured    -> erfasste Sender im Anlern-Modus
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..actor_state import ActorStateStore, estimate_position_now
from ..cascade import Cascade
from ..config import AppConfig
from ..devices import Device, DeviceChannel, DeviceRegistry, ObserverBinding
from ..eep import get_profile_registry
from ..gateway import GatewayManager, ReceivedTelegram
from ..mqtt_client import floor_segment, name_segment, room_segment, slugify
from ..products import ProductDB
from ..sender_routing import (
    fam14_address_to_sender_id,
    find_gateway_for_sender,
    fts14em_data_byte,
    fts14em_sender_id,
    used_and_free_ids,
)
from ..tx_router import TXRouter

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# FAM14-Liste (M91): serverseitige Persistenz im /data-Volume statt
# Browser-localStorage. So ist die Modulliste auf allen Geraeten (Desktop,
# Handy, Tablet) identisch und uebersteht das Loeschen von Browserdaten.
# ---------------------------------------------------------------------------

def _fam14_path(config_dir: Path) -> Path:
    return config_dir / "fam14_list.json"


def _load_fam14(config_dir: Path) -> list[dict]:
    """FAM14-Liste aus fam14_list.json lesen. Fehlt/kaputt -> leere Liste."""
    p = _fam14_path(config_dir)
    if not p.exists():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and it.get("base_id"):
            out.append({
                "name": str(it.get("name") or ""),
                "base_id": str(it["base_id"]).upper(),
            })
    return out


def _save_fam14(config_dir: Path, items: list[dict]) -> None:
    """FAM14-Liste atomar-ish nach fam14_list.json schreiben."""
    p = _fam14_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _format_validation_error(exc: Exception) -> str:
    """
    Pydantic ValidationError -> kurze, fuer User verstaendliche Meldung.
    Beispiel: 'channels.0.enocean_id: muss 8 Hex-Zeichen sein (z.B. FF810055)'.
    """
    try:
        from pydantic import ValidationError
        if isinstance(exc, ValidationError):
            parts = []
            for err in exc.errors()[:3]:  # max 3 Fehler
                loc = ".".join(str(x) for x in err.get("loc", []))
                msg = err.get("msg", "ungueltig")
                # Spezifische Hinweise bei bekannten Patterns
                if "pattern" in msg.lower() and "enocean_id" in loc.lower():
                    msg = "muss 8 Hex-Zeichen sein (z.B. FF810055)"
                elif "pattern" in msg.lower() and "learned_pair_id" in loc.lower():
                    msg = "muss 8 Hex-Zeichen sein (z.B. FF810055)"
                parts.append(f"{loc}: {msg}")
            return "; ".join(parts)
    except Exception:  # noqa: BLE001
        pass
    return str(exc)[:200]


def _product_to_json(p) -> dict:
    """ProductInfo -> JSON-Dict fuer UI, inkl. abgeleiteter Hints."""
    return {
        "manufacturer": p.manufacturer,
        "model": p.model,
        "eep": p.eep,
        "device_type": p.device_type,
        "description": p.description,
        "channel_count": p.channel_count,
        "fam14_bus": p.fam14_bus,
        "fam14_addressing": p.fam14_addressing,
        "ui_kind": p.ui_kind,
        "channel_name_template": p.channel_name_template,
    }


def _channels_for_extra_functions(
    infos: list, existing: list[dict],
) -> list[dict]:
    """
    Baut Channel-Dicts fuer die ZUSATZ-Funktionen eines Multi-Funktions-
    Geraets (alle Funktionen ausser der Primaerfunktion). Beispiel thanos:
    Primaer = Magnetkontakt, Zusatz = Temp/Sollwert + Tasten.

    Jede Funktion liefert `channel_count` Channels (meist 1, z.B. Switch_8 → 8).
    EEP kommt aus der Funktion (oder Modellname-Fallback), Richtung aus ui_kind.
    Sender-IDs bleiben leer — der User traegt sie nach / lernt an. Channel-IDs
    werden gegen die bereits vorhandenen disambiguiert (f<N>).

    Bewusst KEINE FAM14-Bus-/Tarif-/bundled-Logik: Zusatz-Funktionen sind in
    der DB durchweg simple Single-/N-Channel-Funktionen.
    """
    preg = get_profile_registry()
    used_ids = {str(c.get("channel_id")) for c in existing}
    out: list[dict] = []
    n_existing = len(existing)
    for fi, info in enumerate(infos, start=1):
        eep = info.eep
        if not eep:
            prof = preg.match_by_model(info.model)
            eep = prof.eep_id if prof else None
        direction = "bi" if info.ui_kind != "rx" else "rx"
        count = info.channel_count
        base_name = info.description or info.device_type or f"Funktion {fi}"
        for i in range(1, count + 1):
            cid = f"f{n_existing + len(out) + 1}"
            while cid in used_ids:
                cid = f"{cid}_"
            used_ids.add(cid)
            name = base_name if count == 1 else f"{base_name} {i}"
            out.append({
                "channel_id": cid,
                "name": name,
                "enocean_id": None,
                "eep": eep or "UNKNOWN",
                "direction": direction,
                "floor": "",
                "room": "",
                "light_group": "",
                "light_role": "",
                "senders": [],
                "via_gateway": None,
                "learned_pair_id": None,
                "controls": [],
                "observers": [],
                "meta": {
                    "device_type": info.device_type,
                    "function_of_model": True,
                },
            })
    return out


# ---------------------------------------------------------------------------
# Telegramm-Logger (Ring-Buffer + WS-Broadcaster)
# ---------------------------------------------------------------------------


class TelegramLog:
    """Ring-Buffer der letzten N Telegramme + WebSocket-Broadcast + State-Cache."""

    def __init__(self, capacity: int = 500) -> None:
        self.buffer: deque[dict] = deque(maxlen=capacity)
        self._ws_clients: set[WebSocket] = set()
        # Letzter Wert pro Sender-ID — fuer schnelle UI-Anzeige.
        # Schluessel ist normalerweise nur die Sender-ID (Hex-Upper).
        # Bei Multi-Sub-Channel-EEPs (A5-12-01 mit channel-Byte: DSZ14DRS-
        # Doppeltarif, F3Z14D-3-Phasen) speichern wir zusaetzlich pro
        # (sender_id, tele_channel) — Schluessel "SENDERID#N". Sonst wuerde
        # ein Tarif-1-Telegramm den Tarif-0-Wert im merge ueberschreiben.
        self.last_state: dict[str, dict] = {}

    def add(
        self,
        rx: ReceivedTelegram,
        device_match: dict | None = None,
        decoded: dict | None = None,
    ) -> None:
        sid = rx.telegram.sender_id_hex
        entry = {
            "ts": round(rx.received_at, 3),
            "gw": rx.gateway_name,
            "rorg": rx.telegram.rorg_name,
            "sender_id": sid,
            "rssi_dbm": rx.telegram.rssi_dbm,
            "payload": rx.telegram.payload.hex(),
            "device": device_match,
            # Roh-decodiert auch fuer unzugeordnete Sender (Observer-PTMs)
            "decoded": decoded,
            # M81b: Marker fuer Non-RADIO_ERP1 Pakete (ReMan, SmartAck, etc.).
            # UI kann per Checkbox optional ausblenden.
            "is_raw": rx.telegram.is_raw_packet,
        }
        self.buffer.append(entry)

        # Last-state-cache pro Sender — auch wenn der Sender NICHT als Device
        # angelegt ist (z.B. PTM-Beobachter die nur als observers verknuepft
        # sind). Wir mergen dekodierte Werte generisch: neue Felder ueberschreiben,
        # fehlende bleiben erhalten (Multi-Telegramm-Sensoren wie FWS61/FWZ14).
        #
        # Multi-Sub-Channel-EEPs (A5-12-01 DSZ14DRS/F3Z14D): zusaetzlich einen
        # Cache-Eintrag pro (sender_id, tele_channel) damit ein Tarif-1-
        # Telegramm den Tarif-0-Wert nicht ueberschreibt.
        cache_entry = entry

        def _add_field_meta(prev_meta: dict, decoded_now: dict, e: dict) -> dict:
            """Pro Feld den Timestamp + RSSI dieses Telegramms tracken.
            Damit kann die UI 'vor 1m · -73 dBm' PRO FELD anzeigen statt
            fuer den ganzen Sender (z.B. wenn die Energie alle 10 Min
            ankommt aber die Leistung alle 20 Sek)."""
            fm = dict(prev_meta or {})
            for fname in decoded_now:
                if fname.startswith("_"):
                    continue
                fm[fname] = {"ts": e["ts"], "rssi_dbm": e["rssi_dbm"]}
            return fm

        sub_keys: list[str] = [sid]
        if device_match:
            decoded_match = device_match.get("decoded") or {}
            if decoded_match:
                prev = self.last_state.get(sid)
                prev_decoded = (prev.get("device") or {}).get("decoded", {}) if prev else {}
                merged = {**prev_decoded, **decoded_match}
                # A5-12-01 Seriennummer aggregieren: wenn beide BCD-Teile
                # bekannt sind, daraus den vollstaendigen String bauen.
                sh = merged.get("serial_high")
                sl = merged.get("serial_low")
                if sh and sl and "serial_number" not in decoded_match:
                    merged["serial_number"] = f"S-{sh}{sl}"
                    # serial_number bekommt das Timestamp dieses Telegramms,
                    # weil es erst jetzt komplett ist.
                    decoded_match = dict(decoded_match)
                    decoded_match["serial_number"] = merged["serial_number"]
                merged["_field_meta"] = _add_field_meta(
                    prev_decoded.get("_field_meta", {}), decoded_match, entry,
                )
                cache_entry = {**entry, "device": {**device_match, "decoded": merged}}

                # Pro tele_channel separat speichern (wenn das EEP einen
                # telegram_channel_field hat und der Wert im decoded ist)
                eep_id = device_match.get("eep")
                if eep_id:
                    profile = get_profile_registry().get(eep_id)
                    tc_field = profile.telegram_channel_field if profile else None
                    if tc_field and tc_field in decoded_match:
                        # tc-field kann int (DSZ14DRS Doppeltarif: 0/1) oder
                        # String sein (FT55 rocker_side: 'A'/'B'). str()
                        # konvertiert beides konsistent: int 0 -> '0', 'A' -> 'A'.
                        sub_key = f"{sid}#{decoded_match[tc_field]}"
                        sub_keys.append(sub_key)
                        prev_sub = self.last_state.get(sub_key)
                        prev_sub_decoded = (prev_sub.get("device") or {}).get("decoded", {}) if prev_sub else {}
                        merged_sub = {**prev_sub_decoded, **decoded_match}
                        merged_sub["_field_meta"] = _add_field_meta(
                            prev_sub_decoded.get("_field_meta", {}), decoded_match, entry,
                        )
                        sub_cache = {**entry, "device": {**device_match, "decoded": merged_sub}}
                        self.last_state[sub_key] = sub_cache

        self.last_state[sid] = cache_entry
        # broadcast — fire-and-forget
        for ws in list(self._ws_clients):
            try:
                asyncio.get_running_loop().create_task(ws.send_json(entry))
            except Exception:  # noqa: BLE001
                self._ws_clients.discard(ws)

    def add_tx(
        self,
        sender_id_hex: str,
        rorg_name: str,
        payload_hex: str,
        gateway: str,
        label: str = "",
    ) -> None:
        """
        M84: Loggt eine AUSGEHENDE Sendung (TX) ins Live-Log.

        Damit sieht der User seine eigenen Befehle (Schalten, Pairing-Telegramme,
        Lerntelegramme) zeitlich neben den empfangenen Telegrammen.
        """
        import time as _time
        entry = {
            "ts": round(_time.time(), 3),
            "gw": gateway,
            "rorg": rorg_name,
            "sender_id": sender_id_hex.upper(),
            "rssi_dbm": None,
            "payload": payload_hex,
            "device": None,
            "decoded": None,
            "is_raw": False,
            # M84: Marker dass dies eine eigene Aussendung ist (UI → "→ TX")
            "is_tx": True,
            "tx_label": label or "",
        }
        self.buffer.append(entry)
        for ws in list(self._ws_clients):
            try:
                asyncio.get_running_loop().create_task(ws.send_json(entry))
            except Exception:  # noqa: BLE001
                self._ws_clients.discard(ws)

    def add_raw(
        self,
        gateway: str,
        packet_type_name: str,
        data_hex: str,
        optional_hex: str,
    ) -> None:
        """
        M86: Roh-Funk-Monitor — JEDES ESP3-Paket ungefiltert (vor Cascade/Dedup).
        Zeigt komplette data + optional Bytes ohne jede Interpretation.
        Eigener Eintrag mit is_raw_monitor=True, im Frontend per Checkbox
        einblendbar.
        """
        import time as _time
        entry = {
            "ts": round(_time.time(), 3),
            "gw": gateway,
            "rorg": packet_type_name,
            "sender_id": "",
            "rssi_dbm": None,
            "payload": data_hex,
            "optional": optional_hex,
            "device": None,
            "decoded": None,
            "is_raw": True,
            "is_raw_monitor": True,
        }
        self.buffer.append(entry)
        for ws in list(self._ws_clients):
            try:
                asyncio.get_running_loop().create_task(ws.send_json(entry))
            except Exception:  # noqa: BLE001
                self._ws_clients.discard(ws)

    def recent(self, limit: int = 50) -> list[dict]:
        return list(self.buffer)[-limit:]

    def add_ws(self, ws: WebSocket) -> None:
        self._ws_clients.add(ws)

    def remove_ws(self, ws: WebSocket) -> None:
        self._ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# Anlern-Modus
# ---------------------------------------------------------------------------


class TeachInMode:
    """
    Sammelt Sender-IDs die noch nicht in der DeviceRegistry sind.

    Default-Verhalten: nur Telegramme akzeptieren die explizit als LRN
    gekennzeichnet sind (4BS/1BS-Lern-Telegramme mit LRN-bit). Das verhindert
    versehentliches Anlernen von Sensoren die zufaellig gerade senden.

    Fuer nicht zugaengliche Sensoren ohne LRN-Taste (Wand-PTMs, schon
    verbaute Module) kann `only_lrn=False` gesetzt werden — dann wird jedes
    empfangene Telegramm zum Anlern-Kandidaten.
    """

    def __init__(self) -> None:
        self.active = False
        self.started_at: float | None = None
        self.only_lrn: bool = True
        # sender_id_hex -> {first_seen, last_seen, count, rorg, rssi_history}
        self.captured: dict[str, dict[str, Any]] = {}

    def start(self, only_lrn: bool = True) -> None:
        self.active = True
        self.only_lrn = only_lrn
        self.started_at = time.time()
        self.captured.clear()
        log.info("Anlern-Modus aktiviert (only_lrn=%s)", only_lrn)

    def stop(self) -> None:
        self.active = False
        log.info("Anlern-Modus beendet (%d Sender erfasst)", len(self.captured))

    def observe(
        self,
        rx: ReceivedTelegram,
        is_unknown: bool,
        decoded: dict[str, Any] | None = None,
    ) -> None:
        if not self.active or not is_unknown:
            return
        # LRN-Filter: nur Telegramme die der Decoder als teach_in markiert hat
        if self.only_lrn:
            if not decoded or not decoded.get("teach_in"):
                return
        sid = rx.telegram.sender_id_hex
        if sid not in self.captured:
            self.captured[sid] = {
                "sender_id": sid,
                "first_seen": rx.received_at,
                "last_seen": rx.received_at,
                "count": 0,
                "rorg": rx.telegram.rorg_name,
                "rssi_history": [],
                "payload_history": [],
            }
        entry = self.captured[sid]
        entry["last_seen"] = rx.received_at
        entry["count"] += 1
        if rx.telegram.rssi_dbm is not None:
            entry["rssi_history"].append(rx.telegram.rssi_dbm)
            if len(entry["rssi_history"]) > 10:
                entry["rssi_history"].pop(0)
        entry["payload_history"].append(rx.telegram.payload.hex())
        if len(entry["payload_history"]) > 5:
            entry["payload_history"].pop(0)


# ---------------------------------------------------------------------------
# Pydantic-Schemas
# ---------------------------------------------------------------------------


class GatewayCreate(BaseModel):
    name: str
    type: str = "TCM310-LAN"
    host: str
    port: int = 5000
    enabled: bool = True
    base_id: str | None = None
    rssi_filter: int | None = None
    repeater_level: int = 0
    floor_assignments: list[str] = Field(default_factory=list)


class SenderBindingCreate(BaseModel):
    """M59: ein SenderBinding-Eintrag im PUT-Payload."""
    sender_id: str
    via_gateway: str | None = None
    label: str = ""
    active: bool = False


class DeviceChannelCreate(BaseModel):
    channel_id: str
    name: str
    # M95: optionaler Etage/Raum-Override pro Channel (Multi-Channel-Aktoren)
    floor: str = ""
    room: str = ""
    # M103: Farbleuchte aus 2 Kanaelen (Gruppe + Rolle)
    light_group: str = ""
    light_role: str = ""
    enocean_id: str | None = None
    eep: str = "UNKNOWN"
    direction: str = "rx"
    # M59 Multi-Sender
    senders: list[SenderBindingCreate] = Field(default_factory=list)
    # Legacy-Felder (DEPRECATED, werden via Model-Validator in senders migriert)
    via_gateway: str | None = None
    learned_pair_id: str | None = None
    # Akzeptiert legacy strings ("FF810055" = match='any') ODER dict
    # {sender_id, match} mit Filter (rocker:A/B, event:A_top..B_bottom).
    observers: list[ObserverBinding | str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class DeviceCreate(BaseModel):
    device_id: str
    manufacturer: str = ""
    model: str = ""
    name: str
    room: str = ""
    floor: str = ""
    channels: list[DeviceChannelCreate] = Field(default_factory=list)
    notes: str = ""


class Fam14Entry(BaseModel):
    """M91: ein FAM14-Modul (Name + Base-ID), serverseitig persistiert."""
    name: str = ""
    base_id: str = Field(pattern=r"^[0-9A-Fa-f]{8}$")


class Fam14ListPayload(BaseModel):
    """PUT-Body fuer /api/fam14."""
    items: list[Fam14Entry] = Field(default_factory=list)


class ConfigUpdate(BaseModel):
    """PUT-Body fuer /api/config (MQTT + globale Defaults)."""
    mqtt: dict | None = None
    defaults: dict | None = None


# ---------------------------------------------------------------------------
# Server-Klasse
# ---------------------------------------------------------------------------


class WebUIServer:
    """
    Container fuer alles was die Web-UI braucht.

    Wird in main.py instanziiert und in uvicorn gehostet.
    """

    def __init__(
        self,
        cfg: AppConfig,
        manager: GatewayManager,
        devices: DeviceRegistry,
        cascade: Cascade,
        tx_router: TXRouter,
        products: ProductDB,
        config_dir: Path,
        state_store: ActorStateStore | None = None,
        observed_senders=None,
        publisher=None,
    ) -> None:
        self.cfg = cfg
        self.manager = manager
        self.devices = devices
        self.cascade = cascade
        self.tx_router = tx_router
        self.products = products
        self.config_dir = config_dir
        self.state_store = state_store
        # M61: observed_senders kann None sein in Tests
        self.observed_senders = observed_senders
        # MQTT-Publisher fuer Live-Reconnect beim Aendern der MQTT-Config
        # (Einstellungsseite). None in Tests.
        self.publisher = publisher
        self.log_buffer = TelegramLog()
        self.teach = TeachInMode()
        self._uvicorn_task: asyncio.Task | None = None
        self._uvicorn_server: uvicorn.Server | None = None

    async def start(self) -> None:
        if not self.cfg.webui.enabled:
            log.info("Web-UI deaktiviert (config)")
            return
        app = create_app(self)
        config = uvicorn.Config(
            app,
            host=self.cfg.webui.host,
            port=self.cfg.webui.port,
            log_level="warning",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_task = asyncio.create_task(
            self._uvicorn_server.serve(), name="webui"
        )
        log.info(
            "Web-UI gestartet auf http://%s:%d",
            self.cfg.webui.host, self.cfg.webui.port,
        )

    async def stop(self) -> None:
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._uvicorn_task:
            try:
                await asyncio.wait_for(self._uvicorn_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._uvicorn_task.cancel()


# ---------------------------------------------------------------------------
# FastAPI-App-Factory
# ---------------------------------------------------------------------------


def create_app(srv: WebUIServer) -> FastAPI:
    app = FastAPI(
        title="enocean2mqtt_timberwolf",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # -------- Static / SPA --------

    # Content-Type-Mapping nach Datei-Extension
    _CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".svg":  "image/svg+xml",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".ico":  "image/x-icon",
        ".json": "application/json",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }

    @app.get("/")
    async def index():
        index_file = STATIC_DIR / "index.html"
        try:
            content = index_file.read_text(encoding="utf-8")
            # Cache-Buster: alle /assets/*.js und *.css mit ?v=<version> versehen,
            # damit Browser bei jedem Update frisch laedt
            content = content.replace(
                'src="/assets/vue.global.js"',
                f'src="/assets/vue.global.js?v={__version__}"'
            ).replace(
                'src="/assets/app.js"',
                f'src="/assets/app.js?v={__version__}"'
            ).replace(
                'href="/assets/brand.css"',
                f'href="/assets/brand.css?v={__version__}"'
            )
            return HTMLResponse(
                content=content,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        except Exception as exc:
            log.exception("Index handler failed")
            return JSONResponse(
                {"error": str(exc), "type": type(exc).__name__,
                 "static_dir": str(STATIC_DIR),
                 "static_exists": STATIC_DIR.exists()},
                status_code=500,
            )

    @app.get("/m")
    async def mobile():
        """M93: schlanke Handy-Schaltseite (eigene Mini-App, unabh. von app.js)."""
        f = STATIC_DIR / "mobile.html"
        try:
            content = f.read_text(encoding="utf-8")
            content = content.replace(
                'src="/assets/vue.global.js"',
                f'src="/assets/vue.global.js?v={__version__}"'
            ).replace(
                'href="/assets/brand.css"',
                f'href="/assets/brand.css?v={__version__}"'
            )
            return HTMLResponse(
                content=content,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Mobile handler failed")
            return JSONResponse({"error": str(exc)}, status_code=500)

    # Eigene Static-Route (statt StaticFiles) — keine Threads, asyncio-pur.
    # Loesung fuer "RuntimeError: can't start new thread" auf RAM-armen
    # Hosts wie dem Timberwolf BCM2711.
    assets_dir = (STATIC_DIR / "assets").resolve()

    @app.get("/assets/{path:path}")
    async def serve_asset(path: str):
        # Pfad sicher aufloesen, Directory-Traversal verhindern
        target = (assets_dir / path).resolve()
        try:
            target.relative_to(assets_dir)
        except ValueError:
            raise HTTPException(403, "forbidden")
        if not target.is_file():
            raise HTTPException(404, f"not found: {path}")
        content = target.read_bytes()
        media_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return Response(
            content=content,
            media_type=media_type,
            # Kein Browser-Cache fuer Assets — Versionierung passiert via
            # Cache-Buster im Index-HTML
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # -------- Info / Health --------

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/info")
    async def info() -> dict:
        return {
            "version": __version__,
            "gateways": len(srv.cfg.gateways),
            "devices": len(srv.devices),
            "mqtt_host": srv.cfg.mqtt.host,
            "mqtt_port": srv.cfg.mqtt.port,
            "mqtt_connected": (
                srv.publisher.is_connected if srv.publisher is not None else None
            ),
            "cascade_strategy": srv.cfg.cascade.send_strategy,
        }

    # -------- Einstellungen (MQTT + globale Defaults) --------

    @app.get("/api/config")
    async def get_config() -> dict:
        m = srv.cfg.mqtt
        return {
            "mqtt": {
                "host": m.host,
                "port": m.port,
                "username": m.username or "",
                "password": m.password or "",
                "base_topic": m.base_topic,
                "qos": m.qos,
                "retain_state": m.retain_state,
            },
            "defaults": {
                "ptm_on_press": srv.cfg.defaults.ptm_on_press,
            },
        }

    @app.put("/api/config")
    async def put_config(payload: ConfigUpdate) -> dict:
        from ..config import MQTTConfig

        changed_mqtt = False
        if payload.mqtt is not None:
            merged = {
                **srv.cfg.mqtt.model_dump(),
                **{k: v for k, v in payload.mqtt.items()
                   if k in MQTTConfig.model_fields},
            }
            # Leere User/Passwort-Felder als "nicht gesetzt" interpretieren.
            if merged.get("username") == "":
                merged["username"] = None
            if merged.get("password") == "":
                merged["password"] = None
            try:
                srv.cfg.mqtt = MQTTConfig.model_validate(merged)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(422, _format_validation_error(exc))
            changed_mqtt = True

        if payload.defaults is not None:
            val = payload.defaults.get("ptm_on_press")
            if val is not None:
                if val not in ("I", "0"):
                    raise HTTPException(422, "ptm_on_press muss 'I' oder '0' sein")
                srv.cfg.defaults.ptm_on_press = val

        srv.cfg.save(srv.config_dir)

        # MQTT live neu verbinden (kein Container-Neustart noetig)
        reconnected = False
        if changed_mqtt and srv.publisher is not None:
            try:
                await srv.publisher.reload(srv.cfg.mqtt)
                reconnected = True
            except Exception as exc:  # noqa: BLE001
                log.warning("MQTT-Reload fehlgeschlagen: %s", exc)

        return {"ok": True, "mqtt_reconnected": reconnected}

    # -------- Gateways --------

    @app.get("/api/gateways")
    async def list_gateways() -> list[dict]:
        status = srv.manager.status_summary()
        # M60: Channel-zugewiesene Sender-IDs einsammeln (assigned).
        # M61: Plus live-observed Sender-IDs die noch keinem Channel angehoeren.
        # Beide zaehlen als "belegt" — die observed aber visuell getrennt.
        used_lookup: dict[str, list[dict]] = {}
        for d in srv.devices.all():
            for ch in d.channels:
                # M59: alle SenderBindings (aktiv + inaktiv)
                for s in ch.senders:
                    sid = s.sender_id.upper()
                    used_lookup.setdefault(sid, []).append({
                        "device_id": d.device_id,
                        "device_name": d.name,
                        "channel_id": ch.channel_id,
                        "channel_name": ch.name,
                        "active": s.active,
                        "label": s.label,
                    })
        assigned_ids = list(used_lookup.keys())
        # M62/M63: defensive self-healing
        #  - observed-Eintraege fuer Channel-assignedIDs entfernen
        #  - observed-Eintraege gegen aktuelle Gateway-Config umhaengen/loeschen
        if srv.observed_senders:
            n1 = srv.observed_senders.cleanup_against_known_ids(assigned_ids)
            n2, n3 = srv.observed_senders.remap_to_correct_gateway(srv.cfg.gateways)
            if n1 or n2 or n3:
                srv.observed_senders.save(force=True)
        # M61: observed sender IDs pro Gateway (nach Cleanup)
        observed_by_gw: dict[str, list[dict]] = {}
        if srv.observed_senders:
            for obs in srv.observed_senders._items.values():
                observed_by_gw.setdefault(obs.gateway, []).append({
                    "sender_id": obs.sender_id,
                    "rorg": obs.rorg,
                    "count": obs.count,
                    "first_seen": obs.first_seen,
                    "last_seen": obs.last_seen,
                    "rssi_dbm": obs.rssi_dbm,
                })

        result = []
        for gw_cfg in srv.cfg.gateways:
            s = status.get(gw_cfg.name, {})
            entry = {
                "name": gw_cfg.name,
                "type": gw_cfg.type,
                "host": gw_cfg.host,
                "port": gw_cfg.port,
                "enabled": gw_cfg.enabled,
                "base_id": gw_cfg.base_id,
                "rssi_filter": gw_cfg.rssi_filter,
                "repeater_level": gw_cfg.repeater_level,
                "floor_assignments": list(gw_cfg.floor_assignments),
                "runtime": s,
            }
            # M60+M61: Block-Belegung wenn Base-ID gesetzt
            if gw_cfg.base_id:
                # observed IDs auch als used betrachten — duerfen NICHT als
                # frei vorgeschlagen werden, sind aber visuell getrennt
                gw_observed = observed_by_gw.get(gw_cfg.name, [])
                observed_ids = [o["sender_id"] for o in gw_observed]
                total_used_for_gw = assigned_ids + observed_ids
                used, free = used_and_free_ids(gw_cfg.base_id, total_used_for_gw)
                entry["block"] = {
                    "assigned": [
                        {"sender_id": sid, "usages": used_lookup.get(sid, [])}
                        for sid in used
                        if sid in used_lookup
                    ],
                    "observed": gw_observed,
                    "used_total": len(used),
                    "free_count": len(free),
                    "free_first": free[:10],
                    "total": 128,
                }
            result.append(entry)
        return result

    @app.post("/api/gateways/{gateway_name}/forget-observed/{sender_id}")
    async def forget_observed(gateway_name: str, sender_id: str) -> dict:
        """
        Entfernt eine observed Sender-ID. Wird automatisch aufgerufen wenn
        der User die ID als Channel anlegt — oder manuell wenn er sie
        explizit ignorieren will.
        """
        if not srv.observed_senders:
            raise HTTPException(503, "observed-senders nicht verfuegbar")
        n = srv.observed_senders.forget(sender_id)
        srv.observed_senders.save(force=True)
        return {"ok": True, "removed": n, "sender_id": sender_id.upper()}

    @app.get("/api/gateways/{gateway_name}/next-free-sender-id")
    async def next_free_sender_id(gateway_name: str) -> dict:
        """
        Liefert die naechste freie Sender-ID im Block des Gateways (M61).
        Beruecksichtigt sowohl Channel-zugewiesene als auch live observed IDs.
        """
        gw = next(
            (g for g in srv.cfg.gateways if g.name == gateway_name),
            None,
        )
        if not gw:
            raise HTTPException(404, f"Gateway '{gateway_name}' nicht gefunden")
        if not gw.base_id:
            raise HTTPException(
                400, f"Gateway '{gateway_name}' hat keine base_id konfiguriert",
            )
        # Channel-zugewiesene IDs
        assigned: list[str] = []
        for d in srv.devices.all():
            for ch in d.channels:
                for s in ch.senders:
                    assigned.append(s.sender_id.upper())
        # Observed IDs
        observed_ids: list[str] = []
        if srv.observed_senders:
            observed_ids = srv.observed_senders.all_sender_ids_for_gateway(
                gateway_name,
            )
        _, free = used_and_free_ids(gw.base_id, assigned + observed_ids)
        if not free:
            raise HTTPException(409, f"Block des Gateways '{gateway_name}' ist voll")
        # Erste freie — typischerweise Base+1 (Base+0 wird in der Praxis oft
        # gemieden, ist aber technisch erlaubt)
        return {
            "sender_id": free[0],
            "gateway": gateway_name,
            "base_id": gw.base_id,
            "free_remaining": len(free),
        }

    @app.post("/api/gateways", status_code=201)
    async def create_gateway(payload: GatewayCreate) -> dict:
        # Doppelnamen verhindern
        if any(g.name == payload.name for g in srv.cfg.gateways):
            raise HTTPException(409, f"Gateway '{payload.name}' existiert bereits")
        from ..config import GatewayConfig

        gw = GatewayConfig(**payload.model_dump())
        srv.cfg.gateways.append(gw)
        srv.cfg.save(srv.config_dir)
        log.info("Gateway angelegt: %s — RESTART erforderlich fuer Aktivierung", gw.name)
        return {"ok": True, "gateway": gw.model_dump(mode="json"), "restart_required": True}

    @app.put("/api/gateways/{name}")
    async def update_gateway(name: str, payload: GatewayCreate) -> dict:
        idx = next(
            (i for i, g in enumerate(srv.cfg.gateways) if g.name == name),
            -1,
        )
        if idx < 0:
            raise HTTPException(404, "Gateway not found")
        from ..config import GatewayConfig

        gw = GatewayConfig(**payload.model_dump())
        srv.cfg.gateways[idx] = gw
        srv.cfg.save(srv.config_dir)
        log.info("Gateway %s aktualisiert — RESTART noetig fuer Netzwerk-Aenderungen", name)
        return {"ok": True, "gateway": gw.model_dump(mode="json"), "restart_required": True}

    @app.delete("/api/gateways/{name}")
    async def delete_gateway(name: str) -> dict:
        idx = next(
            (i for i, g in enumerate(srv.cfg.gateways) if g.name == name),
            -1,
        )
        if idx < 0:
            raise HTTPException(404, "Gateway not found")
        del srv.cfg.gateways[idx]
        srv.cfg.save(srv.config_dir)
        log.info("Gateway %s entfernt", name)
        return {"ok": True, "removed": name, "restart_required": True}

    # -------- FAM14-Liste (M91: serverseitig statt localStorage) --------

    @app.get("/api/fam14")
    async def get_fam14() -> dict:
        return {"items": _load_fam14(srv.config_dir)}

    @app.put("/api/fam14")
    async def put_fam14(payload: Fam14ListPayload) -> dict:
        # Normalisieren (Base-ID gross) + Duplikate (gleiche base_id) entfernen.
        seen: set[str] = set()
        items: list[dict] = []
        for e in payload.items:
            bid = e.base_id.upper()
            if bid in seen:
                continue
            seen.add(bid)
            items.append({"name": e.name, "base_id": bid})
        _save_fam14(srv.config_dir, items)
        log.info("FAM14-Liste gespeichert: %d Modul(e)", len(items))
        return {"items": items}

    # -------- Profiles --------

    @app.get("/api/profiles")
    async def list_profiles() -> list[dict]:
        """
        Alle bekannten EEPProfiles inkl. Field-Defs (label, unit, icon, format).
        Die UI nutzt das fuer channelKind / formatDecoded / Anzeigen.
        """
        preg = get_profile_registry()
        return [p.to_json() for p in preg.all()]

    # -------- Devices --------

    @app.get("/api/devices")
    async def list_devices() -> list[dict]:
        """
        Geraete-Liste mit last_state, actor_state, Topic und Profile-Info
        pro Channel. Topic ist der MQTT-Pfad den der User in Timberwolf
        verwenden soll: enocean/<floor>/<room>/<device>/<channel>/state.
        """
        out = []
        preg = get_profile_registry()
        base_topic = srv.cfg.mqtt.base_topic.rstrip("/")
        # Erste Gateway-Base-ID fuer ID-Berechnungs-Hinweise im UI
        # (Sender-ID = Base + PCT14-Adresse + Offset bei Eltako-Bus-Modulen).
        first_base_id = next(
            (gw.base_id for gw in srv.cfg.gateways if gw.base_id),
            None,
        )
        for d in srv.devices.all():
            dd = d.model_dump(mode="json")
            # M95: floor/room werden pro Channel effektiv berechnet (Channel-
            # Override vor Device) — siehe Topic-Aufbau weiter unten.
            # Product-Info anhaengen — description aus products.yaml
            # enthaelt bei Eltako-Bus-Modulen den ID-Rechenweg (M53).
            prod = srv.products.lookup(d.manufacturer, d.model)
            if prod:
                dd["product_info"] = {
                    "description": prod.description,
                    "channel_count": prod.channel_count,
                    "multi_channel_kind": prod.multi_channel_kind,
                    "channel_name_template": prod.channel_name_template,
                    "teach_in_procedure": prod.teach_in_procedure,
                    "fam14_bus": prod.fam14_bus,
                    "fam14_addressing": prod.fam14_addressing,
                    "device_type": prod.device_type,
                }
            if first_base_id:
                dd["gateway_base_id"] = first_base_id
            # M60: Pro Sender automatisch das passende Gateway zuordnen
            # (auch wenn deaktiviert). Damit sieht der User sofort welches
            # Gateway den Befehl mit dieser ID raussenden MUSS.
            for ch_dd in dd.get("channels", []):
                for s in ch_dd.get("senders", []):
                    sid = s.get("sender_id")
                    if sid:
                        s["gateway_match"] = find_gateway_for_sender(
                            sid, srv.cfg.gateways,
                        )
            for c in dd.get("channels", []):
                sid = c.get("enocean_id")
                if sid:
                    sid_up = sid.upper()
                    tele_chan = (c.get("meta") or {}).get("tele_channel")
                    # Bei Multi-Sub-Channel-EEPs (DSZ14DRS-Doppeltarif): NUR den
                    # passenden Sub-Channel-Cache verwenden. KEIN Fallback auf
                    # sender-only — sonst zeigt der noch-nie-gesendete Tarif 1
                    # die Werte des aktiven Tarif 0.
                    if tele_chan is not None:
                        # tele_chan kann int (Doppeltarif 0/1) oder String
                        # (FT55-Wippentaster 'A'/'B') sein. f-string normalisiert.
                        last = srv.log_buffer.last_state.get(f"{sid_up}#{tele_chan}")
                    else:
                        last = srv.log_buffer.last_state.get(sid_up)
                    if last:
                        # Pro Channel den FIELD-SPEZIFISCHEN Timestamp + RSSI
                        # einsetzen statt den letzten Telegramm-Zeitpunkt.
                        # Beispiel: Energie wird alle 10 Min gesendet, Leistung
                        # alle 20 Sek — beide Channels sollen ihre eigene
                        # "vor X · -73 dBm" Anzeige bekommen.
                        field_filter = (c.get("meta") or {}).get("field")
                        if field_filter:
                            fm = (last.get("device") or {}).get("decoded", {}).get("_field_meta") or {}
                            field_entry = fm.get(field_filter)
                            if field_entry:
                                last = {
                                    **last,
                                    "ts": field_entry.get("ts", last.get("ts")),
                                    "rssi_dbm": field_entry.get("rssi_dbm", last.get("rssi_dbm")),
                                }
                        c["last_state"] = last
                # ActorState fuer Position/Dim/On injecten
                if srv.state_store:
                    st = srv.state_store.get(d.device_id, c["channel_id"])
                    state_dict = st.to_dict()
                    if st.moving:
                        state_dict["position_percent"] = round(
                            estimate_position_now(st), 1
                        )
                    c["actor_state"] = state_dict
                # Topic-Pfad: {base}/{floor}/{room}/{device_name}/{channel_name}/state
                # device_name aus device.name (Fallback device_id); channel_name
                # aus channel.name (Fallback channel_id). Leere floor/room werden
                # uebersprungen — kein Leersegment im Pfad.
                d_slug = name_segment(d.name or "", fallback=d.device_id)
                ch_slug = name_segment(c.get("name") or "", fallback=c.get("channel_id", ""))
                # M95: Channel-Override (Raum den DIESER Channel schaltet) hat
                # Vorrang, sonst erbt der Channel floor/room vom Device.
                eff_floor = floor_segment((c.get("floor") or "").strip() or d.floor)
                eff_room = room_segment((c.get("room") or "").strip() or d.room)
                # Ein-Kanal-Geraete: Kanal-Segment faellt weg (identisch zu
                # device_topic()). Projektvorgabe: KEINE Doppelung
                # .../device/device/state — der Timberwolf haengt /set an das
                # collapsed Topic an. Erst Multi-Kanal-Geraete brauchen das
                # Kanal-Segment zur Unterscheidung.
                parts = [base_topic, eff_floor, eff_room, d_slug]
                if len(d.channels) > 1 and ch_slug:
                    parts.append(ch_slug)
                parts.append("state")
                c["topic"] = "/".join(p for p in parts if p)
                # Profile-Info fuer UI (label/unit/icon/kind je field)
                profile = preg.get(c.get("eep"))
                field_filter = (c.get("meta") or {}).get("field")
                if profile:
                    # M73: A5-38-08 hat zwei Sub-Profile (01=Switch, 02=Dim).
                    # device_type vom Channel oder Produkt hat Vorrang vor
                    # profile.ui_kind, damit FSR14 (Switch_X) Switch-Buttons
                    # zeigt statt Dimmer-Buttons.
                    # M78: device_types die "switch" enthalten aber Sensor
                    # sind (PTMSwitchModule, KeyCardSwitch, DUXButtons) duerfen
                    # NICHT als Aktor klassifiziert werden — der User kann die
                    # Sender-ID des Tasters nicht parallel ausstrahlen. Reine
                    # rx-Anzeige, kein TX-Block, keine Beobachter-Liste.
                    ui_kind = profile.ui_kind
                    dt_meta = ((c.get("meta") or {}).get("device_type") or "").lower()
                    pdt = (prod.device_type if prod else "").lower()
                    combined_dt = dt_meta + " " + pdt
                    SENSOR_HINTS = (
                        "ptmswitch", "ptm switch",
                        "keycardswitch", "duxbuttons",
                        "windowhandle", "windowcontact", "pir",
                        "smokealarm", "leakage", "gassensor",
                        "weatherstation", "multisensor",
                        "temperature", "humidity", "brightness",
                        "energymeter", "meter", "counter",
                        "voltagesensor", "tracker",
                        "buildingblock", "gateway",
                    )
                    is_sensor_module = any(
                        h in combined_dt for h in SENSOR_HINTS
                    )
                    # M79: gebuendelte PTM-Channels (OPUS Bridge u.a.) sind
                    # immer rx, auch wenn das Produkt ein Aktor ist.
                    is_bundled_ptm = bool(
                        (c.get("meta") or {}).get("is_bundled_ptm")
                    )
                    # M79d: Sicherheitsnetz fuer ALTE Imports (vor M79) bei
                    # denen die PTM-Sub-Channels noch als D2-01-XX importiert
                    # wurden — der Channel-Name enthaelt dann typischerweise
                    # "PTM"/"Taster". Forciert rx, damit keine sinnlosen
                    # Aktor-Buttons erscheinen. Re-Import erzeugt sauberes
                    # Schema mit F6-02-01 + is_bundled_ptm-Flag.
                    cname = (c.get("name") or "").lower()
                    is_ptm_by_name = any(
                        kw in cname for kw in ("ptm", "funktaster", "wandtaster")
                    )
                    if is_sensor_module or is_bundled_ptm or is_ptm_by_name:
                        ui_kind = "rx"
                    elif "switch" in combined_dt or "schalter" in combined_dt:
                        ui_kind = "switch"
                    elif "jalousie" in combined_dt or "shutter" in combined_dt or "rolladen" in combined_dt:
                        ui_kind = "shutter"
                    elif "dimmer" in combined_dt:
                        ui_kind = "dimmer"
                    elif "valve" in combined_dt or "ventil" in combined_dt:
                        ui_kind = "valve"
                    c["profile"] = {
                        "name": profile.name,
                        "ui_kind": ui_kind,
                    }
                    # Wenn channel.meta.field gesetzt ist, picken wir das Feld:
                    if field_filter:
                        fdef = profile.get_field(field_filter)
                        if fdef:
                            c["profile"]["field"] = fdef.to_json()
                    else:
                        # Single-Channel-Geraet — alle Felder gehoeren dazu
                        c["profile"]["fields"] = [
                            f.to_json() for f in profile.fields
                        ]
                # Beobachter-States: pro observer-ID den letzten Status mitgeben
                # (Sender-ID, letzte Empfangszeit, kurze Beschreibung).
                # Decoded kann entweder im device.decoded (wenn der Sender als
                # eigenes Device existiert) oder im top-level decoded (PTM ohne
                # Device-Eintrag) stehen.
                obs_states = []
                # Klassische Beobachter (Wand-PTMs, Bewegungsmelder etc.)
                # WICHTIG: NICHT `d` als lokale Variable nutzen — `d` ist das
                # Device-Objekt der aeusseren Schleife und wird unten noch
                # benoetigt (state_store.get(d.device_id, ...)).
                for obs in (c.get("observers") or []):
                    # Observer kann legacy string ODER dict {sender_id, match}
                    # sein. Beide Formen normalisieren.
                    if isinstance(obs, str):
                        obs_sid = obs.upper()
                        obs_match = "any"
                    elif isinstance(obs, dict):
                        obs_sid = (obs.get("sender_id") or "").upper()
                        obs_match = obs.get("match", "any")
                    else:
                        continue
                    if not obs_sid:
                        continue
                    last = srv.log_buffer.last_state.get(obs_sid)
                    if last:
                        decoded_for_obs = (last.get("device") or {}).get("decoded") or last.get("decoded")
                    else:
                        decoded_for_obs = None
                    obs_states.append({
                        "sender_id": obs_sid,
                        "match": obs_match,
                        "kind": "observer",
                        "label": "",
                        "last_seen": last.get("ts") if last else None,
                        "rssi_dbm": last.get("rssi_dbm") if last else None,
                        "decoded": decoded_for_obs,
                        "rorg": last.get("rorg") if last else None,
                    })
                # M70: inaktive SenderBindings (M59) wirken auch als Beobachter
                for s in (c.get("senders") or []):
                    if s.get("active"):
                        continue
                    sid = (s.get("sender_id") or "").upper()
                    if not sid:
                        continue
                    last = srv.log_buffer.last_state.get(sid)
                    if last:
                        decoded_for_obs = (last.get("device") or {}).get("decoded") or last.get("decoded")
                    else:
                        decoded_for_obs = None
                    obs_states.append({
                        "sender_id": sid,
                        "kind": "inactive_sender",
                        "label": s.get("label", "") or "",
                        "last_seen": last.get("ts") if last else None,
                        "rssi_dbm": last.get("rssi_dbm") if last else None,
                        "decoded": decoded_for_obs,
                        "rorg": last.get("rorg") if last else None,
                    })
                if obs_states:
                    c["observer_states"] = obs_states
            out.append(dd)
        return out

    @app.get("/api/state/{sender_id}")
    async def get_state(sender_id: str) -> dict:
        s = srv.log_buffer.last_state.get(sender_id.upper())
        if not s:
            raise HTTPException(404, "no telegram for this sender yet")
        return s

    def _forget_observed_for_device(device: Device) -> None:
        """M62: alle senders.sender_id des Devices aus observed_senders entfernen.
        Verhindert Doppel-Zaehlung in der Gateway-Block-Uebersicht."""
        if not srv.observed_senders:
            return
        any_removed = False
        for ch in device.channels:
            for s in ch.senders:
                if srv.observed_senders.forget(s.sender_id):
                    any_removed = True
        if any_removed:
            srv.observed_senders.save(force=True)

    @app.post("/api/devices", status_code=201)
    async def create_device(payload: DeviceCreate) -> dict:
        try:
            d = Device(**payload.model_dump())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, _format_validation_error(exc))
        srv.devices.upsert(d)
        srv.devices.save(srv.config_dir / "devices.yaml")
        _forget_observed_for_device(d)
        return d.model_dump(mode="json")

    @app.put("/api/devices/{device_id}")
    async def update_device(device_id: str, payload: DeviceCreate) -> dict:
        if payload.device_id != device_id:
            raise HTTPException(400, "device_id mismatch in body vs URL")
        try:
            d = Device(**payload.model_dump())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, _format_validation_error(exc))
        srv.devices.upsert(d)
        srv.devices.save(srv.config_dir / "devices.yaml")
        _forget_observed_for_device(d)
        return d.model_dump(mode="json")

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str) -> dict:
        ok = srv.devices.remove(device_id)
        if not ok:
            raise HTTPException(404, "device not found")
        srv.devices.save(srv.config_dir / "devices.yaml")
        return {"ok": True, "removed": device_id}

    @app.post("/api/devices/{device_id}/channels/{channel_id}/switch-eep")
    async def switch_channel_eep(
        device_id: str,
        channel_id: str,
        payload: dict[str, Any],
    ) -> dict:
        """
        Manueller EEP-Wechsel fuer einen Channel (Strom <-> Gas <-> Wasser).
        Equivalent zum Auto-EEP-Wechsel via Lerntelegramm (M47), aber per UI
        ausloesbar — fuer den Fall, dass der Anwender im PCT14 den Zaehlertyp
        am F3Z14D-Eingang umstellt, aber keine Lerntelegramme erzeugen will
        oder kann.

        Body: {"eep": "A5-12-02"}
        Akzeptiert nur A5-12-01/02/03. Mappt meta.field automatisch
        (energy_kwh<->volume_m3, current_w<->flow_m3h/flow_l_min). Loescht
        ein etwaiges meta.unit_override, weil die Default-Einheit jetzt
        eine andere ist.
        """
        from ..pipeline import remap_a5_12_field

        new_eep = (payload or {}).get("eep") or ""
        if new_eep not in ("A5-12-01", "A5-12-02", "A5-12-03"):
            raise HTTPException(
                400,
                "Nur A5-12-01 (Strom), A5-12-02 (Gas) oder A5-12-03 (Wasser) erlaubt",
            )

        device = next(
            (d for d in srv.devices.all() if d.device_id == device_id),
            None,
        )
        if not device:
            raise HTTPException(404, "Geraet nicht gefunden")
        ch = next(
            (c for c in device.channels if c.channel_id == channel_id),
            None,
        )
        if not ch:
            raise HTTPException(404, "Channel nicht gefunden")

        old_eep = ch.eep
        if not (old_eep or "").startswith("A5-12-"):
            raise HTTPException(
                400,
                f"Channel-EEP {old_eep!r} gehoert nicht zur A5-12-Familie",
            )
        if old_eep == new_eep:
            return {"ok": True, "unchanged": True, "eep": new_eep}

        ch.eep = new_eep
        if ch.meta and "field" in ch.meta:
            new_field = remap_a5_12_field(ch.meta["field"], new_eep)
            if new_field:
                ch.meta["field"] = new_field
        # unit_override gilt fuer das alte EEP — bei Wechsel verwerfen, damit
        # die Default-Einheit des neuen Profils greift.
        if ch.meta and "unit_override" in ch.meta:
            del ch.meta["unit_override"]

        srv.devices.save(srv.config_dir / "devices.yaml")
        log.info(
            "Manueller EEP-Wechsel: %s/%s %s -> %s (field=%s)",
            device_id, channel_id, old_eep, new_eep,
            (ch.meta or {}).get("field"),
        )
        return {
            "ok": True,
            "old_eep": old_eep,
            "eep": new_eep,
            "field": (ch.meta or {}).get("field"),
        }

    @app.delete("/api/devices/{device_id}/channels/{channel_id}")
    async def delete_channel(device_id: str, channel_id: str) -> dict:
        """
        Einzelnen Channel aus einem Device entfernen — fuer Karteileichen
        (z.B. 3.1_serial_number bei F3Z14D nach altem Import-Schema).
        """
        device = next(
            (d for d in srv.devices.all() if d.device_id == device_id),
            None,
        )
        if not device:
            raise HTTPException(404, "Geraet nicht gefunden")
        before = len(device.channels)
        device.channels = [c for c in device.channels if c.channel_id != channel_id]
        if len(device.channels) == before:
            raise HTTPException(404, "Channel nicht gefunden")
        srv.devices.save(srv.config_dir / "devices.yaml")
        log.info("Channel geloescht: %s/%s", device_id, channel_id)
        return {"ok": True, "removed": channel_id}

    @app.post("/api/devices/{device_id}/channels/{channel_id}/send-teach-in")
    async def send_teach_in(
        device_id: str,
        channel_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        """
        Sendet ein 4BS-Lerntelegramm an einen Aktor (M57 + M59).

        Body (optional): {"sender_id": "FF810055"} — Sender-ID die angelernt
        werden soll. MUSS in channel.senders existieren. Ohne Angabe: nutzt
        den aktiven SenderBinding (channel.active_sender).
        """
        sender_id = None
        if payload and isinstance(payload, dict):
            sender_id = payload.get("sender_id") or None
        try:
            return await srv.tx_router.send_teach_in(
                device_id, channel_id, sender_id_hex=sender_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Lerntelegramm fehlgeschlagen")
            raise HTTPException(500, f"Sendefehler: {exc}")

    @app.post("/api/devices/{device_id}/channels/{channel_id}/test")
    async def test_channel(
        device_id: str,
        channel_id: str,
        command: dict[str, Any],
    ) -> dict:
        await srv.tx_router.handle_command(device_id, channel_id, command)
        # Aktor-State (z.B. gemerkter Dimmwert) SOFORT persistieren, damit er
        # einen Container-Restart/Deploy ueberlebt. handle_command markiert nur
        # dirty; ohne expliziten save() ging der Memory beim Neustart verloren
        # -> "An" sprang dann auf 100% statt auf den gemerkten Wert.
        if srv.state_store:
            try:
                srv.state_store.save()
            except Exception:  # noqa: BLE001
                log.warning("state_store.save nach Test-Befehl fehlgeschlagen")
        return {"ok": True, "sent": command}

    @app.post("/api/pairing/reman")
    async def pairing_reman(payload: dict[str, Any]) -> dict:
        """
        M83: OPUS-BRiDGE-Pairing via Remote Management.

        Body: {security_code, sender_id, gateway?, actor_id?}
        Sendet UNLOCK + LOCK an den Aktor; der lernt die sender_id.
        """
        code = (payload.get("security_code") or "").strip()
        sender = (payload.get("sender_id") or "").strip()
        gateway = payload.get("gateway") or None
        actor = (payload.get("actor_id") or "").strip() or None
        eep = (payload.get("eep") or "D2-01-01").strip()
        if not code or not sender:
            raise HTTPException(400, "security_code und sender_id erforderlich")
        try:
            return await srv.tx_router.send_reman_pairing(
                code, sender, gateway=gateway, actor_id_hex=actor, eep=eep,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("ReMan-Pairing fehlgeschlagen")
            raise HTTPException(500, f"Pairing-Sendefehler: {exc}")

    # ---- ActorState + Eichfahrt ----

    @app.get("/api/actor-state/{device_id}/{channel_id}")
    async def get_actor_state(device_id: str, channel_id: str) -> dict:
        if not srv.state_store:
            raise HTTPException(503, "state-store deaktiviert")
        st = srv.state_store.get(device_id, channel_id)
        d = st.to_dict()
        if st.moving:
            d["position_percent"] = round(estimate_position_now(st), 1)
        return d

    @app.post("/api/actor-state/{device_id}/{channel_id}/calibration")
    async def set_calibration(
        device_id: str, channel_id: str, payload: dict[str, Any],
    ) -> dict:
        """
        Setzt die Eichfahrt-Laufzeit(en). Getrennt nach Richtung:
          travel_time_s    = Senken (0→100)
          travel_time_up_s = Heben  (100→0), optional — 0/weggelassen = wie Senken
        Mindestens travel_time_s muss gesetzt werden.
        """
        if not srv.state_store:
            raise HTTPException(503, "state-store deaktiviert")
        st = srv.state_store.get(device_id, channel_id)
        travel = float(payload.get("travel_time_s", 0))
        if travel < 1.0 or travel > 300.0:
            raise HTTPException(400, "travel_time_s muss 1-300 Sekunden sein")
        st.travel_time_s = travel
        # Heben-Zeit ist optional. 0 (oder fehlend) bedeutet "wie Senken".
        if "travel_time_up_s" in payload and payload["travel_time_up_s"] not in (None, ""):
            travel_up = float(payload["travel_time_up_s"])
            if travel_up != 0.0 and (travel_up < 1.0 or travel_up > 300.0):
                raise HTTPException(400, "travel_time_up_s muss 0 oder 1-300 Sekunden sein")
            st.travel_time_up_s = travel_up
        st.calibrated = True
        srv.state_store.mark_dirty()
        srv.state_store.save()
        log.info("Eichfahrt %s/%s: Senken %.1fs / Heben %.1fs gespeichert",
                 device_id, channel_id, st.travel_time_s, st.travel_time_up_s)
        return {
            "ok": True,
            "travel_time_s": st.travel_time_s,
            "travel_time_up_s": st.travel_time_up_s,
        }

    @app.post("/api/actor-state/{device_id}/{channel_id}/set-position")
    async def set_known_position(
        device_id: str, channel_id: str, payload: dict[str, Any],
    ) -> dict:
        """User bestätigt manuell: Rolladen ist bei X% (z.B. nach Eichfahrt)."""
        if not srv.state_store:
            raise HTTPException(503, "state-store deaktiviert")
        pos = float(payload.get("position_percent", 0))
        pos = max(0.0, min(100.0, pos))
        st = srv.state_store.get(device_id, channel_id)
        st.position_percent = pos
        st.moving = None
        st.moving_started_at = None
        st.moving_target = None
        srv.state_store.mark_dirty()
        srv.state_store.save()
        return {"ok": True, "position_percent": pos}

    # -------- Products --------

    @app.get("/api/products")
    async def list_products(manufacturer: str | None = None) -> dict:
        if manufacturer:
            items = srv.products.models_for(manufacturer)
            return {
                "manufacturer": manufacturer,
                "models": [_product_to_json(p) for p in items],
            }
        return {
            "manufacturers": srv.products.manufacturers(),
            "total": len(srv.products),
        }

    @app.get("/api/products/scaffold-channels")
    async def scaffold_channels(
        manufacturer: str,
        model: str,
        fam14_base_id: str | None = None,
        bus_start_address: int | None = None,
        fts14em_group: int | None = None,
        fts14em_subdial_pos: int | None = None,
        fts14em_mode: str = "UT",
    ) -> dict:
        """
        Erzeugt die Channel-Vorbelegung fuer ein Produkt nach gleicher Logik
        wie der CSV-Importer (M53). Damit liefert das manuelle „Geraet
        hinzufuegen" exakt die gleichen Channels wie ein Re-Import:
          - F3Z14D -> 6 Channels (3 Eingaenge x Energie/Leistung)
          - DSZ14DRS -> 5 Channels (Tarif 0/1 x Energie/Leistung + Seriennummer)
          - FSR14-4x -> 4 Schaltkanaele

        M72: Optional fam14_base_id + bus_start_address — bei FAM14-Bus-Modulen
        werden die RX-Sender-IDs aus PCT14-Adressen generiert
        und in channel.enocean_id eingetragen.

        M76: Encoding per product.fam14_addressing ("bcd"|"hex"):
          - Dimmer/Zaehler: BCD (Adr 26 dez → 0x26)
          - Schaltrelais/Rolladen: HEX (Adr 28 dez → 0x1C)
        """
        from ..device_scaffold import CsvRow, convert_rows_to_devices

        info = srv.products.lookup(manufacturer, model)
        if not info:
            raise HTTPException(404, f"Modell {manufacturer}/{model} nicht in der DB")
        addressing = info.fam14_addressing

        # rocker_split-Sonderpfad: Wippentaster (FT55/FT4/FT4F) und Eingangs-
        # interfaces (FSM61-UC) — eine RX-ID, zwei Channels mit Filter nach
        # meta.tele_channel='A'/'B'. Channel-Label haengt vom Geraetetyp ab:
        # echte Wippen-Schalter -> "Wippe A/B", Eingangs-Interface -> "Eingang A/B".
        if info.multi_channel_kind == "rocker_split":
            channels_out = []
            base_label = "Wippe" if "FT" in model.upper() else "Eingang"
            for label_suffix, side, ch_id in [
                (f"{base_label} A (links)",  "A", "A"),
                (f"{base_label} B (rechts)", "B", "B"),
            ]:
                label = label_suffix
                channels_out.append({
                    "channel_id": ch_id,
                    "name": label,
                    "enocean_id": None,  # User traegt RX-ID einmal ein
                    "eep": info.eep or "F6-02-01",
                    "direction": "rx",
                    "floor": "",
                    "room": "",
                    "light_group": "",
                    "light_role": "",
                    "senders": [],
                    "via_gateway": None,
                    "learned_pair_id": None,
                    "controls": [],
                    "observers": [],
                    "meta": {
                        "tele_channel": side,
                    },
                })
            return {
                "device_name": model,
                "manufacturer": manufacturer,
                "model": model,
                "eep": info.eep,
                "fam14_bus": False,
                "fam14_addressing": "",
                "rocker_split": True,
                "rx_preview": [],
                "channels": channels_out,
            }

        # FTS14EM-Sonderpfad: Drehschalter-Konfig statt FAM14-Bus-Adresse.
        # Channels werden direkt generiert (keine FAM14-Block-IDs), 10 (UT)
        # oder 5 (RT). IDs gehen nicht ueber Funk, sondern dienen als
        # Observer-IDs an Aktor-Channels.
        is_fts14em = (
            manufacturer.strip().lower() == "eltako"
            and model.strip().upper() == "FTS14EM"
        )
        if is_fts14em and fts14em_group is not None and fts14em_subdial_pos is not None:
            mode = (fts14em_mode or "UT").upper()
            if mode not in ("UT", "RT"):
                raise HTTPException(400, f"fts14em_mode muss UT oder RT sein, war: {mode}")
            input_positions = {
                0x70: "rechts oben",
                0x50: "rechts unten",
                0x30: "links oben",
                0x10: "links unten",
            }
            channels_out: list[dict] = []
            rx_preview = []
            if mode == "UT":
                for i in range(1, 11):
                    sid = fts14em_sender_id(
                        fts14em_group, fts14em_subdial_pos, i, "UT",
                    )
                    db = fts14em_data_byte(i, "UT")
                    pos = input_positions.get(db or 0, "")
                    channels_out.append({
                        "channel_id": str(i),
                        "name": f"E{i} {pos}".strip(),
                        "enocean_id": sid,
                        "eep": "F6-02-01",
                        "direction": "rx",
                        "floor": "",
                        "room": "",
                        "light_group": "",
                        "light_role": "",
                        "senders": [],
                        "via_gateway": None,
                        "learned_pair_id": None,
                        "controls": [],
                        "observers": [],
                        "meta": {
                            "fts14em_input": i,
                            "fts14em_data_byte": f"0x{db:02X}" if db is not None else None,
                            "fts14em_mode": "UT",
                        },
                    })
                    if sid:
                        rx_preview.append({"index": i - 1, "input": i, "sender_id": sid})
            else:  # RT
                rt_pairs = [(1, 2, "Wippe rechts (E1+E2)"),
                            (3, 4, "Wippe links  (E3+E4)"),
                            (5, 6, "Wippe rechts (E5+E6)"),
                            (7, 8, "Wippe links  (E7+E8)"),
                            (9, 10, "Wippe rechts (E9+E10)")]
                for idx, (a, b, label) in enumerate(rt_pairs, start=1):
                    sid = fts14em_sender_id(
                        fts14em_group, fts14em_subdial_pos, b, "RT",
                    )
                    db = fts14em_data_byte(b, "RT")
                    channels_out.append({
                        "channel_id": str(idx),
                        "name": label,
                        "enocean_id": sid,
                        "eep": "F6-02-01",
                        "direction": "rx",
                        "floor": "",
                        "room": "",
                        "light_group": "",
                        "light_role": "",
                        "senders": [],
                        "via_gateway": None,
                        "learned_pair_id": None,
                        "controls": [],
                        "observers": [],
                        "meta": {
                            "fts14em_input_pair": f"E{a}+E{b}",
                            "fts14em_data_byte": f"0x{db:02X}" if db is not None else None,
                            "fts14em_mode": "RT",
                        },
                    })
                    if sid:
                        rx_preview.append({"index": idx - 1, "input": b, "sender_id": sid})

            return {
                "device_name": f"FTS14EM Gruppe {fts14em_group} S{fts14em_subdial_pos} {mode}",
                "manufacturer": manufacturer,
                "model": model,
                "eep": "F6-02-01",
                "fam14_bus": False,
                "fam14_addressing": "",
                "fts14em": {
                    "group": fts14em_group,
                    "subdial_pos": fts14em_subdial_pos,
                    "mode": mode,
                },
                "rx_preview": rx_preview,
                "channels": channels_out,
            }

        # Bauen ein minimales Pseudo-CSV-Setup: Group-Header + 1 Stub-Datenrow.
        # Der Importer erkennt Stub und triggert seinen Auto-Gen-Pfad fuer
        # multi_channel_kind=separate_ids/tariff_byte und multi-Hardware-Channel.
        rows = [
            CsvRow(is_group_header=True, manufacturer=manufacturer, model=model),
            CsvRow(
                is_data=True,
                manufacturer=manufacturer,
                model=model,
                name=model or "Geraet",
                room="",
                address_raw="nicht angelernt",
            ),
        ]
        new_devices, _ = convert_rows_to_devices(rows, srv.products)
        if not new_devices:
            return {"channels": []}
        d = new_devices[0]
        channels_json = [c.model_dump(mode="json") for c in d.channels]

        # M72: FAM14-Bus-Auto-IDs eintragen
        rx_preview: list[dict] = []
        if (info.fam14_bus and fam14_base_id and bus_start_address is not None):
            # Anzahl der Bus-Adressen die das Modul belegt = channel_count
            # (z.B. FDG14=16, F3Z14D=3, FSR14-4x=4, DSZ14DRS=2)
            n_addresses = info.channel_count
            # Für tariff_byte (DSZ14DRS) gibt es trotz 2 Tarifen NUR 1 Sender-ID.
            if info.multi_channel_kind == "tariff_byte":
                n_addresses = 1
            for i in range(n_addresses):
                addr = bus_start_address + i
                sid = fam14_address_to_sender_id(fam14_base_id, addr, addressing)
                if sid:
                    rx_preview.append({"index": i, "address": addr, "sender_id": sid})

            # Channels nach derselben Aufzählung mit enocean_id befüllen
            if info.multi_channel_kind == "separate_ids":
                # F3Z14D-Style: pro Bus-Adresse ein "Sub-Modul" mit
                # mehreren Channels (energy_kwh + current_w pro Eingang).
                # Channel-IDs sind "1_energy_kwh", "1_current_w",
                # "2_energy_kwh", ... → Index = (channel_id.split('_')[0] - 1)
                for ch in channels_json:
                    cid = ch.get("channel_id", "")
                    prefix = cid.split("_", 1)[0]
                    try:
                        idx = int(prefix) - 1
                    except ValueError:
                        continue
                    if 0 <= idx < n_addresses:
                        sid = fam14_address_to_sender_id(
                            fam14_base_id, bus_start_address + idx, addressing,
                        )
                        if sid:
                            ch["enocean_id"] = sid
            elif info.multi_channel_kind == "tariff_byte":
                # DSZ14DRS: nur EINE Sender-ID fuer alle Tarif/Feld-Channels
                sid = fam14_address_to_sender_id(fam14_base_id, bus_start_address)
                for ch in channels_json:
                    if sid:
                        ch["enocean_id"] = sid
            else:
                # Standard: pro Channel eine Bus-Adresse (FSR14-2x, FSR14-4x,
                # FDG14, FUD14 etc.). Channel-IDs sind "1.1", "1.2", ...
                for i, ch in enumerate(channels_json):
                    sid = fam14_address_to_sender_id(
                        fam14_base_id, bus_start_address + i, addressing,
                    )
                    if sid:
                        ch["enocean_id"] = sid

        # Multi-Funktions-Geraete: die ProductDB haelt pro Modell mehrere
        # Funktions-Zeilen (thanos: Sensor+Kontakt+Tasten; SRC-ADO BCS:
        # Valve+Jalousie+Dimmer+Switch). convert hat nur die Primaerfunktion
        # aufgebaut — die restlichen Funktionen als zusaetzliche Channels
        # anhaengen, damit das manuelle Hinzufuegen das ganze Geraet abbildet.
        funcs = srv.products.functions(manufacturer, model)
        if len(funcs) > 1:
            channels_json.extend(
                _channels_for_extra_functions(funcs[1:], channels_json)
            )

        return {
            "device_name": d.name,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "eep": info.eep,
            "fam14_bus": info.fam14_bus,
            "fam14_addressing": addressing,
            "rx_preview": rx_preview,
            "channels": channels_json,
        }

    @app.get("/api/products/search")
    async def search_products(q: str = "", limit: int = 50) -> list[dict]:
        """
        Volltext-Autocomplete: liefert Treffer aus allen Geraeten.
        Format: {manufacturer, model, eep, channel_count, ui_kind, channel_name_template}
        """
        q = (q or "").lower().strip()
        out: list[dict] = []
        for mfr in srv.products.manufacturers():
            for p in srv.products.models_for(mfr):
                if not q:
                    out.append(_product_to_json(p))
                    continue
                hay = (
                    (p.manufacturer or "").lower()
                    + " " + (p.model or "").lower()
                    + " " + (p.description or "").lower()
                    + " " + (p.device_type or "").lower()
                )
                if q in hay:
                    out.append(_product_to_json(p))
                if len(out) >= limit:
                    return out
        return out

    # -------- Log + Diagnostics --------

    @app.get("/api/log/recent")
    async def recent_log(limit: int = 50) -> list[dict]:
        return srv.log_buffer.recent(limit=limit)

    @app.get("/api/diagnostics")
    async def diagnostics() -> dict:
        return {
            "cascade": {
                "received_total": srv.cascade.stats.received_total,
                "duplicates_dropped": srv.cascade.stats.duplicates_dropped,
                "passed_through": srv.cascade.stats.passed_through,
                "dedup_window_ms": srv.cfg.cascade.dedup_window_ms,
            },
            "rssi_table": srv.cascade.rssi.snapshot(),
            "gateways": srv.manager.status_summary(),
        }

    @app.websocket("/api/ws/telegrams")
    async def ws_telegrams(websocket: WebSocket) -> None:
        await websocket.accept()
        srv.log_buffer.add_ws(websocket)
        try:
            # Send initial buffer
            for entry in srv.log_buffer.recent(50):
                await websocket.send_json(entry)
            # Keep alive
            while True:
                await websocket.receive_text()  # client ping
        except WebSocketDisconnect:
            pass
        finally:
            srv.log_buffer.remove_ws(websocket)

    # -------- Teach-In --------

    @app.post("/api/teach/start")
    async def teach_start(payload: dict | None = None) -> dict:
        only_lrn = True
        if isinstance(payload, dict):
            only_lrn = bool(payload.get("only_lrn", True))
        srv.teach.start(only_lrn=only_lrn)
        return {"active": True, "only_lrn": only_lrn}

    @app.post("/api/teach/stop")
    async def teach_stop() -> dict:
        srv.teach.stop()
        return {"active": False}

    @app.get("/api/teach/captured")
    async def teach_captured() -> dict:
        items = sorted(
            srv.teach.captured.values(),
            key=lambda x: x["last_seen"],
            reverse=True,
        )
        return {
            "active": srv.teach.active,
            "only_lrn": srv.teach.only_lrn,
            "started_at": srv.teach.started_at,
            "captured": items,
        }

    return app
