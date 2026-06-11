"""M91: Tests fuer die serverseitige FAM14-Listen-Persistenz (/data)."""
from __future__ import annotations

import json

from app.webui.server import _fam14_path, _load_fam14, _save_fam14


def test_fam14_roundtrip(tmp_path):
    items = [
        {"name": "Haus 1 EG", "base_id": "FF800080"},
        {"name": "Haus 1 OG", "base_id": "FF800100"},
    ]
    _save_fam14(tmp_path, items)
    assert _fam14_path(tmp_path).exists()
    loaded = _load_fam14(tmp_path)
    assert loaded == items


def test_fam14_missing_file_returns_empty(tmp_path):
    assert _load_fam14(tmp_path) == []


def test_fam14_malformed_json_returns_empty(tmp_path):
    _fam14_path(tmp_path).write_text("{ kaputt", encoding="utf-8")
    assert _load_fam14(tmp_path) == []


def test_fam14_base_id_normalized_uppercase(tmp_path):
    # Kleinbuchstaben in der Datei -> beim Laden gross
    _fam14_path(tmp_path).write_text(
        json.dumps({"items": [{"name": "x", "base_id": "FF800080"}]}),
        encoding="utf-8",
    )
    loaded = _load_fam14(tmp_path)
    assert loaded == [{"name": "x", "base_id": "FF800080"}]


def test_fam14_skips_entries_without_base_id(tmp_path):
    _fam14_path(tmp_path).write_text(
        json.dumps({"items": [
            {"name": "ok", "base_id": "FF800080"},
            {"name": "kein base"},          # gefiltert
            "string statt dict",            # gefiltert
        ]}),
        encoding="utf-8",
    )
    loaded = _load_fam14(tmp_path)
    assert loaded == [{"name": "ok", "base_id": "FF800080"}]


def test_fam14_accepts_bare_list_without_wrapper(tmp_path):
    # Toleranz: Datei enthaelt direkt eine Liste statt {"items": [...]}
    _fam14_path(tmp_path).write_text(
        json.dumps([{"name": "y", "base_id": "FFAA0080"}]),
        encoding="utf-8",
    )
    assert _load_fam14(tmp_path) == [{"name": "y", "base_id": "FFAA0080"}]


def test_fam14_name_defaults_to_empty_string(tmp_path):
    _fam14_path(tmp_path).write_text(
        json.dumps({"items": [{"base_id": "FFAA0080"}]}),
        encoding="utf-8",
    )
    assert _load_fam14(tmp_path) == [{"name": "", "base_id": "FFAA0080"}]
