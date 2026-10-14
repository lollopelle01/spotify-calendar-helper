"""Test del sync (task D2 e D3) con `FakeSpotifyClient`. Nessuna rete."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.grouping import group_by_artist
from spotify_calendar_helper.models import (
    CalendarPlaylist,
    PlaylistSpec,
    Track,
    make_marker,
    parse_marker,
)
from spotify_calendar_helper.state import State
from spotify_calendar_helper.sync import SyncOptions, find_existing, sync_specs
from tests.fakes import FakeSpotifyClient, make_track

ALFA = "artist_alfa"
BETA = "artist_beta"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(data_dir=tmp_path)


@pytest.fixture
def client() -> FakeSpotifyClient:
    return FakeSpotifyClient()


@pytest.fixture
def state(settings: Settings) -> State:
    return State(settings.state_path)


def tracks(artist: str, ids: range) -> list[Track]:
    return [make_track(f"{artist[:1].lower()}{i}", artists=[artist]) for i in ids]


def cal(year: int, month: int, items: list[Track]) -> CalendarPlaylist:
    return CalendarPlaylist(
        id=f"pl_{year}_{month}", name=f"{year}-{month:02d}", year=year, month=month,
        snapshot_id="s", tracks=tuple(items),
    )


def base_calendar() -> list[CalendarPlaylist]:
    return [
        cal(2025, 1, tracks("Alfa", range(5))),
        # Gamma ha 2 brani: sotto soglia (min_tracks = 4), non deve diventare una playlist.
        cal(2025, 2, tracks("Beta", range(4)) + tracks("Gamma", range(2))),
    ]


def specs_for(calendar: list[CalendarPlaylist], settings: Settings) -> list[PlaylistSpec]:
    return group_by_artist(calendar, settings)


def playlist_of(client: FakeSpotifyClient, key: str):
    (info,) = client.find_by_marker("artist", key)
    return info


def uris(client: FakeSpotifyClient, key: str) -> list[str]:
    return client.uris_of(playlist_of(client, key).id)


def run(client, state, specs, settings, **opts):
    return sync_specs(client, state, specs, settings, SyncOptions(**opts))


# --- primo sync -----------------------------------------------------------


def test_primo_sync_crea_le_playlist(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    report = run(client, state, specs, settings)

    assert [c.key for c in report.created] == [ALFA, BETA]
    assert report.updated == [] and report.unchanged == []
    assert uris(client, ALFA) == [f"spotify:track:a{i}" for i in range(5)]
    assert uris(client, BETA) == [f"spotify:track:b{i}" for i in range(4)]
    assert [c.added for c in report.created] == [5, 4]


def test_playlist_create_private_e_con_marker(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    info = playlist_of(client, ALFA)
    assert info.public is False
    assert parse_marker(info.description) == ("artist", ALFA)
    assert info.name == "Artista - Alfa"


def test_public_se_configurato(client, state, settings) -> None:
    settings.config["sync"]["public"] = True
    run(client, state, specs_for(base_calendar(), settings), settings)
    assert playlist_of(client, ALFA).public is True


def test_stato_registrato_dopo_il_sync(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    loaded = State.load(settings.state_path)
    assert loaded.warning is None
    assert loaded.get("artist", ALFA).playlist_id == playlist_of(client, ALFA).id
    assert loaded.last_run.created == 2


def test_nessuna_specifica(client, state, settings) -> None:
    report = run(client, state, [], settings)
    assert report.is_noop and client.write_count == 0
    assert State.load(settings.state_path).last_run is not None


def test_batch_rispettato_con_molti_brani(client, state, settings) -> None:
    calendar = [cal(2025, 1, tracks("Alfa", range(25)))]
    run(client, state, specs_for(calendar, settings), settings)
    assert client.add_requests == [10, 10, 5]
    assert len(uris(client, ALFA)) == 25


# --- idempotenza e aggiornamenti --------------------------------------------


def test_secondo_sync_non_modifica_nulla(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    client.reset_counters()

    report = run(client, State.load(settings.state_path), specs, settings)
    assert report.is_noop
    assert client.write_count == 0
    assert sorted(report.unchanged) == [f"artist:{ALFA}", f"artist:{BETA}"]


def test_nuovo_mese_aggiunge_solo_il_nuovo(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    before = playlist_of(client, ALFA).id

    marzo = cal(2025, 3, tracks("Alfa", range(5, 7)) + tracks("Beta", range(4, 5)) + tracks("Delta", range(4)))
    client.reset_counters()
    report = run(client, state, specs_for(base_calendar() + [marzo], settings), settings)

    assert [c.key for c in report.created] == ["artist_delta"]
    changes = {c.key: c for c in report.updated}
    assert changes[ALFA].added == 2 and changes[BETA].added == 1
    assert playlist_of(client, ALFA).id == before  # stessa playlist, nessun duplicato
    assert uris(client, ALFA)[-2:] == ["spotify:track:a5", "spotify:track:a6"]
    assert uris(client, ALFA)[:5] == [f"spotify:track:a{i}" for i in range(5)]
    assert all(size <= 10 for size in client.add_requests)


def test_nessun_brano_duplicato_dopo_piu_sync(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    for _ in range(3):
        run(client, State.load(settings.state_path), specs, settings)
    assert len(uris(client, ALFA)) == len(set(uris(client, ALFA))) == 5


def test_playlist_rinominata_a_mano_viene_aggiornata(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    pid = playlist_of(client, ALFA).id
    client.rename_for_test(pid, "La mia rinominata")

    calendar = base_calendar() + [cal(2025, 3, tracks("Alfa", range(5, 6)))]
    report = run(client, State.load(settings.state_path), specs_for(calendar, settings), settings)

    (change,) = report.updated
    assert change.playlist_id == pid and change.added == 1 and change.name == "La mia rinominata"
    assert playlist_of(client, ALFA).name == "La mia rinominata"  # il nome non viene toccato


def test_playlist_non_tocca_nome_e_descrizione_se_il_marker_c_e(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    client.reset_counters()
    run(client, State.load(settings.state_path), specs_for(base_calendar(), settings), settings)
    assert [c for c in client.calls if c[0] == "update_details"] == []


# --- recupero dello stato ---------------------------------------------------


def test_stato_cancellato_ricostruito_dal_marker(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    ids = {p.id for p in client.list_my_playlists()}
    settings.state_path.unlink()
    client.reset_counters()

    fresh = State.load(settings.state_path)
    report = run(client, fresh, specs, settings)

    assert report.created == [] and report.is_noop and client.write_count == 0
    assert {p.id for p in client.list_my_playlists()} == ids  # nessun duplicato
    assert fresh.get("artist", ALFA).playlist_id == playlist_of(client, ALFA).id


def test_playlist_cancellata_a_mano_viene_ricreata_con_avviso(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    client.delete_playlist_for_test(playlist_of(client, ALFA).id)

    report = run(client, State.load(settings.state_path), specs, settings)
    assert [c.key for c in report.created] == [ALFA]
    assert any("non esiste più" in w for w in report.warnings)
    assert len(uris(client, ALFA)) == 5


def test_stato_registrato_ma_playlist_ricreata_con_stesso_marker(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    old = playlist_of(client, ALFA)
    client.delete_playlist_for_test(old.id)
    nuova = client.seed_playlist(
        "Ricreata a mano", [make_track("a0", artists=["Alfa"])], description=old.description
    )
    report = run(client, State.load(settings.state_path), specs, settings)
    assert report.created == []
    assert playlist_of(client, ALFA).id == nuova.id
    assert any("ricollego" in w for w in report.warnings)


def test_marker_duplicato_usa_una_sola_playlist_e_avvisa(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    original = playlist_of(client, ALFA)
    client.seed_playlist("Doppione", [make_track("zz")], description=original.description)
    settings.state_path.unlink()

    report = run(client, State.load(settings.state_path), specs, settings)
    assert any("stesso marker" in w for w in report.warnings)
    assert report.created == []
    assert State.load(settings.state_path).get("artist", ALFA).playlist_id == original.id  # più brani


def test_playlist_altrui_con_lo_stesso_marker_non_vale(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    client.seed_playlist(
        "Di un altro", [make_track("q")], description=f"x {make_marker('artist', ALFA)}", owner_id="altro"
    )
    report = run(client, state, specs, settings)
    assert ALFA in [c.key for c in report.created]


def test_marker_mancante_viene_ripristinato_senza_perdere_la_descrizione(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    pid = playlist_of(client, ALFA).id
    client.update_details(pid, description="La mia descrizione")

    report = run(client, State.load(settings.state_path), specs, settings)
    (change,) = report.updated
    assert change.added == 0 and change.removed == 0
    info = next(p for p in client.list_my_playlists() if p.id == pid)
    assert info.description == f"La mia descrizione {make_marker('artist', ALFA)}"

    client.reset_counters()
    again = run(client, State.load(settings.state_path), specs, settings)
    assert again.is_noop and client.write_count == 0


# --- dry-run ------------------------------------------------------------------


def test_dry_run_non_scrive_nulla(client, state, settings) -> None:
    report = run(client, state, specs_for(base_calendar(), settings), settings, dry_run=True)
    assert report.dry_run is True
    assert [(c.key, c.playlist_id, c.added) for c in report.created] == [(ALFA, None, 5), (BETA, None, 4)]
    assert client.write_count == 0
    assert client.list_my_playlists() == []
    assert not settings.state_path.exists()


def test_dry_run_su_playlist_esistenti_riporta_senza_modificare(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    client.reset_counters()
    calendar = base_calendar() + [cal(2025, 3, tracks("Alfa", range(5, 8)))]

    report = run(client, State.load(settings.state_path), specs_for(calendar, settings), settings, dry_run=True)
    (change,) = report.updated
    assert change.key == ALFA and change.added == 3
    assert client.write_count == 0
    assert len(uris(client, ALFA)) == 5
    assert len(State.load(settings.state_path).runs) == 1  # il dry-run non entra nello storico


# --- prune ----------------------------------------------------------------------


def calendar_senza_a0() -> list[CalendarPlaylist]:
    return [
        cal(2025, 1, tracks("Alfa", range(1, 5))),
        cal(2025, 2, tracks("Beta", range(4)) + tracks("Gamma", range(2))),
    ]


def test_senza_prune_non_rimuove_mai(client, state, settings) -> None:
    settings.config["sync"]["mode"] = "mirror"
    run(client, state, specs_for(base_calendar(), settings), settings)
    client.reset_counters()
    report = run(client, State.load(settings.state_path), specs_for(calendar_senza_a0(), settings), settings)
    assert report.is_noop
    assert "spotify:track:a0" in uris(client, ALFA)


def test_prune_ignorato_in_modalita_additive(client, state, settings) -> None:
    run(client, state, specs_for(base_calendar(), settings), settings)
    client.reset_counters()
    report = run(
        client, State.load(settings.state_path), specs_for(calendar_senza_a0(), settings), settings, prune=True
    )
    assert any("--prune ignorato" in w for w in report.warnings)
    assert client.write_count == 0
    assert "spotify:track:a0" in uris(client, ALFA)


def test_prune_in_modalita_mirror_rimuove_i_brani_spariti(client, state, settings) -> None:
    settings.config["sync"]["mode"] = "mirror"
    run(client, state, specs_for(base_calendar(), settings), settings)
    report = run(
        client, State.load(settings.state_path), specs_for(calendar_senza_a0(), settings), settings, prune=True
    )
    (change,) = report.updated
    assert change.key == ALFA and change.removed == 1 and change.added == 0
    assert "spotify:track:a0" not in uris(client, ALFA)
    assert len(uris(client, ALFA)) == 4


def test_prune_con_dry_run_riporta_ma_non_rimuove(client, state, settings) -> None:
    settings.config["sync"]["mode"] = "mirror"
    run(client, state, specs_for(base_calendar(), settings), settings)
    client.reset_counters()
    report = run(
        client, State.load(settings.state_path), specs_for(calendar_senza_a0(), settings), settings,
        prune=True, dry_run=True,
    )
    assert report.updated[0].removed == 1
    assert client.write_count == 0
    assert "spotify:track:a0" in uris(client, ALFA)


# --- robustezza ---------------------------------------------------------------------


class FlakyClient(FakeSpotifyClient):
    """Fallisce alla N-esima chiamata di add_items (es. 429 dopo i retry)."""

    def __init__(self, fail_on_call: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_on_call = fail_on_call
        self._add_calls = 0

    def add_items(self, playlist_id, uris) -> None:
        self._add_calls += 1
        if self.fail_on_call == self._add_calls:
            raise RuntimeError("429 Too Many Requests")
        super().add_items(playlist_id, uris)


def test_interruzione_a_meta_non_crea_duplicati_al_run_successivo(settings) -> None:
    flaky = FlakyClient(fail_on_call=2)
    specs = specs_for(base_calendar(), settings)
    with pytest.raises(RuntimeError):
        sync_specs(flaky, State(settings.state_path), specs, settings)

    # Lo stato è stato salvato comunque, con entrambe le playlist create.
    saved = State.load(settings.state_path)
    assert saved.get("artist", ALFA) is not None and saved.get("artist", BETA) is not None
    assert len(flaky.list_my_playlists()) == 2

    flaky.fail_on_call = None
    report = sync_specs(flaky, State.load(settings.state_path), specs, settings)
    assert report.created == []
    assert len(flaky.list_my_playlists()) == 2
    assert uris(flaky, BETA) == [f"spotify:track:b{i}" for i in range(4)]

    # ... e anche se lo stato fosse perso, il marker basta.
    settings.state_path.unlink()
    flaky.reset_counters()
    again = sync_specs(flaky, State.load(settings.state_path), specs, settings)
    assert again.is_noop and flaky.write_count == 0


def test_specifica_duplicata_ignorata_con_avviso(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    report = run(client, state, [*specs, specs[0]], settings)
    assert len(report.created) == 2
    assert any("duplicata" in w for w in report.warnings)


def test_errore_di_salvataggio_stato_diventa_avviso(client, state, settings, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("disco pieno")

    monkeypatch.setattr("spotify_calendar_helper.state.os.replace", boom)
    report = run(client, state, specs_for(base_calendar(), settings), settings)
    assert len(report.created) == 2
    assert any("disco pieno" in w for w in report.warnings)


def test_storico_dei_run(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    run(client, State.load(settings.state_path), specs, settings)
    runs = State.load(settings.state_path).runs
    assert [(r.created, r.unchanged) for r in runs] == [(2, 0), (0, 2)]


# --- find_existing (D2) -------------------------------------------------------------


def test_find_existing_priorita_allo_stato(client, state, settings) -> None:
    specs = specs_for(base_calendar(), settings)
    run(client, state, specs, settings)
    # Una playlist "gemella" con lo stesso marker, ma lo stato punta alla prima.
    original = playlist_of(client, ALFA)
    client.seed_playlist("Gemella", [make_track(f"x{i}") for i in range(20)], description=original.description)
    match = find_existing(specs[0], State.load(settings.state_path), client.list_my_playlists(), "me")
    assert match.playlist.id == original.id and match.via == "state"
    assert match.warnings == ()


def test_find_existing_nessuna(client, state, settings) -> None:
    spec = specs_for(base_calendar(), settings)[0]
    match = find_existing(spec, state, client.list_my_playlists(), "me")
    assert match.playlist is None and match.via == "none" and match.warnings == ()
