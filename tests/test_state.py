"""Test dello stato locale (task D1). Nessuna rete."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spotify_calendar_helper.state import (
    MAX_RUNS,
    STATE_VERSION,
    RunRecord,
    State,
    state_key,
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state.json"


def test_file_mancante_da_stato_vuoto_senza_avvisi(state_path: Path) -> None:
    state = State.load(state_path)
    assert state.playlists == {} and state.runs == [] and state.warning is None
    assert state.last_run is None


def test_roundtrip_playlist_e_run(state_path: Path) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "Artista - Alfa", now="2025-03-01T10:00:00+00:00")
    state.put("genre", "indie", "pl2", "Genere - Indie", now="2025-03-01T10:00:00+00:00")
    state.add_run(RunRecord("2025-03-01T10:00:00+00:00", "sync", created=2, warnings=1))
    state.save()

    loaded = State.load(state_path)
    assert loaded.warning is None
    assert loaded.get("artist", "id1") == state.get("artist", "id1")
    assert loaded.get("genre", "indie").name == "Genere - Indie"
    assert loaded.last_run == RunRecord("2025-03-01T10:00:00+00:00", "sync", created=2, warnings=1)


def test_chiave_composta_da_kind_e_chiave() -> None:
    assert state_key("artist", "abc") == "artist:abc"


def test_put_mantiene_created_at_se_l_id_non_cambia(state_path: Path) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "Artista - Alfa", now="2025-01-01T00:00:00+00:00")
    updated = state.put("artist", "id1", "pl1", "Artista - Alfa (rinominata)", now="2025-02-01T00:00:00+00:00")
    assert updated.created_at == "2025-01-01T00:00:00+00:00"
    assert updated.last_sync == "2025-02-01T00:00:00+00:00"
    assert updated.name == "Artista - Alfa (rinominata)"


def test_put_con_id_diverso_riparte_il_created_at(state_path: Path) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "Artista - Alfa", now="2025-01-01T00:00:00+00:00")
    new = state.put("artist", "id1", "pl_nuova", "Artista - Alfa", now="2025-02-01T00:00:00+00:00")
    assert new.created_at == "2025-02-01T00:00:00+00:00"


def test_put_senza_now_usa_un_timestamp_utc(state_path: Path) -> None:
    record = State(state_path).put("artist", "id1", "pl1", "X")
    assert record.created_at.endswith("+00:00")


def test_forget(state_path: Path) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "X")
    assert state.forget("artist", "id1") is True
    assert state.forget("artist", "id1") is False
    assert state.get("artist", "id1") is None


def test_storico_limitato_agli_ultimi_run(state_path: Path) -> None:
    state = State(state_path)
    for i in range(MAX_RUNS + 10):
        state.add_run(RunRecord(f"t{i:03d}", "sync"))
    assert len(state.runs) == MAX_RUNS
    assert state.runs[0].timestamp == "t010"
    assert state.last_run.timestamp == f"t{MAX_RUNS + 9:03d}"


def test_json_corrotto_riparte_da_zero_con_avviso(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{rotto", encoding="utf-8")
    state = State.load(state_path)
    assert state.playlists == {} and state.warning is not None


def test_versione_diversa_riparte_da_zero_con_avviso(state_path: Path) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"version": STATE_VERSION + 1}), encoding="utf-8")
    state = State.load(state_path)
    assert state.playlists == {} and state.warning is not None


def test_voci_malformate_scartate_le_altre_restano(state_path: Path) -> None:
    good = State(state_path)
    good.put("artist", "ok", "pl1", "Artista - Ok", now="2025-01-01T00:00:00+00:00")
    good.add_run(RunRecord("t1", "sync"))
    good.save()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["playlists"]["artist:rotta"] = {"playlist_id": "x"}
    data["playlists"]["tipo:strano"] = {
        "playlist_id": "x", "name": "n", "kind": "album", "created_at": "t", "last_sync": "t",
    }
    data["runs"].append({"timestamp": "t2"})
    state_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = State.load(state_path)
    assert set(loaded.playlists) == {"artist:ok"}
    assert len(loaded.runs) == 1
    assert loaded.warning is not None and "3" in loaded.warning


def test_scrittura_atomica_non_lascia_file_temporanei(state_path: Path) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "X")
    state.save()
    state.save()
    assert [p.name for p in state_path.parent.iterdir()] == [state_path.name]


def test_scrittura_fallita_non_rovina_il_file_esistente(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State(state_path)
    state.put("artist", "id1", "pl1", "X")
    state.save()
    before = state_path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disco pieno")

    monkeypatch.setattr("spotify_calendar_helper.state.os.replace", boom)
    state.put("artist", "id2", "pl2", "Y")
    with pytest.raises(OSError):
        state.save()
    assert state_path.read_text(encoding="utf-8") == before
    assert [p.name for p in state_path.parent.iterdir()] == [state_path.name]
