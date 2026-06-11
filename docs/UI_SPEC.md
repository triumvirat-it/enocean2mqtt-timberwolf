# Web-UI Spezifikation (V1)

Interne Design-Spezifikation, Mai 2026.

## Zielnutzer

**Primär:** Timberwolf-Besitzer ohne Entwickler-Hintergrund.
Konkretes Profil: Bankkaufmann mit Timberwolf, kann Portainer bedienen,
kann YAML/Konsole NICHT. Erwartet Web-UI mit Klicks und Knöpfen.

**Sekundär:** Power-User (Hba selbst). Bekommt nichts Eigenes — alle Features
müssen auch für ihn klickbar sein.

**Sprache:** Deutsch. Englisch als spätere Erweiterung.

## Inspirations-Quelle (UX-Patterns)

Timberwolf-eigene UI (1-Wire Manager etc.). Wir bauen analoge Patterns:
- Tag-Pills mit X zum Entfernen + Autocomplete
- Tabellen mit Spalten-Filter
- Status-Ampeln (grün/gelb/rot)
- Filter-Eingabe pro Spalte

## Visuelles Design (Triumvirat-IT Brand)

Quelle: `C:\xampp-8.2.12\xampp\htdocs\triumviratwebseite\brand\tokens.json`

**Grundton:** hell, warm, freundlich — bewusst nicht das kühle Tech-Blau-Grau.

**Farben:**
- Hintergrund: `#fefcf8` (warmes Off-White)
- Card-Hintergrund: `#fff3e6` (sanft getönt)
- Text primär: `#1a1f2c` (Anthrazit)
- Hilfstext: `#7a8294`
- Rahmen/Trennlinien: `#f0e4d6`
- Akzente: Korall `#ff6b6b` (Hauptakzent/CTAs), Gelb `#fcd34d` (Sekundär), Mint `#2dd4bf` (Tertiär, Status)
- Status: Erfolg/Online Mint, OK Hellgrün `#5eff9a`

**Schriften:**
- Headlines: Space Grotesk
- Body: Inter
- Mono (Sender-IDs, Topics, Hex): JetBrains Mono

**Form-Sprache:**
- Border-Radius: 8px klein, 18px Standard, 24px groß, 100px Pills
- Weiche Schatten (`0 16px 40px rgba(26,31,44,0.1)` für Cards)
- Animationen: 350ms, sanfte cubic-bezier

**Logo:** SVG-Marke + Wordmark („triumvirat" — erstes t klein) aus
`brand/logos/`. Header-Position oben links, dezent.

**Wirkung:** wärmer als Timberwolf-Standard, soll für Nicht-Entwickler
einladend und nicht-bedrohlich wirken.

## Marken-Positionierung

**Tool-Positionierung:** Community-Tool, kein Triumvirat-Produkt.
Triumvirat-Logo dezent als „bereitgestellt von" (z.B. im Footer und
About-Dialog), aber UI-Hauptfläche bleibt zurückhaltend.

**Credits prominent:**
Das Tool baut auf mehreren Open-Source-Projekten auf — diese müssen
sichtbar gewürdigt werden. Insbesondere:
- **python-enocean** (kipe, MIT) — Basis für ESP3 und EEP.xml
- **FHEM EnOcean-Module** (GPL) — Referenz für Eltako-Profile
- **Home Assistant EnOcean** — Inspirationsquelle für Patterns
- **EnOcean Alliance** — offene Spezifikationen

Diese Liste lebt im CREDITS.md im Repo und ist im UI-About-Dialog
verlinkt.

## Forum-Auftritt

Bei der späteren Veröffentlichung im Elabnet-Forum: als Community-
Initiative präsentieren („Hier ist ein Tool das ich für die Timberwolf-
EnOcean-Community gebaut habe"), nicht als Firmen-Produktvermarktung.
Triumvirat-IT als Author erwähnt, aber nicht im Vordergrund.

## Pflicht-Features V1

### 1. Setup-Wizard / Plus-Button-Konzept

Initialer Wizard wenn noch nichts konfiguriert ist. Danach jederzeit ein
**„+"-Button** zum Hinzufügen weiterer Gateways/Geräte. Ein zentrales Konzept.

Schritte beim Erst-Wizard:
1. Gateway hinzufügen (IP, Port, Test-Verbindung)
2. MQTT-Broker-Settings (Host/Port/User/Pass, Test-Verbindung)
3. Bestätigung „läuft" → ab in die Geräte-Liste

### 2. Anlern-Modus

Per Knopf „Neues Gerät anlernen":
- UI lauscht auf alle eingehenden Telegramme die noch zu keinem bekannten
  Gerät gehören
- Bei erstem unbekannten Telegramm: Vorschau anzeigen (Sender-ID, RORG, RSSI)
- **EEP-Vorschlag** automatisch (basierend auf Hersteller-DB oder
  EEP-Heuristik)
- User vergibt:
  - **Name** (Pflicht, freitext)
  - **Hersteller + Modell** (Dropdown mit Suche, aus Geräte-DB)
  - **Tags** (mehrere, frei wählbar, Autocomplete aus bisher genutzten)
  - **Verschlüsselt anlernen?** (Checkbox, pro Gerät, default: aus —
    nur aktivieren wenn das Gerät Secure unterstützt)
- Bei Multi-Channel-Geräten: nach Geräte-Anlern direkt Channels konfigurieren

### 3. Geräte-Liste (2-Ebenen wie Timberwolf)

**Geräte-Ebene:**
- Tabelle mit Spalten: Typ-Icon, Name, Sender-ID, Hersteller/Modell,
  Status-Ampel, Tags, Aktionen
- Spalten-Filter (auch nach Tags)
- Klick auf Gerät → Channels darunter ausklappen

**Channel-Ebene:**
- Pro Channel eigene Zeile: Channel-ID, Name, EEP, aktueller Wert,
  RSSI, MQTT-Topic, eigene Channel-Tags

**Tags:**
- Flach, mehrere pro Gerät und/oder Channel, frei wählbar
- **Räume sind Tags** (z.B. `WohnenEG`, `BadOG`) — keine separate Eigenschaft,
  Konsistent mit Timberwolf-Pattern
- Autocomplete aus bereits vergebenen Tags
- Filter „zeige Geräte mit Tag X"

### 4. Timberwolf-Setup-Helper

Pro Gerät/Channel zeigt die UI „So legst du das im Timberwolf an":
- **MQTT-Topic** zum Kopieren (Copy-Button)
- **JSON-Pfad** für Wert-Extraktion (z.B. `$.value`)
- **Vorgeschlagener Datentyp** (Zahl/Boolean/Text)
- **Zeitserie sinnvoll?** Hinweis (z.B. „Wert ändert sich häufig → Zeitserie ja")
- **Aktor-Hinweis:** „Set-Topic: …/set, sende JSON {value: true}"

Hintergrund: Timberwolf-API kann KEINE Objekte programmatisch anlegen,
daher muss diese Setup-Information so kopierbar wie möglich sein.

### 5. Live-Log

Letzte ~50 Telegramme rollend, mit:
- Zeitstempel
- Gateway-Name
- Sender-ID
- RORG / RSSI
- Match zu bekanntem Gerät (Name) oder „unbekannt"
- Decode-Ergebnis (falls EEP bekannt)
- Filter (nach GW, nach Sender, nach Match-Status)

Zweck: Bei Problemen sieht User sofort ob überhaupt Telegramme kommen.

### 6. Multi-Gateway / Kaskadierung

UI-Bereich für **mehrere LAN-Gateways parallel**:
- GW-Liste mit Status (verbunden, Telegramm-Counter, letzte Verbindung)
- Pro GW: aktiv/inaktiv toggle
- Dedup-Window-Setting (z.B. 200ms)
- Sende-Strategie wählbar:
  - „bestes RSSI" (empfohlen)
  - „alle GWs senden"
  - „nach Tag-Zuordnung"
- RSSI-Tabelle pro Sender/GW (Diagnose)

Hintergrund: Funkgeometrie in größeren Häusern (z.B. L-Bungalow mit Stahlbeton)
braucht Multi-GW für robusten Empfang.

### 7. Sende-Befehle / Aktor-Test

UI-Test-Buttons pro Aktor-Channel:
- Schalter: An/Aus
- Dimmer: 0–100% Slider + Ein/Aus
- Rolladen: Auf/Stop/Ab + Position
- Rückmeldung: hat Aktor reagiert? (Feedback-Telegramm)

Hintergrund: Ohne Sende-Test ist die UI nur halb fertig — User will sofort
sehen ob Schalten klappt.

## Später (V2+)

- **Backup / Restore** der gesamten Config
- Erweiterte Diagnose (Heatmap, History)
- Übersetzungen (Englisch, ggf. weitere)
- Multi-User / Auth

## Niemals

- **Direkter YAML-Edit in der UI.** Wenn jemand YAML editieren will,
  macht er das per Portainer-Console / SSH am Container. Die UI bleibt
  konsequent klickbar.

## Verschlüsselung (EnOcean Secure)

- **Pro Gerät**, nicht global
- Aktivierung im Anlern-Wizard (Checkbox „verschlüsselt anlernen")
- AES-128-Key + Rolling Code pro Sender-ID gespeichert
- Architektur jetzt vorbereiten, vollständige Implementierung wenn echtes
  Secure-Gerät verfügbar

## Architektur-Implikationen

Die V1-Web-UI bedingt dass folgende Module bis dahin existieren müssen:

- **M3** Multi-Gateway + Kaskadierung (für Punkt 6)
- **M4** Web-UI selbst (Backend + Frontend)
- **M5** Senden + Teach-In (für Punkt 7 + Anlern-Modus)

## Tag-Vorschläge beim Anlernen

**Pattern: „vorsichtig vorschlagen, direkt wegklickbar".**

Beim Anlernen werden automatisch Tag-Vorschläge als Chips eingeblendet,
jeder mit X zum sofortigen Entfernen. Quellen für Vorschläge:

- **Aus Hersteller-Modell** (z.B. Eltako FSR14 → Tags `Eltako`, `Schaltaktor`)
- **Aus Geräte-Typ** (z.B. PIR → Tag `Bewegungsmelder`)
- **Aus zuletzt vergebenen Tags** (User hat gerade 3 Geräte mit Tag `EG`
  angelegt → auch beim nächsten als Vorschlag)

User übernimmt was passt, klickt anderes mit X weg, ergänzt eigene.
Keine versteckten Auto-Tags — alle Vorschläge sichtbar und einzeln entfernbar.

## Implementierungs-Reihenfolge

**Entschieden:** Backend zuerst, dann UI. Reihenfolge:

1. **M3** — Multi-Gateway + Kaskadierung
2. **M5** — Senden + Teach-In an Aktoren
3. **M4** — Web-UI (auf fertigem Backend aufsetzend)

Begründung: Wenn die UI gebaut wird, funktioniert jeder Button sofort.
Keine „totes Backend hinter UI"-Phase.
