# Veröffentlichen

Für Maintainer: wie ein Stand ins öffentliche Repo und als Release zu den
Anwendern kommt — ohne dass dabei Daten einer echten Anlage mitgehen.

## Der Grundsatz

**Veröffentlicht wird nur, was auf der Positivliste steht** (`release-liste.txt`).
Die `.gitignore` ist kein Exportfilter: Sie verhindert nur, dass `git add .`
etwas mitnimmt. Ein Arbeitsordner enthält in der Praxis mehr — Logbücher,
Notizen, Konfigurationen mit echten IDs und Adressen, Hilfswerkzeuge. Was nicht
ausdrücklich freigegeben ist, bleibt draußen.

Daneben gilt: **Testdaten und Beispiele nur mit Beispielwerten.** IP-Adressen
aus `192.0.2.0/24` (Dokumentationsnetz), Sender-IDs aus erfundenen Blöcken,
Topics wie `enocean/raum_a/…`. Nie die IDs, Topics oder Raumnamen der eigenen
Anlage abschreiben — auch nicht „nur im Test".

## `release.ps1`

```powershell
.\release.ps1                      # nur prüfen
.\release.ps1 -Paket               # prüfen + dist/enocean2mqtt-source.tar.gz bauen
.\release.ps1 -Veroeffentlichen    # prüfen + Paket + GitHub-Release v<Version>
```

Die Prüfung bricht ab, wenn

1. eine Datei im Stand **nicht auf der Positivliste** steht,
2. ein **Zugangsdatum** auftaucht (GitHub-/Slack-Token, privater Schlüssel,
   Verweis in einen Passwort-Tresor u. a.),
3. ein Eintrag der **lokalen Sperrliste** auftaucht.

Geprüft wird der ganze Stand **und** jede Zeile, die in noch nicht gepushten
Commits dazukam — auch eine, die ein späterer Commit wieder entfernt hat; sie
ginge sonst mit der Historie hinaus. Gemeldet werden nur Datei und Art des
Fundes, nie der Inhalt.

### Die Sperrliste

`release-sperrliste.txt` liegt **nur lokal** (steht in `.gitignore`) und
enthält, was die eigene Anlage verrät: echte EnOcean-IDs, interne
IP-Adressen, Namen, echte Topics. Ein regulärer Ausdruck je Zeile:

```text
# Netz
192\.168\.10\.
# Echte Kennungen
FF1234AB
```

Fehlt die Datei, warnt das Skript und sucht nur nach Zugangsdaten.

### Als Haken vor jedem Push

```sh
# .git/hooks/pre-push
#!/bin/sh
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./release.ps1
```

Dann bricht `git push` ab, bevor etwas hinausgeht.

## Ein Release machen

1. Version in `app/__init__.py` erhöhen.
2. In `CHANGELOG.md` den Abschnitt „Unveröffentlicht" in `## <Version> — <Datum>`
   umbenennen. Verhaltensänderungen als **Bitte prüfen** markieren.
3. Bei neuen Profilen oder Geräten [Kompatibilitaet.md](Kompatibilitaet.md)
   nachziehen (der Test `tests/test_kompatibilitaet.py` erinnert daran) und
   [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md) prüfen.
4. `python -m pytest tests/ -q`, committen, pushen.
5. `$env:GITHUB_TOKEN = '<Token mit repo-Recht>'; .\release.ps1 -Veroeffentlichen`

Das Paket entsteht aus dem Git-Stand (`git archive`), nicht aus dem
Arbeitsordner: ohne `test-data/`, mit dem Dockerfile im Hauptordner, so wie
Portainer es für „Build a new image" erwartet. Die Release-Notiz ist der
Changelog-Abschnitt der Version.
