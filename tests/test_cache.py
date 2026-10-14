"""Test della cache del calendario (task B2). Nessuna rete."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotify_calendar_helper.cache import CACHE_VERSION, CalendarCache, track_from_dict, track_to_dict
from tests.fakes import make_track


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "calendar_cache.json"


def test_file_mancante_da_cache_vuota_senza_avvisi(cache_path: Path) -> None:
    cache = CalendarCache.load(cache_path)
    assert cache.entries == {}
    assert cache.warning is None


def test_salvataggio_e_ricaricamento_identici(cache_path: Path) -> None:
    tracks = [
        make_track("t1", "Uno", ["Artista A", "Artista B"], added_at="2025-03-01T10:00:00Z"),
        make_track("t2", "Due", ["Artista A"], added_at=None, album_name="Àlbum ✓"),
    ]
    cache = CalendarCache(cache_path)
    cache.store("pl1", "snap1", "Marzo 2025", tracks, skipped_items=3)
    cache.save()

    loaded = CalendarCache.load(cache_path)
    assert loaded.warning is None
    entry = loaded.entries["pl1"]
    assert entry.tracks == tuple(tracks)
    assert entry.skipped_items == 3
    assert entry.name == "Marzo 2025"
    assert entry.snapshot_id == "snap1"


def test_creata_la_cartella_dei_dati(cache_path: Path) -> None:
    assert not cache_path.parent.exists()
    CalendarCache(cache_path).save()
    assert cache_path.exists()


def test_lookup_hit_solo_con_snapshot_uguale(cache_path: Path) -> None:
    cache = CalendarCache(cache_path)
    cache.store("pl1", "snap1", "Marzo 2025", [make_track("t1")])
    assert cache.lookup("pl1", "snap1") is not None
    assert cache.lookup("pl1", "snap2") is None
    assert cache.lookup("altra", "snap1") is None


def test_senza_snapshot_la_cache_non_si_usa(cache_path: Path) -> None:
    cache = CalendarCache(cache_path)
    cache.store("pl1", None, "Marzo 2025", [make_track("t1")])
    assert cache.lookup("pl1", None) is None


def test_store_sostituisce_la_voce_precedente(cache_path: Path) -> None:
    cache = CalendarCache(cache_path)
    cache.store("pl1", "snap1", "Marzo 2025", [make_track("t1")])
    cache.store("pl1", "snap2", "Marzo 2025", [make_track("t1"), make_track("t2")])
    assert cache.lookup("pl1", "snap1") is None
    entry = cache.lookup("pl1", "snap2")
    assert entry is not None and len(entry.tracks) == 2


def test_discard_except_toglie_le_playlist_sparite(cache_path: Path) -> None:
    cache = CalendarCache(cache_path)
    for pid in ("a", "b", "c"):
        cache.store(pid, "s", pid, [])
    assert cache.discard_except(["a", "c"]) == 1
    assert set(cache.entries) == {"a", "c"}


def test_json_corrotto_riparte_da_zero_con_avviso(cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{non è json", encoding="utf-8")
    cache = CalendarCache.load(cache_path)
    assert cache.entries == {}
    assert cache.warning is not None


def test_versione_diversa_riparte_da_zero_con_avviso(cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({"version": CACHE_VERSION + 1, "playlists": {}}), encoding="utf-8")
    cache = CalendarCache.load(cache_path)
    assert cache.entries == {}
    assert cache.warning is not None


def test_radice_non_dizionario_riparte_da_zero(cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("[1, 2, 3]", encoding="utf-8")
    cache = CalendarCache.load(cache_path)
    assert cache.entries == {}
    assert cache.warning is not None


def test_voci_malformate_scartate_le_altre_restano(cache_path: Path) -> None:
    good = CalendarCache(cache_path)
    good.store("ok", "s1", "Marzo 2025", [make_track("t1")])
    good.save()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    data["playlists"]["rotta"] = {"snapshot_id": "s2", "name": "X", "tracks": [{"id": "solo-id"}]}
    data["playlists"]["pure-rotta"] = "stringa"
    cache_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = CalendarCache.load(cache_path)
    assert set(loaded.entries) == {"ok"}
    assert loaded.warning is not None and "2" in loaded.warning


def test_scrittura_atomica_non_lascia_file_temporanei(cache_path: Path) -> None:
    cache = CalendarCache(cache_path)
    cache.store("pl1", "s", "Marzo 2025", [make_track("t1")])
    cache.save()
    cache.save()
    leftovers = [p.name for p in cache_path.parent.iterdir() if p.name != cache_path.name]
    assert leftovers == []


def test_scrittura_fallita_non_rovina_il_file_esistente(
    cache_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = CalendarCache(cache_path)
    cache.store("pl1", "s", "Marzo 2025", [make_track("t1")])
    cache.save()
    before = cache_path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disco pieno")

    monkeypatch.setattr("spotify_calendar_helper.cache.os.replace", boom)
    cache.store("pl2", "s", "Aprile 2025", [])
    with pytest.raises(OSError):
        cache.save()

    assert cache_path.read_text(encoding="utf-8") == before
    assert [p.name for p in cache_path.parent.iterdir()] == [cache_path.name]


def test_track_roundtrip_con_artisti_multipli() -> None:
    track = make_track("t1", "Duetto", ["A", "B", "C"])
    assert track_from_dict(track_to_dict(track)) == track


def test_track_malformato_solleva_errore() -> None:
    with pytest.raises((KeyError, ValueError, TypeError)):
        track_from_dict({"id": "x", "uri": "u", "name": "n", "artists": "non-una-lista"})
