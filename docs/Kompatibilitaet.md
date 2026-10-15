# Kompatibilität

Zwei Fragen, die hier bewusst getrennt beantwortet werden:

- **Umgesetzt** — es gibt einen Decoder (Empfang) bzw. Encoder (Senden), und
  automatische Tests prüfen die Byte-Belegung. Das sagt noch nichts darüber,
  ob ein bestimmtes Gerät sich genau so verhält.
- **Im Betrieb erprobt** — das Profil läuft in einer echten Installation mit
  echter Hardware, in der angegebenen Richtung.

Ein gesendetes Telegramm ist kein Beweis, dass der Aktor geschaltet hat. Wo
ein Gerät keine Rückmeldung schickt, steht das in den
[bekannten Einschränkungen](../KNOWN_LIMITATIONS.md).

Stand: v0.9.173. Die Tabelle der Profile wird von
`tests/test_kompatibilitaet.py` gegen den Code geprüft — fehlt ein Profil
hier oder stimmt die Spalte *Senden* nicht, schlägt der Test fehl.

---

## Profile

| EEP | Bezeichnung | Empfang | Senden | Im Betrieb erprobt |
|---|---|---|---|---|
| `F6-02-01` | Wippentaster (PTM) | ja | ja | Empfang |
| `F6-02-02` | Wippentaster (PTM) | ja | ja | – |
| `F6-03-01` | Wippentaster, 2 Wippen | ja | ja | – |
| `F6-03-02` | Wippentaster | ja | ja | – |
| `F6-05-01` | Wassermelder | ja | – | – |
| `F6-05-02` | Rauchmelder | ja | – | Empfang |
| `F6-10-00` | Fenstergriff | ja | – | – |
| `A5-02-01` | Temperatursensor | ja | – | – |
| `A5-02-05` | Temperatursensor | ja | – | – |
| `A5-04-01` | Temperatur + Feuchte | ja | – | – |
| `A5-04-02` | Temperatur + Feuchte | ja | – | – |
| `A5-07-01` | Bewegungsmelder | ja | – | – |
| `A5-07-02` | Bewegungsmelder | ja | – | – |
| `A5-10-02` | Raumbedienteil (Temperatur, Sollwert, Lüfter, Belegung) | ja | – | Empfang |
| `A5-10-03` | Raumbedienteil (Temperatur, Sollwert) | ja | ja | Empfang + Senden |
| `A5-10-06` | Raumbedienteil (Temperatur, Sollwert, Tag/Nacht) | ja | ja | Empfang + Senden |
| `A5-12-00` | Zähler | ja | – | – |
| `A5-12-01` | Energiezähler | ja | – | Empfang |
| `A5-12-02` | Gaszähler | ja | – | Empfang |
| `A5-12-03` | Wasserzähler | ja | – | Empfang |
| `A5-13-01` | Wetterstation | ja | – | Empfang |
| `A5-13-02` | Wetterstation (Sonne) | ja | – | – |
| `A5-38-08` | Central Command (Schalten/Dimmen) | ja | ja | Empfang + Senden |
| `A5-3F-7F` | Universal 4BS / Rollladen | ja | ja | Empfang + Senden |
| `ELTAKO-FSB` | Eltako Rollladen-Aktor | ja | ja | – |
| `ELTAKO-FSR` | Eltako Schalt-Aktor | ja | ja | – |
| `ELTAKO-FSR-FEEDBACK` | Eltako FSR, Rückmeldung | ja | – | – |
| `ELTAKO-FUD-FEEDBACK` | Eltako Dimmer, Rückmeldung | ja | – | – |
| `ELTAKO-FSB-FEEDBACK` | Eltako Rollladen, Rückmeldung | ja | – | – |
| `D5-00-01` | Magnetkontakt | ja | – | Empfang |
| `D2-01-00` | VLD-Aktor, 1 Kanal Schalten + Pilot Wire | ja | ja | – |
| `D2-01-01` | VLD-Aktor, 2 Kanäle Schalten | ja | ja | – |
| `D2-01-03` | VLD-Aktor, 1 Kanal Dimmen | ja | ja | – |
| `D2-01-08` | VLD-Aktor, Schalten + Energiemessung | ja | ja | – |
| `D2-01-09` | VLD-Aktor, Schalten/Dimmen + Energiemessung | ja | ja | – |
| `D2-01-0C` | VLD-Aktor, Schalten/Dimmen | ja | ja | – |
| `D2-01-0D` | VLD-Aktor, Schalten/Dimmen + Pilot Wire | ja | ja | – |
| `D2-01-12` | VLD-Aktor, mehrkanalig | ja | ja | – |
| `D2-01-XX` | VLD-Aktor (generisch, z. B. OPUS BRiDGE) | ja | ja | Empfang + Senden |
| `D2-05-00` | Jalousie/Rollladen mit Positionsangabe | ja | – | – |
| `D2-05-01` | Jalousie/Rollladen, Position + Lamelle | ja | – | – |
| `D2-05-02` | Jalousie/Rollladen (OPUS BRiDGE) | ja | – | Empfang |
| `D2-05-XX` | Jalousie/Rollladen (generisch) | ja | – | – |

---

## Geräte im Betrieb

Diese Geräte laufen in einer Referenzinstallation (Wohnhaus, Timberwolf-Server,
mehrere LAN-Gateways). Die Richtung ist die, in der sie dort genutzt werden.

**Eltako Baureihe 14** (Hutschiene, über FAM14 auf Funk):

| Gerät | EEP | Richtung |
|---|---|---|
| FSR14-2x, FSR14-4x | A5-38-08 | Empfang + Senden |
| FDG14 | A5-38-08 | Empfang + Senden |
| FWZ14-65A, DSZ14DRS-3x65A | A5-12-01 | Empfang |
| F3Z14D | A5-12-01, A5-12-02, A5-12-03 | Empfang |
| FTS14EM | F6-02-01 | Empfang |

**Eltako dezentral:**

| Gerät | EEP | Richtung |
|---|---|---|
| FSB61NP (Rollladen) | A5-3F-7F | Empfang + Senden |
| FLD61 | A5-38-08 | Empfang + Senden |
| FSM61-UC, FT55, FSSA-230V | F6-02-01 | Empfang |
| FWS61 (Wetterstation) | A5-13-01 | Empfang |

**OPUS GreenNet / BRiDGE (Jäger Direkt):**

| Gerät | EEP | Richtung |
|---|---|---|
| BRiDGE UP-eSchalter 1 und 2 Kanal V2 | D2-01-XX, F6-02-01 | Empfang + Senden |
| BRiDGE LED-Funk-Dimmer | D2-01-XX, F6-02-01 | Empfang + Senden |
| BRiDGE UP-eSchalter Jal V2 | D2-05-02, F6-02-01 | Empfang (Fahrbefehle über die Bridge: siehe Einschränkungen) |
| GN-A-U230V-JRG (Jalousie) | A5-3F-7F | Empfang + Senden |
| GN-A-U230V-UD | A5-38-08 | Empfang |
| GN-A-U230V-HKR-1 (Heizungsaktor, baugleich Eltako FHK61) | A5-10-03 | Senden |
| RTS55 (Raumbedienteil) | A5-10-03 | Empfang |
| RTS55 SP+S | A5-10-06 | Empfang |
| Wandsender 2 und 4 Kanal, Steckdosenschaltaktor | F6-02-01 | Empfang |
| Tür- und Fensterkontakt | D5-00-01 | Empfang |

**Weitere:**

| Gerät | EEP | Richtung |
|---|---|---|
| Thermokon SRC-DO8 (Heizregler, 8 Ausgänge) | A5-10-06 | Senden |
| Thermokon SR07 PMS | A5-10-06 | Empfang |
| Thermokon Thanos LQ SR | A5-10-02, D5-00-01, F6-02-01 | Empfang |
| AFRISO ASD20/1 (Rauchmelder) | F6-05-02 | Empfang |

**Gateways:** LAN-Gateways mit ESP3 über TCP (TCM310/TCM515).

---

## Ein Gerät fehlt oder verhält sich anders?

Bitte ein Issue mit Hersteller, Modell, EEP und einem Telegramm-Mitschnitt aus
dem Live-Log der Web-UI anlegen — **vorher eigene IDs, Topics und IP-Adressen
unkenntlich machen.**
