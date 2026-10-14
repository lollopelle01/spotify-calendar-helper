"""Test del contratto: modelli, marker, Protocol e fake."""

from __future__ import annotations

import pytest

from spotify_calendar_helper.models import (
    PlaylistChange,
    SyncReport,
    make_marker,
    parse_marker,
)
from spotify_calendar_helper.protocols import GenreProvider, SpotifyClient
from tests.fakes import (
    FakeApiError,
    FakeForbiddenError,
    FakeGenreProvider,
    FakeSpotifyClient,
    make_track,
)

# --- marker -----------------------------------------------------------------


def test_marker_roundtrip():
    marker = make_marker("artist", "4Z8W4fKeB5YxbusRsdQVPb")
    assert marker == "[sch:artist:4Z8W4fKeB5YxbusRsdQVPb]"
    assert parse_marker(f"Descrizione libera. {marker}") == ("artist", "4Z8W4fKeB5YxbusRsdQVPb")


def test_marker_genre_slug():
    assert parse_marker(make_marker("genre", "indie-pop")) == ("genre", "indie-pop")


@pytest.mark.parametrize("text", [None, "", "nessun marker", "[sch:album:x]", "[sch:artist:]"])
def test_parse_marker_assente(text):
    assert parse_marker(text) is None


@pytest.mark.parametrize("key", ["", "con spazio", "chiusa]"])
def test_make_marker_chiave_non_valida(key):
    with pytest.raises(ValueError):
        make_marker("artist", key)


# --- modelli ----------------------------------------------------------------


def test_track_primary_artist():
    t = make_track("t1", artists=["A", "B"])
    assert t.primary_artist is not None and t.primary_artist.name == "A"
    assert make_track("t2", artists=[]).primary_artist is None


def test_sync_report_noop():
    report = SyncReport()
    assert report.is_noop
    report.updated.append(PlaylistChange("artist", "a1", "Artista - A", "pl_1", added=2))
    assert not report.is_noop


# --- conformità ai Protocol -------------------------------------------------


def test_fake_rispetta_i_protocol():
    assert isinstance(FakeSpotifyClient(), SpotifyClient)
    assert isinstance(FakeGenreProvider(), GenreProvider)


# --- comportamento del FakeSpotifyClient ------------------------------------


def test_creazione_e_aggiunta_in_ordine(fake_client):
    pl = fake_client.create_playlist("Artista - A", "desc [sch:artist:a1]")
    assert pl.owner_id == "me" and pl.public is False
    fake_client.add_items(pl.id, ["spotify:track:1", "spotify:track:2", "spotify:track:3"])
    assert fake_client.uris_of(pl.id) == [
        "spotify:track:1",
        "spotify:track:2",
        "spotify:track:3",
    ]


def test_batch_rispettato():
    client = FakeSpotifyClient(api_max_batch=10, add_batch_size=10)
    pl = client.create_playlist("x")
    client.add_items(pl.id, [f"spotify:track:{i}" for i in range(25)])
    assert client.add_requests == [10, 10, 5]


def test_batch_troppo_grande_fa_emergere_l_errore():
    client = FakeSpotifyClient(api_max_batch=10, add_batch_size=100)
    pl = client.create_playlist("x")
    with pytest.raises(FakeApiError):
        client.add_items(pl.id, [f"spotify:track:{i}" for i in range(50)])


def test_playlist_altrui_non_leggibile_ne_modificabile(fake_client):
    other = fake_client.seed_playlist("Di altri", [make_track("t1")], owner_id="altro")
    assert other in fake_client.list_my_playlists()
    with pytest.raises(FakeForbiddenError):
        fake_client.get_playlist_items(other.id)
    with pytest.raises(FakeForbiddenError):
        fake_client.add_items(other.id, ["spotify:track:9"])


def test_snapshot_cambia_dopo_scrittura(fake_client):
    pl = fake_client.seed_playlist("Marzo 2025", [make_track("t1")])
    before = fake_client.list_my_playlists()[0].snapshot_id
    fake_client.add_items(pl.id, ["spotify:track:2"])
    after = fake_client.list_my_playlists()[0].snapshot_id
    assert before != after


def test_elementi_saltati_contati(fake_client):
    pl = fake_client.seed_playlist("Marzo 2025", [make_track("t1")], skipped=3)
    items = fake_client.get_playlist_items(pl.id)
    assert len(items.tracks) == 1 and items.skipped == 3


def test_ricerca_per_marker_dopo_rinomina(fake_client):
    pl = fake_client.create_playlist("Artista - A", "x [sch:artist:a1]")
    fake_client.rename_for_test(pl.id, "Il mio nome")
    found = fake_client.find_by_marker("artist", "a1")
    assert [p.id for p in found] == [pl.id]
    assert found[0].name == "Il mio nome"


def test_write_count_per_idempotenza(fake_client):
    pl = fake_client.create_playlist("x")
    fake_client.add_items(pl.id, ["spotify:track:1"])
    assert fake_client.write_count == 2
    fake_client.reset_counters()
    fake_client.list_my_playlists()
    fake_client.get_playlist_items(pl.id)
    assert fake_client.write_count == 0


def test_update_details_non_tocca_i_parametri_none(fake_client):
    pl = fake_client.create_playlist("Nome", "Descr")
    fake_client.update_details(pl.id, description="Nuova")
    info = fake_client.list_my_playlists()[0]
    assert info.name == "Nome" and info.description == "Nuova"


def test_get_artist_senza_generi(fake_client):
    fake_client.seed_playlist("m", [make_track("t1", artists=["A"])])
    artist = fake_client.get_artist("artist_a")
    assert artist.name == "A" and artist.genres == ()


def test_fake_genre_provider():
    provider = FakeGenreProvider({"a1": ["indie pop", "bedroom pop"]})
    assert provider.genres_for_artist("a1", "A") == ["indie pop", "bedroom pop"]
    assert provider.genres_for_artist("zzz", "Z") == []
    assert provider.calls == ["a1", "zzz"]
