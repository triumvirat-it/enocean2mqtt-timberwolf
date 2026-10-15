"""
Die Kompatibilitaetsliste (docs/Kompatibilitaet.md) muss zum Code passen.

Warum ein Test: Eine Liste, die von Hand gepflegt wird, veraltet still. Wer
ein Profil ergaenzt oder einen Encoder nachruestet, soll hier daran erinnert
werden, die Liste mitzuziehen — sonst verspricht sie etwas, das der Code nicht
kann, oder verschweigt etwas, das er kann.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.eep.profiles import PROFILES

DOKU = Path(__file__).resolve().parent.parent / "docs" / "Kompatibilitaet.md"


def _profil_zeilen() -> dict[str, list[str]]:
    """EEP -> Spalten der Tabelle im Abschnitt 'Profile'."""
    text = DOKU.read_text(encoding="utf-8")
    abschnitt = text.split("## Profile", 1)[1].split("\n## ", 1)[0]
    zeilen: dict[str, list[str]] = {}
    for zeile in abschnitt.splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$", zeile)
        if m:
            zeilen[m.group(1)] = [s.strip() for s in m.group(2).split("|")]
    return zeilen


def test_jedes_profil_steht_in_der_liste():
    zeilen = _profil_zeilen()
    fehlen = sorted(p.eep_id for p in PROFILES if p.eep_id not in zeilen)
    assert not fehlen, f"in docs/Kompatibilitaet.md nachtragen: {fehlen}"


def test_die_liste_nennt_kein_profil_das_es_nicht_gibt():
    vorhanden = {p.eep_id for p in PROFILES}
    zu_viel = sorted(set(_profil_zeilen()) - vorhanden)
    assert not zu_viel, f"steht in der Liste, gibt es im Code nicht: {zu_viel}"


def test_spalten_empfang_und_senden_stimmen():
    zeilen = _profil_zeilen()
    falsch = []
    for p in PROFILES:
        _, empfang, senden, _ = zeilen[p.eep_id]
        if (empfang == "ja") != (p.decoder is not None):
            falsch.append(f"{p.eep_id} Empfang")
        if (senden == "ja") != (p.encoder is not None):
            falsch.append(f"{p.eep_id} Senden")
    assert not falsch, f"Spalte passt nicht zum Code: {falsch}"


def test_erprobtes_senden_setzt_einen_encoder_voraus():
    """'Senden' im Betrieb erprobt, ohne Encoder — das waere eine Behauptung ohne Code."""
    zeilen = _profil_zeilen()
    widerspruch = [eep for eep, sp in zeilen.items()
                   if "Senden" in sp[3] and sp[2] != "ja"]
    assert not widerspruch, widerspruch
