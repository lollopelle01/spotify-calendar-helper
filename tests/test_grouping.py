"""Test del raggruppamento per artista (task C1). Nessuna rete."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.grouping import (
    MAX_NAME_LENGTH,
    chronological_unique_tracks,
    group_by_artist,
    normalize_title,
    sanitize_playlist_name,
)
from spotify_calendar_helper.models import ArtistRef, CalendarPlaylist, Track, parse_marker
from tests.fakes import make_artist, make_track


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(data_dir=tmp_path)


def cal(year: int, month: int, tracks: list[Track], pid: str | None = None) -> CalendarPlaylist:
    return CalendarPlaylist(
        id=pid or f"pl_{year}_{month}",
        name=f"{year}-{month:02d}",
        year=year,
        month=month,
        snapshot_id="s",
        tracks=tuple(tracks),
    )


def tracks_of(artist: str, n: int, start: int = 0, **kwargs) -> list[Track]:
    return [make_track(f"{artist}_{i}", artists=[artist], **kwargs) for i in range(start, start + n)]


# --- soglie ---------------------------------------------------------------


def test_artista_sotto_soglia_escluso(settings: Settings) -> None:
    calendar = [cal(2025, 3, tracks_of("Alfa", 3))]
    assert group_by_artist(calendar, settings) == []


def test_artista_alla_soglia_incluso(settings: Settings) -> None:
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    specs = group_by_artist(calendar, settings)
    assert [s.name for s in specs] == ["Artista - Alfa"]
    assert len(specs[0].track_uris) == 4


def test_soglia_configurabile(settings: Settings) -> None:
    settings.config["artists"]["min_tracks"] = 2
    calendar = [cal(2025, 3, tracks_of("Alfa", 2) + tracks_of("Beta", 1))]
    assert [s.name for s in group_by_artist(calendar, settings)] == ["Artista - Alfa"]


def test_prefisso_configurabile(settings: Settings) -> None:
    settings.config["artists"]["playlist_prefix"] = "Mix: "
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    assert group_by_artist(calendar, settings)[0].name == "Mix: Alfa"


# --- ordine e duplicati ---------------------------------------------------


def test_ordine_cronologico_tra_mesi_anche_se_il_calendario_e_disordinato(settings: Settings) -> None:
    mar = tracks_of("Alfa", 2, added_at="2025-03-10T00:00:00Z")
    gen = tracks_of("Alfa", 2, start=10, added_at="2025-01-10T00:00:00Z")
    specs = group_by_artist([cal(2025, 3, mar), cal(2025, 1, gen)], settings)
    assert specs[0].track_uris == (
        "spotify:track:Alfa_10",
        "spotify:track:Alfa_11",
        "spotify:track:Alfa_0",
        "spotify:track:Alfa_1",
    )


def test_ordine_per_added_at_dentro_il_mese_e_senza_data_in_fondo(settings: Settings) -> None:
    tracks = [
        make_track("senza_data", artists=["Alfa"], added_at=None),
        make_track("tardi", artists=["Alfa"], added_at="2025-03-20T00:00:00Z"),
        make_track("presto", artists=["Alfa"], added_at="2025-03-02T00:00:00Z"),
        make_track("senza_data_2", artists=["Alfa"], added_at=None),
    ]
    specs = group_by_artist([cal(2025, 3, tracks)], settings)
    assert [u.rsplit(":", 1)[1] for u in specs[0].track_uris] == [
        "presto",
        "tardi",
        "senza_data",
        "senza_data_2",
    ]


def test_stesso_brano_in_due_mesi_conta_una_volta(settings: Settings) -> None:
    ripetuto = make_track("hit", artists=["Alfa"])
    altri = tracks_of("Alfa", 3)
    specs = group_by_artist([cal(2025, 1, [ripetuto, *altri]), cal(2025, 2, [ripetuto])], settings)
    assert specs[0].track_uris.count("spotify:track:hit") == 1
    assert len(specs[0].track_uris) == 4


def test_nessun_duplicato_nelle_uri(settings: Settings) -> None:
    t = make_track("x", artists=["Alfa"])
    calendar = [cal(2025, 1, [t, t, *tracks_of("Alfa", 3)])]
    uris = group_by_artist(calendar, settings)[0].track_uris
    assert len(uris) == len(set(uris))


# --- più artisti ----------------------------------------------------------


def test_solo_artista_principale_per_default(settings: Settings) -> None:
    feat = [make_track(f"f{i}", artists=["Alfa", "Beta"]) for i in range(4)]
    specs = group_by_artist([cal(2025, 3, feat)], settings)
    assert [s.name for s in specs] == ["Artista - Alfa"]


def test_tutti_gli_artisti_se_primary_only_falso(settings: Settings) -> None:
    settings.config["artists"]["primary_artist_only"] = False
    feat = [make_track(f"f{i}", artists=["Alfa", "Beta"]) for i in range(4)]
    specs = group_by_artist([cal(2025, 3, feat)], settings)
    assert [s.name for s in specs] == ["Artista - Alfa", "Artista - Beta"]
    assert specs[0].track_uris == specs[1].track_uris


def test_stesso_artista_ripetuto_nel_brano_conta_una_volta(settings: Settings) -> None:
    settings.config["artists"]["primary_artist_only"] = False
    a = make_artist("Alfa")
    tracks = [make_track(f"d{i}", artists=[a, a]) for i in range(4)]
    specs = group_by_artist([cal(2025, 3, tracks)], settings)
    assert len(specs[0].track_uris) == 4


# --- brani non raggruppabili ----------------------------------------------


def test_brani_senza_artisti_ignorati(settings: Settings) -> None:
    orfani = [make_track(f"o{i}", artists=[]) for i in range(10)]
    assert group_by_artist([cal(2025, 3, orfani)], settings) == []


def test_artista_con_id_vuoto_o_invalido_ignorato(settings: Settings) -> None:
    # make_artist() sostituisce un ID vuoto con uno generato: qui serve un ArtistRef vero.
    vuoto = [make_track(f"v{i}", artists=[ArtistRef(id="", name="Local")]) for i in range(4)]
    spazio = [make_track(f"s{i}", artists=[make_artist("Strano", artist_id="a b")]) for i in range(4)]
    assert group_by_artist([cal(2025, 3, vuoto + spazio)], settings) == []


# --- marker, descrizione, nomi ---------------------------------------------


def test_descrizione_contiene_il_marker_leggibile(settings: Settings) -> None:
    calendar = [cal(2025, 3, tracks_of("Alfa", 4))]
    spec = group_by_artist(calendar, settings)[0]
    assert spec.kind == "artist"
    assert parse_marker(spec.description) == ("artist", spec.key)
    assert spec.key == "artist_alfa"


def test_omonimi_ricevono_un_suffisso_diverso(settings: Settings) -> None:
    uno = [make_track(f"u{i}", artists=[make_artist("Nome", artist_id="idUNO12345")]) for i in range(4)]
    due = [make_track(f"d{i}", artists=[make_artist("Nome", artist_id="idDUE67890")]) for i in range(4)]
    specs = group_by_artist([cal(2025, 3, uno + due)], settings)
    names = [s.name for s in specs]
    assert len(set(names)) == 2
    assert all(n.startswith("Artista - Nome (") for n in names)


def test_nomi_diversi_non_ricevono_suffisso(settings: Settings) -> None:
    calendar = [cal(2025, 3, tracks_of("Alfa", 4) + tracks_of("Beta", 4))]
    assert [s.name for s in group_by_artist(calendar, settings)] == ["Artista - Alfa", "Artista - Beta"]


def test_nome_lungo_viene_accorciato(settings: Settings) -> None:
    lungo = "X" * 300
    tracks = [make_track(f"l{i}", artists=[make_artist(lungo, artist_id="idlungo")]) for i in range(4)]
    spec = group_by_artist([cal(2025, 3, tracks)], settings)[0]
    assert len(spec.name) <= MAX_NAME_LENGTH
    assert spec.name.endswith("…")
    assert parse_marker(spec.description) == ("artist", "idlungo")


def test_output_ordinato_per_nome(settings: Settings) -> None:
    calendar = [cal(2025, 3, tracks_of("zeta", 4) + tracks_of("Alfa", 4) + tracks_of("beta", 4))]
    assert [s.name for s in group_by_artist(calendar, settings)] == [
        "Artista - Alfa",
        "Artista - beta",
        "Artista - zeta",
    ]


def test_calendario_vuoto(settings: Settings) -> None:
    assert group_by_artist([], settings) == []


def test_non_modifica_gli_input(settings: Settings) -> None:
    playlist = cal(2025, 3, tracks_of("Alfa", 4))
    before = (playlist.tracks, list(settings.config["artists"].items()))
    group_by_artist([playlist], settings)
    assert (playlist.tracks, list(settings.config["artists"].items())) == before


# --- dedupe_same_song -------------------------------------------------------


def test_dedupe_same_song_unisce_le_versioni_remaster(settings: Settings) -> None:
    settings.config["sync"]["dedupe_same_song"] = True
    versioni = [
        make_track("a1", "Canzone", ["Alfa"]),
        make_track("a2", "Canzone - Remastered 2011", ["Alfa"]),
        make_track("a3", "Canzone (Deluxe Edition)", ["Alfa"]),
        make_track("a4", "Altra", ["Alfa"]),
        make_track("a5", "Ancora", ["Alfa"]),
        make_track("a6", "Piu", ["Alfa"]),
    ]
    spec = group_by_artist([cal(2025, 3, versioni)], settings)[0]
    assert len(spec.track_uris) == 4


def test_dedupe_same_song_non_unisce_remix_e_live(settings: Settings) -> None:
    settings.config["sync"]["dedupe_same_song"] = True
    tracks = [
        make_track("b1", "Canzone", ["Alfa"]),
        make_track("b2", "Canzone (Remix)", ["Alfa"]),
        make_track("b3", "Canzone - Live", ["Alfa"]),
        make_track("b4", "Canzone (Live)", ["Alfa"]),
    ]
    assert len(group_by_artist([cal(2025, 3, tracks)], settings)[0].track_uris) == 4


def test_dedupe_same_song_distingue_artisti_diversi(settings: Settings) -> None:
    settings.config["sync"]["dedupe_same_song"] = True
    settings.config["artists"]["min_tracks"] = 1
    tracks = [make_track("c1", "Canzone", ["Alfa"]), make_track("c2", "Canzone", ["Beta"])]
    assert len(group_by_artist([cal(2025, 3, tracks)], settings)) == 2


def test_senza_dedupe_le_versioni_restano_distinte(settings: Settings) -> None:
    tracks = [make_track("d1", "Canzone", ["Alfa"]), make_track("d2", "Canzone - Remastered", ["Alfa"])]
    settings.config["artists"]["min_tracks"] = 1
    assert len(group_by_artist([cal(2025, 3, tracks)], settings)[0].track_uris) == 2


# --- funzioni di supporto ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Due   spazi ", "Due spazi"),
        ("a\nb\tc", "a b c"),
        ("zero\u200bwidth", "zerowidth"),
        ("", "Senza nome"),
        ("   ", "Senza nome"),
    ],
)
def test_sanitize_playlist_name(raw: str, expected: str) -> None:
    assert sanitize_playlist_name(raw) == expected


def test_sanitize_accorcia() -> None:
    out = sanitize_playlist_name("a" * 50, max_length=10)
    assert len(out) == 10 and out.endswith("…")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Canzone", "canzone"),
        ("Canzone - Remastered 2011", "canzone"),
        ("Canzone (Remastered)", "canzone"),
        ("Canzone [Mono]", "canzone"),
        ("Canzone (Remix)", "canzone (remix)"),
        ("Canzone - Live at Wembley", "canzone - live at wembley"),
        ("  CANZONE  ", "canzone"),
    ],
)
def test_normalize_title(title: str, expected: str) -> None:
    assert normalize_title(title) == expected


def test_chronological_unique_tracks_ignora_brani_senza_uri() -> None:
    senza_uri = Track(id="x", uri="", name="n", artists=(make_artist("Alfa"),))
    assert chronological_unique_tracks([cal(2025, 3, [senza_uri])]) == []
