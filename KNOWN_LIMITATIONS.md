# Bekannte Einschränkungen

Was die Bridge **nicht** kann oder wo man genau hinsehen muss — damit vor dem
Einspielen klar ist, worauf man sich verlassen kann. Welche Profile und Geräte
umgesetzt bzw. mit echter Hardware erprobt sind, steht in
[docs/Kompatibilitaet.md](docs/Kompatibilitaet.md).

Stand: v0.9.173.

---

## Sicherheit

- **Die Web-UI und ihre Schnittstellen haben keine Anmeldung.** Wer die Adresse
  im Netz erreicht, kann Geräte anlegen, anlernen und schalten. Nur in einem
  vertrauenswürdigen Netz betreiben, **nie** ins Internet freigeben. Für
  Fernzugriff ein VPN davorschalten.
- **MQTT ohne TLS.** Benutzer und Kennwort für den Broker werden unterstützt,
  die Verbindung ist aber nicht verschlüsselt.
- **EnOcean Security** (verschlüsselte, signierte Telegramme) wird nicht
  unterstützt.

## Funk und Geräte

- **Senden heißt nicht geschaltet.** Ein gesendetes Telegramm beweist nicht,
  dass der Aktor es angenommen hat. Verlässlich ist der Zustand nur bei
  Aktoren, die eine Rückmeldung schicken.
- **Rollladen-Positionen sind gerechnet, nicht gemessen** — aus Fahrzeit und
  Eichfahrt. Wird ein Rollladen von Hand oder von einem anderen Sender
  gefahren, kann die angezeigte Position abweichen, bis wieder eine Endlage
  angefahren wird.
- **OPUS BRiDGE UP-eSchalter Jal (D2-05):** Die Bridge empfängt Position und
  Lamellenwinkel, kann diesen Aktoren aber noch keine Fahrbefehle schicken —
  für D2-05 gibt es keinen Encoder. Rollläden und Jalousien über A5-3F-7F
  (Eltako FSB, OPUS GreenNet GN-A-U230V-JRG) fahren dagegen über die Bridge.
- **D2-01-Profile** außer dem generischen OPUS-BRiDGE-Aktor sind umgesetzt,
  aber nicht mit echter Hardware erprobt.
- **Anlernen:** Die Bridge sendet 4BS-Lerntelegramme (mit EEP-Angabe) und die
  Lerntelegramme der Heizungsaktoren. Ob der Empfänger sie angenommen hat,
  sieht man nur am Gerät.
- **Nur LAN-Gateways** (ESP3 über TCP, z. B. TCM310/TCM515). USB-Sticks werden
  nicht direkt angesprochen.
- **Nicht ausgewertet:** SIGNAL-Telegramme (Batteriestand, Funkqualität u. a.),
  Remote Management. Unbekannte Telegrammtypen erscheinen roh im Live-Log.

## Heizungsregelung

- **Der Heizregler meldet nichts zurück.** Ob der Sollwert angekommen ist, sieht
  man nur am Gerät (*Eff. Sollwert*) — siehe Gegenprobe in
  [docs/Heizungsregelung.md](docs/Heizungsregelung.md).
- **Der Verschiebungsbereich muss von Hand übereinstimmen** (Portal und Aktor).
  Weicht er ab, regelt der Aktor auf einen falschen Wert, ohne Fehlermeldung.
- **Nach einem Neustart** steht kurz „kein Ist-Wert", bis jeder Fühler wieder
  gesendet hat. Raumtemperaturen werden bewusst nicht gespeichert.

## Zähler

- Der **Ausreißerschutz** verwirft Sprünge über der eingestellten Grenze. Ein
  echter, sehr großer Verbrauch in kurzer Zeit (oder ein getauschter Zähler)
  wird dadurch ebenfalls verworfen, bis die Grenze angepasst ist. Verworfene
  Werte stehen im Verwurf-Protokoll.

## Betrieb

- **Bedienoberfläche nur auf Deutsch.**
- Auf dem Timberwolf gibt es keinen Host-Zugang: Update über Portainer, siehe
  [DEPLOY.md](DEPLOY.md). Die Konfiguration liegt im Volume und bleibt beim
  Update erhalten.

---

Weitere Einschränkung gefunden? Bitte als Issue melden — mit Version, genauem
Ablauf und **unkenntlich gemachten** IDs, Topics und IP-Adressen.
