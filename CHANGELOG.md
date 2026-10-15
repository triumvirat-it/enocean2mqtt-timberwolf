# Changelog

Alle Änderungen, die man beim Einspielen kennen sollte. Neueste oben.
**Bitte prüfen** markiert Änderungen am Verhalten, auf die sich eine
bestehende Automatik verlassen haben könnte.

Was (noch) nicht geht: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
Welche Geräte erprobt sind: [docs/Kompatibilitaet.md](docs/Kompatibilitaet.md).

---

## Unveröffentlicht

### Neu
- `CHANGELOG.md`, `KNOWN_LIMITATIONS.md` und `docs/Kompatibilitaet.md`
  (umgesetzt vs. mit echter Hardware erprobt, gegen den Code getestet).
- `release.ps1`: Veröffentlichung nur noch über eine Positivliste
  (`release-liste.txt`) mit Prüfung auf Zugangsdaten und anlagenspezifische
  Daten vor jedem Push und Release. Siehe [docs/Veroeffentlichen.md](docs/Veroeffentlichen.md).

---

## 0.9.173 — 2026-10-15

### Neu
- **Heizungsregelung**: Die Bridge spielt das Raumbedienteil für
  Fußbodenheizungs-Aktoren (Thermokon SRC-DO8 über A5-10-06, Eltako FHK61 /
  OPUS GN-A-U230V-HKR-1 über A5-10-03). Wohnbereiche mit Zeitzonen und
  Rückfall-Sollwert, Räume mit mehreren Fühlern, Sollwertrad, Tür-/Fenster-
  kontakte, Betriebsart-Schalter, Dauerabsenkung, Aktortest. Neuer Reiter
  **Heizung**; Beschreibung in [docs/Heizungsregelung.md](docs/Heizungsregelung.md).
- Encoder **A5-10-03** für Eltako-Heizungsaktoren (absolute Solltemperatur
  8…30 °C, DB0 = `0x0F` wie echte Raumbedienteile).
- **Verwurf-Protokoll** (`reject_audit.log`, 45 Tage, `GET /api/reject-audit`):
  jedes verworfene Feld mit Grund, Roh-Payload und letztem gutem Wert.
- **Telegramm-Mitschrift** aller empfangenen Telegramme
  (`telegram_audit.log`, rund 6 Wochen, `GET /api/telegram-audit`).
- `GET /api/health/stale`: Kanäle, die keinen frischen Messwert mehr liefern.
- Mobile-Suche über Name, Raum und IDs.

### Geändert
- **Bitte prüfen — Zähler:** Ausreißerschutz nach oben (`energy_max_jump_kwh`,
  Vorgabe 1000) und zeitbezogen (`energy_max_kw`, Vorgabe 50). Werte darüber
  werden verworfen statt übernommen. Drei aufsteigende Werte unter dem
  gespeicherten Stand entsperren einen hängenden Zähler
  (`energy_entsperren_nach`). Beides in den Einstellungen änderbar.
- **Bitte prüfen — Zähler:** Der letzte gültige Zählerstand überlebt jetzt den
  Neustart (`/data/energy_state.yaml`). Ein 0-Wert nach einem Update wird damit
  verworfen statt zur neuen Referenz.
- **Bitte prüfen — Zähler:** Auf `state`-Topics geht nur noch, was einen
  Messwert trägt. Diagnosemeldungen ohne Wert überschreiben den retained
  Zählerstand im Broker nicht mehr.
- A5-12-01: DB0 wird bitweise ausgewertet (Tarif, Datentyp, Divisor), statt
  gegen eine Liste erlaubter Bytes geprüft. Leistungswerte mit Divisor 100
  werden dadurch nicht mehr verworfen.
- A5-12-Momentanwerte: ein gesetztes oberstes Datenbyte gilt als Bitfehler
  und wird verworfen.
- **Bitte prüfen — Taster (PTM):** `on: true/false` kommt nur noch beim
  Drücken, nicht beim Loslassen, und richtet sich nach der Wippe (oben/unten).
  Neue Momentsignale `taster_ein` / `taster_aus` für flankenbasierte Logik
  (z. B. den „Triggered"-Baustein im Timberwolf).
- Pro-RORG-Routing, wenn eine Sender-ID Telegramme verschiedener EEPs schickt
  (z. B. Wippe und VLD am selben Gerät).
- Die Zeitzone der Heizung steht in der Konfiguration (`zeitzone`, Vorgabe
  `Europe/Berlin`); dafür ist `tzdata` neu in `requirements.txt`.

### Doku
- [docs/MQTT-Anbindung.md](docs/MQTT-Anbindung.md): Praxis-Anleitung für
  Timberwolf-Visu, Home Assistant, Node-RED.

---

## 0.9.141 — 2026-06-11

Erste öffentliche Veröffentlichung: mehrere LAN-Gateways parallel,
EEP-Decoding, MQTT-Publish und -Befehle, Senden und Anlernen, Rollladen-
Positionstracking, Web-UI.
