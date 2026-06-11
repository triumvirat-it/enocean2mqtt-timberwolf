"""Tests fuer die ProductDB."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from app.products import ProductDB, _modelname_variants, _normalize_eep


def _write_yaml(path: Path, products: list[dict]) -> None:
    doc = {"meta": {"count": len(products)}, "products": products}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_normalize_eep_strips_subvariant():
    assert _normalize_eep("A5-38-08(01)") == "A5-38-08"
    assert _normalize_eep("F6-02-01") == "F6-02-01"
    assert _normalize_eep("") is None
    assert _normalize_eep(None) is None


def test_modelname_variants():
    v = _modelname_variants("FSB61NP (ab 41/11)")
    assert "FSB61NP (ab 41/11)" in v
    assert "FSB61NP" in v


def test_loads_builtin_yaml_if_exists():
    """Wenn unsere app/data/products.yaml existiert: einlesbar + nicht leer."""
    builtin = Path(__file__).parent.parent / "app" / "data" / "products.yaml"
    if not builtin.exists():
        pytest.skip(f"{builtin} fehlt — generiert mit extract-Skript")
    db = ProductDB.from_yaml_files(builtin)
    assert len(db) > 100, "Sollte > 100 Geraete enthalten"
    assert "Eltako" in db.manufacturers()


def test_custom_overrides_builtin():
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "builtin.yaml"
        cp = Path(td) / "custom.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Eltako", "model": "FSR14",
             "com_profile": "A5-38-08", "description": "Stock-Beschreibung"},
        ])
        _write_yaml(cp, [
            {"manufacturer": "Eltako", "model": "FSR14",
             "com_profile": "A5-38-08", "description": "User-Override"},
        ])
        db = ProductDB.from_yaml_files(bp, cp)
        hit = db.lookup("Eltako", "FSR14")
        assert hit is not None
        assert hit.description == "User-Override"


def test_lookup_with_model_suffix():
    """Modellname mit '(ab 41/11)' soll auch 'FSB61NP' alleine matchen."""
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "p.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Eltako", "model": "FSB61NP", "com_profile": "A5-3F-7F"},
        ])
        db = ProductDB.from_yaml_files(bp)
        # Direct match
        assert db.lookup("Eltako", "FSB61NP") is not None
        # Toleranter Match mit Suffix
        assert db.lookup("Eltako", "FSB61NP (ab 41/11)") is not None


def test_lookup_case_insensitive_manufacturer():
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "p.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Eltako", "model": "FSR14", "com_profile": "A5-38-08"},
        ])
        db = ProductDB.from_yaml_files(bp)
        assert db.lookup("eltako", "FSR14") is not None
        assert db.lookup("ELTAKO", "FSR14") is not None


def test_lookup_returns_none_for_unknown():
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "p.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Eltako", "model": "FSR14", "com_profile": "A5-38-08"},
        ])
        db = ProductDB.from_yaml_files(bp)
        assert db.lookup("UnbekannterHersteller", "XYZ") is None


def test_fts14em_is_not_fam14_bus():
    """FTS14EM ist KEIN FAM14-Bus-Modul — sie nutzt Quasi-Dezimal-IDs
    (0x10XX..0x14XX) aus den Drehschaltern, nicht FAM14-Base+Bus-Adresse.
    Channel-Count bleibt 10 (PTMSwitchModule_10), aber die IDs werden ueber
    fts14em_sender_id berechnet, nicht ueber fam14_address_to_sender_id."""
    builtin = Path(__file__).parent.parent / "app" / "data" / "products.yaml"
    if not builtin.exists():
        pytest.skip(f"{builtin} fehlt")
    db = ProductDB.from_yaml_files(builtin)
    info = db.lookup("Eltako", "FTS14EM")
    assert info is not None, "FTS14EM muss in der ProductDB sein"
    assert info.fam14_bus is False, "FTS14EM ist KEIN FAM14-Bus-Modul"
    assert info.channel_count == 10, "PTMSwitchModule_10 -> 10 Kanaele"


def test_multi_function_model_keeps_all_functions():
    """Mehrere Builtin-Zeilen mit gleichem Hersteller+Modell duerfen sich
    NICHT mehr ueberschreiben (frueher: DB-Kollaps). lookup() liefert die
    erste (Primaer), functions() liefert alle."""
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "p.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Thermokon", "model": "thanos",
             "com_profile": "D5-00-01", "device_type": "WindowContact"},
            {"manufacturer": "Thermokon", "model": "thanos",
             "device_type": "RoomTemperatureControl", "description": "Temp"},
            {"manufacturer": "Thermokon", "model": "thanos",
             "com_profile": "F6-02-01", "device_type": "PTMSwitchModule"},
        ])
        db = ProductDB.from_yaml_files(bp)
        primary = db.lookup("Thermokon", "thanos")
        assert primary is not None
        assert primary.eep == "D5-00-01"  # erste Zeile = Primaer
        funcs = db.functions("Thermokon", "thanos")
        assert len(funcs) == 3
        assert [f.device_type for f in funcs] == [
            "WindowContact", "RoomTemperatureControl", "PTMSwitchModule",
        ]
        # In der UI-Liste taucht das Modell nur EINMAL auf
        assert len(db.models_for("Thermokon")) == 1


def test_custom_replaces_all_builtin_functions():
    """Eine Custom-Zeile ersetzt die KOMPLETTE Builtin-Funktionsliste eines
    Modells (Override-Semantik bleibt erhalten, auch bei Multi-Funktion)."""
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "b.yaml"
        cp = Path(td) / "c.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Thermokon", "model": "thanos",
             "com_profile": "D5-00-01", "device_type": "WindowContact"},
            {"manufacturer": "Thermokon", "model": "thanos",
             "com_profile": "F6-02-01", "device_type": "PTMSwitchModule"},
        ])
        _write_yaml(cp, [
            {"manufacturer": "Thermokon", "model": "thanos",
             "com_profile": "A5-10-01", "device_type": "RoomTemperatureControl",
             "description": "Custom"},
        ])
        db = ProductDB.from_yaml_files(bp, cp)
        funcs = db.functions("Thermokon", "thanos")
        assert len(funcs) == 1, "Custom ersetzt die ganze Builtin-Liste"
        assert funcs[0].description == "Custom"


def test_builtin_thanos_has_three_functions():
    """Regression gegen den realen DB-Kollaps: die Thermokon-thanos-Zeilen in
    der ausgelieferten products.yaml bleiben als 3 Funktionen erhalten."""
    builtin = Path(__file__).parent.parent / "app" / "data" / "products.yaml"
    if not builtin.exists():
        pytest.skip(f"{builtin} fehlt")
    db = ProductDB.from_yaml_files(builtin)
    assert len(db.functions("Thermokon", "Thanos")) == 3
    # SRC-ADO BCS verlor frueher 3 von 4 Funktionen
    assert len(db.functions("Thermokon", "SRC-ADO BCS")) == 4


def test_models_for_returns_sorted():
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "p.yaml"
        _write_yaml(bp, [
            {"manufacturer": "Eltako", "model": "FUD14", "com_profile": "A5-38-08"},
            {"manufacturer": "Eltako", "model": "FSR14", "com_profile": "A5-38-08"},
            {"manufacturer": "Eltako", "model": "FSB61NP", "com_profile": "A5-3F-7F"},
        ])
        db = ProductDB.from_yaml_files(bp)
        models = db.models_for("Eltako")
        assert len(models) == 3
        assert [m.model for m in models] == ["FSB61NP", "FSR14", "FUD14"]
