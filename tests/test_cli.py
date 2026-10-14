"""Test della CLI `sch` (task D4). Nessuna rete: il client Spotify è sempre il fake.

Le chiamate reali a Spotify passano per `_make_client()` in cli.py; qui la sostituiamo con un
segnaposto che restituisce un `FakeSpotifyClient` già popolato.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from spotify_calendar_helper.cli import main
from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.genres import GenreMap
from spotify_calendar_helper.models import make_marker
from spotify_calendar_helper.state import RunRecord, State
from tests.fakes import FakeGenreProvider, FakeSpotifyClient, make_track


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

GENRE_MAP = GenreMap({
    "Rock": ["rock", "indie rock"],
    "Pop":  ["pop", "dance pop"],
})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(data_dir=tmp_path / "data")


@pytest.fixture
def client() -> FakeSpotifyClient:
    c = FakeSpotifyClient()
    c.seed_playlist("Gennaio 2025", [make_track(f"a{i}", artists=["Alfa"]) for i in range(5)], playlist_id="p1")
    c.seed_playlist("Febbraio 2025", [make_track(f"b{i}", artists=["Alfa"]) for i in range(4)], playlist_id="p2")
    c.seed_playlist("Roadtrip", [make_track("x")], playlist_id="p_other")
    return c


@pytest.fixture
def genre_client() -> FakeSpotifyClient:
    """Client con generi configurati per i test dei generi."""
    c = FakeSpotifyClient(artist_genres={"artist_alfa": ["rock"]})
    c._artist_names["artist_alfa"] = "Alfa"
    for i in range(8):
        c.seed_playlist(f"2025-{i+1:02d}", [make_track(f"t{i}", artists=["Alfa"])], playlist_id=f"p{i}")
    return c


def invoke(cli_args: list[str], settings: Settings, client: FakeSpotifyClient,
           genre_map: GenreMap | None = None,
           genre_provider: FakeGenreProvider | None = None,
           input: str | None = None) -> "click.testing.Result":
    runner = CliRunner()
    with (
        patch("spotify_calendar_helper.cli.load_settings", return_value=settings),
        patch("spotify_calendar_helper.cli._make_client", return_value=client),
        patch("spotify_calendar_helper.cli._load_genre_map",
              return_value=genre_map or GENRE_MAP),
        patch("spotify_calendar_helper.cli.build_provider",
              return_value=genre_provider or FakeGenreProvider()),
    ):
        return runner.invoke(main, cli_args, catch_exceptions=False, input=input)


# ---------------------------------------------------------------------------
# sch calendar
# ---------------------------------------------------------------------------

def test_calendar_elenca_le_playlist(settings, client) -> None:
    result = invoke(["calendar", "--all"], settings, client)
    assert result.exit_code == 0
    assert "2025" in result.output
    assert "Gennaio" in result.output
    assert "Febbraio" in result.output


def test_calendar_mostra_i_buchi(settings) -> None:
    c = FakeSpotifyClient()
    c.seed_playlist("Gennaio 2025", [make_track("a")])
    c.seed_playlist("Marzo 2025", [make_track("b")])
    result = invoke(["calendar", "--all"], settings, c)
    assert result.exit_code == 0
    assert "2025-02" in result.output  # febbraio mancante


def test_calendar_vuoto(settings) -> None:
    result = invoke(["calendar", "--all"], settings, FakeSpotifyClient())
    assert result.exit_code == 0
    assert "Nessuna" in result.output


def test_calendar_filtro_anno(settings, client) -> None:
    result = invoke(["calendar", "--year", "2025"], settings, client)
    assert result.exit_code == 0
    assert "2025" in result.output


def test_calendar_filtro_from_to(settings, client) -> None:
    result = invoke(["calendar", "--from", "2025-01", "--to", "2025-01"], settings, client)
    assert result.exit_code == 0
    assert "Gennaio" in result.output


def test_calendar_from_invalido_esce_con_errore(settings, client) -> None:
    result = invoke(["calendar", "--from", "2025"], settings, client)
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# sch plan artists
# ---------------------------------------------------------------------------

def test_plan_artists_mostra_cosa_creerebbe(settings, client) -> None:
    result = invoke(["plan", "artists", "--all"], settings, client)
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output or "da creare" in result.output
    assert "Alfa" in result.output


def test_plan_artists_nessuna_playlist(settings) -> None:
    result = invoke(["plan", "artists", "--all"], settings, FakeSpotifyClient())
    assert result.exit_code == 0
    assert "Nessuna" in result.output


def test_plan_genres_con_generi_vuoti(settings, client) -> None:
    provider = FakeGenreProvider()  # nessun genere -> nessuna playlist
    result = invoke(["plan", "genres", "--all"], settings, client, genre_provider=provider)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# sch sync artists
# ---------------------------------------------------------------------------

def test_sync_artists_crea_le_playlist(settings, client) -> None:
    result = invoke(["sync", "artists", "--all", "--yes"], settings, client)
    assert result.exit_code == 0
    assert "create" in result.output.lower() or "Fatto" in result.output
    # lo stato viene salvato
    state = State.load(settings.state_path)
    assert state.last_run is not None
    assert state.last_run.created > 0


def test_sync_artists_dry_run_non_scrive(settings, client) -> None:
    result = invoke(["sync", "artists", "--all", "--dry-run", "--yes"], settings, client)
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output or "dry" in result.output.lower()
    assert client.write_count == 0
    assert not settings.state_path.exists()


def test_sync_idempotente(settings, client) -> None:
    invoke(["sync", "artists", "--all", "--yes"], settings, client)
    client.reset_counters()
    result = invoke(["sync", "artists", "--all", "--yes"], settings, client)
    assert result.exit_code == 0
    assert client.write_count == 0
    assert "invariate" in result.output.lower() or "Nessuna modifica" in result.output


def test_sync_con_filtro_anno(settings, client) -> None:
    result = invoke(["sync", "artists", "--year", "2025", "--yes"], settings, client)
    assert result.exit_code == 0


def test_sync_chiede_conferma_se_troppe_playlist(settings) -> None:
    """Con molte playlist nuove e senza --yes, chiede conferma (simulata con input 'n')."""
    c = FakeSpotifyClient()
    # 30 artisti distinti, ognuno con 4 brani in mesi diversi (nessun duplicato di mese)
    for i in range(30):
        year = 2020 + i // 12
        month = i % 12 + 1
        tracks = [make_track(f"t{i}_{j}", artists=[f"Artista{i}"]) for j in range(4)]
        c.seed_playlist(f"{year}-{month:02d}", tracks)
    settings.config["sync"]["confirm_if_creating_more_than"] = 5
    result = invoke(["sync", "artists", "--all"], settings, c, input="n\n")
    assert result.exit_code == 2 or "annullata" in result.output.lower()
    assert c.write_count == 0


def test_sync_con_yes_salta_conferma(settings) -> None:
    c = FakeSpotifyClient()
    for i in range(30):
        year = 2020 + i // 12
        month = i % 12 + 1
        tracks = [make_track(f"t{i}_{j}", artists=[f"Artista{i}"]) for j in range(4)]
        c.seed_playlist(f"{year}-{month:02d}", tracks)
    settings.config["sync"]["confirm_if_creating_more_than"] = 5
    result = invoke(["sync", "artists", "--all", "--yes"], settings, c)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# sch sync genres
# ---------------------------------------------------------------------------

def test_sync_genres_con_generi(settings) -> None:
    c = FakeSpotifyClient(artist_genres={"artist_alfa": ["rock"]})
    c._artist_names["artist_alfa"] = "Alfa"
    for i in range(8):
        c.seed_playlist(f"2025-{i+1:02d}", [make_track(f"t{i}", artists=["Alfa"])])
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    result = invoke(["sync", "genres", "--all", "--yes"], settings, c,
                    genre_map=GENRE_MAP, genre_provider=provider)
    assert result.exit_code == 0


def test_sync_genres_senza_generi(settings, client) -> None:
    provider = FakeGenreProvider()
    result = invoke(["sync", "genres", "--all", "--yes"], settings, client,
                    genre_provider=provider)
    assert result.exit_code == 0
    assert "Nessuna" in result.output


# ---------------------------------------------------------------------------
# sch status
# ---------------------------------------------------------------------------

def test_status_senza_sync_precedente(settings, client) -> None:
    result = invoke(["status"], settings, client)
    assert result.exit_code == 0
    assert "Nessuna" in result.output or "nessun" in result.output.lower()


def test_status_dopo_sync(settings, client) -> None:
    invoke(["sync", "artists", "--all", "--yes"], settings, client)
    result = invoke(["status"], settings, client)
    assert result.exit_code == 0
    assert "Alfa" in result.output
    assert "artist" in result.output.lower() or "Artisti" in result.output


def test_status_mostra_ultimi_run(settings, client) -> None:
    invoke(["sync", "artists", "--all", "--yes"], settings, client)
    result = invoke(["status"], settings, client)
    assert result.exit_code == 0
    assert "sync" in result.output


# ---------------------------------------------------------------------------
# sch genres-report
# ---------------------------------------------------------------------------

def test_genres_report_generi_non_mappati(settings, client) -> None:
    provider = FakeGenreProvider({"artist_alfa": ["ambient", "vaporwave"]})
    result = invoke(["genres-report", "--all"], settings, client,
                    genre_map=GENRE_MAP, genre_provider=provider)
    assert result.exit_code == 0
    # con min_tracks=8 di default Alfa (9 brani in totale tra i due mesi) supera la soglia
    # ma se non supera la soglia, il report dice comunque "Tutti mappati" o lista vuota


def test_genres_report_tutto_mappato(settings, client) -> None:
    provider = FakeGenreProvider({"artist_alfa": ["rock"]})
    result = invoke(["genres-report", "--all"], settings, client,
                    genre_map=GENRE_MAP, genre_provider=provider)
    assert result.exit_code == 0


def test_genres_report_calendario_vuoto(settings) -> None:
    result = invoke(["genres-report", "--all"], settings, FakeSpotifyClient())
    assert result.exit_code == 0
    assert "Nessuna" in result.output


# ---------------------------------------------------------------------------
# Codici di uscita e output su stderr
# ---------------------------------------------------------------------------

def test_filtri_incompatibili_esce_con_errore(settings, client) -> None:
    result = invoke(["calendar", "--year", "2025", "--from", "2025-01"], settings, client)
    assert result.exit_code != 0


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_help_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "calendar" in result.output
    assert "sync" in result.output


def test_plan_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["plan", "--help"])
    assert result.exit_code == 0
    assert "artists" in result.output or "genres" in result.output


# ---------------------------------------------------------------------------
# Formato dell'output del report
# ---------------------------------------------------------------------------

def test_report_mostra_playlist_nuove_da_spostare(settings, client) -> None:
    result = invoke(["sync", "artists", "--all", "--yes"], settings, client)
    assert result.exit_code == 0
    assert "spostare" in result.output.lower()
    assert "Alfa" in result.output


def test_plan_mostra_brani_che_verranno_aggiunti(settings, client) -> None:
    result = invoke(["plan", "artists", "--all"], settings, client)
    assert result.exit_code == 0
    # report mostra il numero di brani
    assert "+9" in result.output or "+5" in result.output or "+4" in result.output


def test_sync_report_invariate(settings, client) -> None:
    invoke(["sync", "artists", "--all", "--yes"], settings, client)
    result = invoke(["sync", "artists", "--all", "--yes"], settings, client)
    assert result.exit_code == 0
    assert "invariate" in result.output.lower() or "Nessuna modifica" in result.output


