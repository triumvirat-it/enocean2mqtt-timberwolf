# Credits & Quellen

Dieses Projekt ist ein Community-Tool für Timberwolf-Server-Nutzer und steht auf
den Schultern vieler Open-Source-Projekte und öffentlich zugänglicher
Hersteller-Dokumentation.

## Open-Source-Komponenten

| Projekt | Lizenz | Wofür |
|---------|--------|------|
| [python-enocean](https://github.com/kipe/enocean) | MIT | Basis-Inspiration für ESP3-Parser und EEP-Konzepte |
| [asyncio-mqtt / aiomqtt](https://github.com/sbtinstruments/aiomqtt) | BSD-3 | MQTT-Client (asyncio) |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Konfigurations-Validierung |
| [FastAPI](https://github.com/tiangolo/fastapi) | MIT | Web-UI-Backend |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3 | ASGI-Server |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | YAML-Parsing |

## EEP-Wissen & Geräte-Datenbank

Die Datei `app/data/products.yaml` enthält eine Liste von ~630 EnOcean-Geräten
mit ihren Standard-EEP-Profilen (Hersteller, Modell, ComProfile, Geräte-Typ,
Konfigurations-Hints). Diese Information ist **Fakten-Wissen** aus folgenden
Quellen:

- **EnOcean Alliance** — [EEP-Spezifikation](https://www.enocean-alliance.org/specifications/) (öffentlich)
- **Eltako GmbH** — [Funkbus-Datenblätter](https://www.eltako.com/de/downloads.html) (öffentlich auf Hersteller-Website)
- **Jäger Direkt (OPUS)** — [Datenblätter](https://www.jaeger-direkt.com)
- **Thermokon** — [Datenblätter](https://www.thermokon.de)
- **AFRISO** — [Datenblätter](https://www.afriso.com)
- **FHEM** EnOcean-Modul-Code (GPL, Referenz für Eltako-Sonderfälle)
- **Home Assistant** EnOcean-Component (Apache-2.0, Inspirationsquelle)

Verifizierung erfolgt mit eigenen EnOcean LAN-Gateways gegen reale Geräte.
Keine Übernahme aus geschützten Drittsoftware-Datenbanken.

**Du hast ein Gerät das fehlt?** Lege es in `data/products_custom.yaml` an
(siehe Format-Beispiel in `app/data/products.yaml`) oder mache einen
Pull Request gegen die Hauptdatenbank.

## Inspirations-Quellen (UX/Design)

- **Timberwolf Server** (Elaborated Networks GmbH) — UX-Patterns wie
  Tag-System, Tabellen-Filter, Status-Ampeln

## Visuelles Design

UI-Design basiert auf dem Triumvirat-IT-Brand-System
(`brand/tokens.json`) — wärmere Farben, abgerundete Formen,
Schriften Space Grotesk + Inter.

## Hardware-Tests / Verifikation

Entwicklung und Tests mit:
- EnOcean LAN-Gateways (TCM310-LAN)
- Timberwolf Server 3500 (ARM64, BCM2711)
- Real-World Anlage mit ~150 EnOcean-Geräten verschiedener Hersteller

## Mitwirkende

- **Triumvirat-IT** (Hauptentwicklung)
- Beiträge und Feedback aus dem
  [Elabnet-Forum](https://forum.timberwolf.io/) Community
