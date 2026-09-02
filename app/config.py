"""
Konfig-Schemas (Pydantic). Quelle: gateways.yaml + devices.yaml unter CONFIG_DIR (Default /data).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, IPvAnyAddress, field_validator


class GatewayConfig(BaseModel):
    """Eine einzelne EnOcean-Gateway-Definition."""

    name: str = Field(..., description="Eindeutiger Name, z.B. 'LAN Gateway1'")
    type: Literal["TCM310-LAN", "TCM515-LAN"] = "TCM310-LAN"
    host: str = Field(..., description="IP-Adresse oder Hostname")
    port: int = Field(5000, ge=1, le=65535)
    enabled: bool = True

    base_id: str | None = Field(
        None,
        description="Basis-ID des GW als 8-stelliger Hex-String (z.B. 'FF810000'). "
        "Wird beim Connect automatisch ausgelesen wenn None.",
        pattern=r"^[0-9A-Fa-f]{8}$",
    )
    rssi_filter: int | None = Field(
        None, ge=-120, le=0, description="Minimaler RSSI (dBm), z.B. -85. None = aus."
    )
    repeater_level: int = Field(0, ge=0, le=2, description="0=aus, 1=1-Level, 2=2-Level")

    floor_assignments: list[str] = Field(
        default_factory=list,
        description="Geschosse/Räume, die diesem GW zugeordnet sind (für Sender-Fallback).",
    )

    @field_validator("base_id")
    @classmethod
    def _upper_base_id(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class CascadeConfig(BaseModel):
    """Globale Meldungs-Kaskadierung."""

    dedup_window_ms: int = Field(
        200, ge=0, le=5000, description="Zeitfenster für Empfangs-Dedup (ms)."
    )
    send_strategy: Literal["best_rssi", "all_gateways", "floor_only"] = "best_rssi"
    rssi_history_size: int = Field(
        20, ge=1, le=200, description="Wie viele letzte RSSI-Werte pro (GW, Sender) für Mittelung."
    )


class MQTTConfig(BaseModel):
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "enocean2mqtt-timberwolf"
    base_topic: str = "enocean"
    qos: int = Field(1, ge=0, le=2)
    retain_state: bool = True


class WebUIConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1, le=65535)
    auth_user: str | None = None
    auth_password: str | None = None


class DefaultsConfig(BaseModel):
    """Globale Default-Einstellungen (editierbar auf der Einstellungsseite)."""

    ptm_on_press: Literal["I", "0"] = Field(
        "I",
        description="Welche Wippen-Haelfte schaltet EIN: 'I' = oben (AI/BI), "
        "'0' = unten (A0/B0). Pro Kanal via meta.ptm_on_press ueberschreibbar.",
    )
    energy_max_jump_kwh: float = Field(
        1000.0,
        description="Maximal plausibler Sprung eines energy_kwh-Zaehlerstands "
        "zwischen zwei Telegrammen. Groessere Spruenge nach oben gelten als "
        "Funk-/Bus-Stoerung und werden verworfen (sonst wuerde ein einzelner "
        "Ausreisser den Monotonie-Filter dauerhaft 'vergiften'). 0 = aus.",
    )
    energy_max_kw: float = Field(
        50.0,
        description="Anschlussleistung in kW, aus der die ZEITBEZOGENE "
        "Sprunggrenze berechnet wird: zwischen zwei Telegrammen kann ein "
        "Zaehler hoechstens (kW x verstrichene Stunden) zulegen. Faengt "
        "Ausreisser, die unter der festen kWh-Grenze liegen — z.B. +353 kWh "
        "innerhalb von 10 Minuten (Vorfall 02.09.2026: der Bueromzaehler war "
        "danach tagelang blockiert). 0 = aus, dann greift nur energy_max_jump_kwh.",
    )
    energy_entsperren_nach: int = Field(
        3,
        description="Selbstheilung des Monotonie-Schutzes: So viele "
        "aufeinanderfolgende, untereinander AUFSTEIGENDE Werte unterhalb des "
        "gespeicherten Standes gelten als Beweis, dass der gespeicherte Stand "
        "der Ausreisser war — er wird dann verworfen und der Zaehler laeuft "
        "weiter. Ohne das bleibt ein Zaehler bis zum Neustart blockiert. "
        "0 = aus.",
    )
    power_max_w: float = Field(
        50000.0,
        description="Obergrenze fuer Momentanleistung (current_w) in Watt. "
        "Hoehere Werte gelten als Funk-/Bus-Stoerung und werden verworfen, "
        "statt im UI-Feld-Cache einzufrieren. Grosszuegig fuer dicke Verbraucher "
        "(Waermepumpe, Elektrolyse). 0 = aus.",
    )
    flow_max_m3h: float = Field(
        100.0,
        description="Obergrenze fuer Gas-Durchfluss (flow_m3h) in m^3/h. "
        "Groessere Werte gelten als Stoerung und werden verworfen. 0 = aus.",
    )
    flow_max_l_min: float = Field(
        1000.0,
        description="Obergrenze fuer Wasser-Durchfluss (flow_l_min) in L/min. "
        "Groessere Werte gelten als Stoerung und werden verworfen. 0 = aus.",
    )
    telegram_audit_enabled: bool = Field(
        True,
        description="Voll-Mitschnitt ALLER empfangenen Telegramme nach "
        "telegram_audit.log (im Named Volume, taeglich rotierend, gzip-Archiv, "
        "~6 Wochen Aufbewahrung). Fuer die nachtraegliche Fehlersuche bei "
        "unplausiblen Werten, deren ausloesendes Telegramm sonst laengst aus "
        "dem RAM-Live-Log gerollt ist. false = aus.",
    )


class AppConfig(BaseModel):
    """Wurzel-Config. Wird aus gateways.yaml gelesen."""

    gateways: list[GatewayConfig] = Field(default_factory=list)
    cascade: CascadeConfig = Field(default_factory=CascadeConfig)
    mqtt: MQTTConfig = Field(default_factory=MQTTConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    log_level: str = "INFO"

    @classmethod
    def load(cls, config_dir: str | Path | None = None) -> "AppConfig":
        config_dir = Path(config_dir or os.environ.get("CONFIG_DIR", "/data"))
        gateways_file = config_dir / "gateways.yaml"

        if not gateways_file.exists():
            raise FileNotFoundError(
                f"Keine Konfig gefunden unter {gateways_file}. "
                f"Lege eine an (siehe gateways.example.yaml)."
            )

        with gateways_file.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        return cls.model_validate(raw)

    def save(self, config_dir: str | Path | None = None) -> None:
        """Schreibt die aktuelle Konfig nach gateways.yaml zurueck."""
        config_dir = Path(config_dir or os.environ.get("CONFIG_DIR", "/data"))
        config_dir.mkdir(parents=True, exist_ok=True)
        gateways_file = config_dir / "gateways.yaml"
        data = self.model_dump(mode="json", exclude_none=False)
        tmp = gateways_file.with_suffix(gateways_file.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        tmp.replace(gateways_file)
