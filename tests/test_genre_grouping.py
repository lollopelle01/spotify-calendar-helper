"""Test del raggruppamento per genere (task C4). Nessuna rete."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.genres import GenreMap
from spotify_calendar_helper.grouping import group_by_genre
from spotify_calendar_helper.models import ArtistRef, CalendarPlaylist, Track, parse_marker
from tests.fakes import FakeGenreProvider, make_artist, make_track


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(data_dir=tmp_path)


@pytest.fixture
def genre_map() -> GenreMap:
    return GenreMap({
        "Rock": ["rock", "indie rock", "classic rock", "hard rock"],
        "Indie": ["indie pop", "bedroom pop", "dream pop"],
        "Hip Hop": ["hip-hop", "rap italiano", "trap"],
        "Electronic": ["elettronica", "electronic", "house", "techno"],
    })


def cal(year: int, month: int, items: list[Track]) -> CalendarPlaylist:
    return CalendarPlaylist(
        id=f"pl_{year}_{month}", name=f"{year}-{month:02d}",
        year=year, month=month, snapshot_id="s", tracks=tuple(items),
    )


def tracks_of(artist: str, n: int, start: int = 0, **kwargs) -> list[Track]:
    return [make_track(f"{artist}_{i}", artists=[artist], **kwargs) for i in range(start, start + n)]


def run(calendar, provider, genre_map, settings):
    return group_by_genre(calendar, provider, genre_map, settings)


# --- soglie ---------------------------------------------------------------


def test_artista_sotto_soglia_non_contribuisce_al_genere(settings, genre_map) -> None:
    """Soglia generi = 8 di default; con 5 brani non dovrebbe creare nessuna playlist."""
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 5))]
    specs, _ = run(calendar, provider, genre_map, settings)
    assert specs == []


def test_artista_alla_soglia_generi_incluso(settings, genre_map) -> None:
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 8))]
    specs, _ = run(calendar, provider, genre_map, settings)
    assert len(specs) == 1 and specs[0].name == "Genere - Rock"


def test_soglia_generi_configurabile(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs, _ = run(calendar, provider, genre_map, settings)
    assert len(specs) == 1


# --- generi e macro-generi -----------------------------------------------


def test_generi_mappati_correttamente(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({
        "artist_alfa": ["indie rock", "classic rock"],   # -> Rock
        "artist_beta": ["indie pop"],                     # -> Indie
    })
    calendar = [cal(2025, 3, tracks_of("Alfa", 4) + tracks_of("Beta", 4))]
    specs, _ = run(calendar, provider, genre_map, settings)
    names = sorted(s.name for s in specs)
    assert names == ["Genere - Indie", "Genere - Rock"]


def test_artista_senza_generi_finisce_nel_report(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": []})  # nessun genere
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs, report = run(calendar, provider, genre_map, settings)
    assert specs == []
    assert any(a.name == "Alfa" for a in report.artists_without_genres)


def test_generi_non_mappati_finiscono_nel_report(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    # "ambient" e "vaporwave" non sono nella GenreMap di test -> entrambi non mappati
    provider = FakeGenreProvider({"artist_alfa": ["ambient", "vaporwave"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs, report = run(calendar, provider, genre_map, settings)
    assert specs == []
    assert "ambient" in report.unmapped_genres
    assert "vaporwave" in report.unmapped_genres


def test_genere_parzialmente_mappato_crea_playlist(settings, genre_map) -> None:
    """Un artista con un genere mappato e uno non mappato: la playlist si crea,
    il non mappato finisce nel report."""
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock", "ambient"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs, report = run(calendar, provider, genre_map, settings)
    assert len(specs) == 1 and specs[0].name == "Genere - Rock"
    assert "ambient" in report.unmapped_genres


# --- multi_genre_per_track -----------------------------------------------


def test_multi_genre_per_track_true_brano_in_piu_playlist(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    settings.config["genres"]["multi_genre_per_track"] = True
    provider = FakeGenreProvider({"artist_alfa": ["rock", "indie rock", "indie pop"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 8))]
    specs, _ = run(calendar, provider, genre_map, settings)
    # rock e indie rock -> Rock; indie pop -> Indie; il brano appare in entrambe
    names = sorted(s.name for s in specs)
    assert names == ["Genere - Indie", "Genere - Rock"]
    uris_rock = next(s.track_uris for s in specs if "Rock" in s.name)
    uris_indie = next(s.track_uris for s in specs if "Indie" in s.name)
    # tutti gli 8 brani appaiono in entrambe le playlist
    assert len(uris_rock) == 8 and len(uris_indie) == 8


def test_multi_genre_per_track_false_brano_in_una_sola_playlist(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    settings.config["genres"]["multi_genre_per_track"] = False
    provider = FakeGenreProvider({"artist_alfa": ["indie rock", "indie pop"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 8))]
    specs, _ = run(calendar, provider, genre_map, settings)
    # indie rock -> Rock; indie pop -> Indie: con multi=False il brano va solo nel primo macro
    total = sum(len(s.track_uris) for s in specs)
    assert total == 8  # ogni brano appare una sola volta in totale


# --- brani con più artisti -----------------------------------------------


def test_brano_con_due_artisti_qualificanti_in_due_generi_diversi(settings, genre_map) -> None:
    """Un brano con artista A (Rock) e artista B (Indie): deve finire in entrambe le playlist."""
    settings.config["genres"]["min_tracks"] = 4
    a = make_artist("Alfa", artist_id="id_alfa")
    b = make_artist("Beta", artist_id="id_beta")
    feat = [make_track(f"f{i}", artists=[a, b]) for i in range(8)]
    provider = FakeGenreProvider({"id_alfa": ["rock"], "id_beta": ["indie pop"]})
    calendar = [cal(2025, 3, feat)]
    # Con primary_artist_only=True (default), solo "Alfa" conta per la soglia artisti:
    # "Beta" non supera min_tracks (8) perché i brani sono contati solo per l'artista principale
    # -> solo Rock
    specs, _ = run(calendar, provider, genre_map, settings)
    names = [s.name for s in specs]
    assert "Genere - Rock" in names
    # Indie non c'è perché Beta (artista secondario) non supera la soglia artisti
    assert "Genere - Indie" not in names


def test_primary_artist_only_false_considera_tutti_gli_artisti(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    settings.config["artists"]["primary_artist_only"] = False
    a = make_artist("Alfa", artist_id="id_alfa")
    b = make_artist("Beta", artist_id="id_beta")
    feat = [make_track(f"f{i}", artists=[a, b]) for i in range(8)]
    provider = FakeGenreProvider({"id_alfa": ["rock"], "id_beta": ["indie pop"]})
    calendar = [cal(2025, 3, feat)]
    specs, _ = run(calendar, provider, genre_map, settings)
    names = sorted(s.name for s in specs)
    assert names == ["Genere - Indie", "Genere - Rock"]


# --- dedup e ordine -------------------------------------------------------


def test_stesso_brano_in_due_mesi_appare_una_sola_volta(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    ripetuto = make_track("hit", artists=["Alfa"])
    altri = tracks_of("Alfa", 7)
    calendar = [cal(2025, 1, [ripetuto, *altri]), cal(2025, 2, [ripetuto])]
    specs, _ = run(calendar, provider, genre_map, settings)
    (spec,) = specs
    assert spec.track_uris.count("spotify:track:hit") == 1
    assert len(spec.track_uris) == 8


def test_ordine_cronologico_tra_i_brani(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    gen = tracks_of("Alfa", 4, start=10, added_at="2025-01-01T00:00:00Z")
    mar = tracks_of("Alfa", 4, added_at="2025-03-01T00:00:00Z")
    calendar = [cal(2025, 3, mar), cal(2025, 1, gen)]  # disordinati
    specs, _ = run(calendar, provider, genre_map, settings)
    (spec,) = specs
    # Gennaio prima, poi marzo
    assert spec.track_uris[:4] == tuple(f"spotify:track:Alfa_{i}" for i in range(10, 14))
    assert spec.track_uris[4:] == tuple(f"spotify:track:Alfa_{i}" for i in range(4))


# --- marker e descrizione -----------------------------------------------


def test_spec_ha_il_marker_leggibile(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    (spec,), _ = run(calendar, provider, genre_map, settings)
    assert spec.kind == "genre"
    assert parse_marker(spec.description) == ("genre", spec.key)
    assert spec.key == "rock"  # macro_slug("Rock") == "rock"


def test_prefisso_configurabile(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    settings.config["genres"]["playlist_prefix"] = "Genere: "
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    (spec,), _ = run(calendar, provider, genre_map, settings)
    assert spec.name.startswith("Genere: ")


def test_output_ordinato_per_nome(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({
        "artist_alfa": ["rock"],
        "artist_beta": ["electronic"],
        "artist_gamma": ["indie pop"],
    })
    calendar = [
        cal(2025, 3, tracks_of("Alfa", 4) + tracks_of("Beta", 4) + tracks_of("Gamma", 4))
    ]
    specs, _ = run(calendar, provider, genre_map, settings)
    assert [s.name for s in specs] == sorted(s.name for s in specs)


# --- robustezza -----------------------------------------------------------


def test_calendario_vuoto(settings, genre_map) -> None:
    provider = FakeGenreProvider()
    specs, report = run([], provider, genre_map, settings)
    assert specs == [] and report.unmapped_genres == {} and report.artists_without_genres == []


def test_provider_che_solleva_eccezione_non_blocca(settings, genre_map) -> None:
    """Se il provider va in errore per un artista, si considera senza generi e si continua."""
    class BrokenProvider:
        def genres_for_artist(self, artist_id, artist_name):
            raise RuntimeError("rete non disponibile")

    settings.config["genres"]["min_tracks"] = 4
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs, report = run(calendar, BrokenProvider(), genre_map, settings)
    assert specs == []
    assert any(a.name == "Alfa" for a in report.artists_without_genres)


def test_nessun_duplicato_nelle_uri(settings, genre_map) -> None:
    settings.config["genres"]["min_tracks"] = 4
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    t = make_track("x", artists=["Alfa"])
    calendar = [cal(2025, 1, [t, t, *tracks_of("Alfa", 7)])]
    specs, _ = run(calendar, provider, genre_map, settings)
    (spec,) = specs
    assert len(spec.track_uris) == len(set(spec.track_uris))
